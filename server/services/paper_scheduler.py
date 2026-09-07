"""Automatic paper-account valuation task.

The scheduler is deliberately fail-closed: if a market adapter cannot return
an acceptable, timestamped observation, that market is deferred and the run
records the boundary. It never substitutes a fabricated price or routes a
cross-market symbol through the A-share provider.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from quant_engine.data.live import LiveMarketDataProvider, MarketDataUnavailableError, MarketQuote, SHANGHAI_TZ
from server.models.database import SessionLocal
from server.models.schema import PaperAccount, PaperSchedulerRun, StrategyObservation
from server.services.paper_trading import build_daily_report, mark_to_market
from server.services.fund_nav_registry import register_fund_nav
from server.services.paper_market_rules import (
    A_SHARE,
    CN_FUND,
    US_EQUITY,
    accepts_quote_freshness,
    is_market_trading_day,
    market_session_status,
    normalize_market,
    normalize_symbol,
)

logger = logging.getLogger(__name__)


def _as_utc(value: str) -> datetime:
    """Normalize legacy naive and newer timezone-aware audit timestamps."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        # An unreadable timestamp must never suppress a safety re-run.
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        # Existing ORM defaults use server-local naive timestamps (Shanghai in
        # the supported deployment).  Interpret them in the application TZ.
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc)


def _min_quote_as_of(values: list[Optional[str]]) -> Optional[str]:
    """Return the earliest actual instant, independent of ISO offset spelling."""
    parsed: list[datetime] = []
    for value in values:
        if not value:
            continue
        try:
            item = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if item.tzinfo is None:
            item = item.replace(tzinfo=SHANGHAI_TZ)
        parsed.append(item.astimezone(timezone.utc))
    return min(parsed).isoformat() if parsed else None


class PaperDailyScheduler:
    """Idempotent trading-day runner used by the application lifespan."""

    def __init__(
        self,
        provider: Optional[LiveMarketDataProvider] = None,
        interval_seconds: int = 300,
        market_providers: Optional[Mapping[str, object]] = None,
        broadcast: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ):
        if interval_seconds < 10:
            raise ValueError("interval_seconds must be at least 10 seconds")
        self.provider = provider
        # Cross-market providers are explicit capabilities. Keeping this
        # mapping separate from the A-share provider prevents a fund code or
        # US ticker from ever reaching AKShare by accident.
        self.market_providers = {
            normalize_market(market): value
            for market, value in (market_providers or {}).items()
        }
        self.interval_seconds = interval_seconds
        self._broadcast = broadcast
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = asyncio.Event()
        self._run_lock = threading.Lock()

    def _publish_valuation(self, account_id: str, valuation: dict) -> None:
        """Publish a committed checkpoint without coupling the worker to I/O."""
        if self._broadcast is None or self._loop is None or not self._loop.is_running():
            return
        message = {
            "type": "portfolio_update",
            "account_id": account_id,
            "data": valuation,
            "execution_mode": "paper_only",
            "paper_only": True,
            "live_execution": False,
        }
        for channel in (f"paper:{account_id}", "dashboard"):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._broadcast(channel, message), self._loop
                )
                future.add_done_callback(self._consume_broadcast_result)
            except Exception:
                logger.debug("paper valuation broadcast scheduling failed", exc_info=True)

    @staticmethod
    def _consume_broadcast_result(future) -> None:
        try:
            future.result()
        except Exception:
            logger.debug("paper valuation broadcast failed", exc_info=True)

    def _fetch_market_quotes(
        self,
        db,
        *,
        market: str,
        requested_codes: list[str],
        day: date,
    ) -> dict[str, MarketQuote]:
        """Fetch one non-A-share market with strict provenance checks."""
        if not requested_codes:
            return {}
        provider = self.market_providers.get(market)
        if provider is None:
            raise MarketDataUnavailableError([{
                "source": f"scheduler:{market}",
                "error": "provider_not_configured",
            }])
        try:
            quotes = provider.fetch_quotes(requested_codes)
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError([{
                "source": f"scheduler:{market}",
                "error": f"{type(exc).__name__}:{exc}",
            }]) from exc
        result: dict[str, MarketQuote] = {}
        for quote in quotes or []:
            try:
                code = normalize_symbol(market, quote.code)
                price = float(quote.price)
            except (TypeError, ValueError):
                continue
            if (
                code not in requested_codes
                or not math.isfinite(price)
                or not price > 0
                or not quote.as_of
                or str(quote.freshness or "").lower() == "manual"
                or not accepts_quote_freshness(market, quote.freshness)
            ):
                continue
            expected_asset_type = "fund" if market == CN_FUND else "equity"
            expected_currency = "CNY" if market == CN_FUND else "USD"
            quote_market = str(getattr(quote, "market", "") or "").strip().lower()
            accepted_markets = {"cn-fund"} if market == CN_FUND else {"us", "us-equity"}
            if (
                quote_market not in accepted_markets
                or str(getattr(quote, "asset_type", "") or "").strip().lower() != expected_asset_type
                or str(getattr(quote, "currency", "") or "").strip().upper() != expected_currency
            ):
                continue
            try:
                observed_at_raw = datetime.fromisoformat(str(quote.as_of).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if observed_at_raw.tzinfo is None:
                observed_at_raw = observed_at_raw.replace(tzinfo=SHANGHAI_TZ)
            observed_at = observed_at_raw.astimezone(timezone.utc)
            if observed_at > datetime.now(timezone.utc) + timedelta(seconds=5):
                continue
            # A current snapshot can be stale, but it must never be a future
            # observation relative to the accounting date.
            market_zone = ZoneInfo("Asia/Shanghai") if market == CN_FUND else ZoneInfo("America/New_York")
            if observed_at.astimezone(market_zone).date() > day:
                continue
            if market == CN_FUND:
                # Fund fills/valuations require exact durable Eastmoney
                # evidence, not caller-supplied source labels.
                if str(quote.source) != "eastmoney:fund_nav":
                    continue
                try:
                    register_fund_nav(
                        code=code,
                        price=price,
                        source=quote.source,
                        as_of=quote.as_of,
                        freshness=quote.freshness,
                        received_at=quote.received_at,
                        db=db,
                    )
                except Exception as exc:
                    raise MarketDataUnavailableError([{
                        "source": "eastmoney:fund_nav",
                        "symbol": code,
                        "error": f"evidence_persistence:{type(exc).__name__}:{exc}",
                    }]) from exc
            result[code] = quote
        missing = [code for code in requested_codes if code not in result]
        if missing:
            raise MarketDataUnavailableError([{
                "source": getattr(provider, "source_name", f"scheduler:{market}"),
                "error": f"missing_or_invalid_quotes:{','.join(missing)}",
            }])
        return result

    def run_once(self, run_date: Optional[date] = None) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {
                "run_date": (run_date or datetime.now(SHANGHAI_TZ).date()).isoformat(),
                "status": "busy", "account_count": 0, "valuation_count": 0,
                "reason": "another_scheduler_run_in_progress",
                "execution_mode": "paper_only", "paper_only": True, "live_execution": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        try:
            return self._run_once(run_date)
        finally:
            self._run_lock.release()

    def _run_once(self, run_date: Optional[date] = None) -> dict:
        automatic = run_date is None
        today = datetime.now(SHANGHAI_TZ).date()
        day = run_date or today
        if day > today:
            raise ValueError("run_date_in_future")
        # Live providers normally expose current snapshots.  It is unsafe to
        # apply those prices to a historical date, so a live implementation
        # must explicitly opt in if it can serve historical data.  Lightweight
        # test/replay providers need not inherit the live-provider contract.
        if (isinstance(self.provider, LiveMarketDataProvider)
                and not getattr(self.provider, "supports_historical_dates", False)
                and day != today):
            raise ValueError("live_scheduler_date_must_be_today")
        if automatic and datetime.now(SHANGHAI_TZ).time() < time(15, 5):
            return {
                "run_date": day.isoformat(), "status": "skipped", "account_count": 0,
                "valuation_count": 0, "reason": "market_not_closed",
                "execution_mode": "paper_only", "paper_only": True, "live_execution": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        db = SessionLocal()
        try:
            existing = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first()
            if existing and existing.status == "completed":
                from server.models.schema import PaperOrder
                latest_order = db.query(PaperOrder).order_by(PaperOrder.created_at.desc()).first()
                watermark = existing.last_run_at or existing.created_at
                active_observations = sum(
                    1 for observation, account in
                    db.query(StrategyObservation, PaperAccount)
                    .join(PaperAccount, PaperAccount.id == StrategyObservation.account_id)
                    .filter(StrategyObservation.status == "running").all()
                    if normalize_market(account.market) in {A_SHARE, CN_FUND}
                )
                deferred_retry = str(existing.reason or "").startswith("deferred_market_adapters:")
                if (not deferred_retry and not active_observations
                        and (not latest_order or _as_utc(latest_order.created_at) <= _as_utc(watermark))):
                    return self._run_dict(existing)
            accounts = db.query(PaperAccount).filter(PaperAccount.status == "active").all()
            # A mixed-market account set may have a valid session even when
            # the mainland calendar is closed (for example a US session on a
            # mainland holiday). Keep unavailable calendars isolated to their
            # market instead of silently treating them as a normal closure.
            session_statuses = {
                normalize_market(account.market): market_session_status(account.market, day)
                for account in accounts
            }
            calendar_unavailable_markets = {
                market for market, status in session_statuses.items()
                if status == "calendar_unavailable"
            }
            if not accounts or not any(status == "open" for status in session_statuses.values()):
                row = existing or PaperSchedulerRun(run_date=day.isoformat(), status="skipped")
                if calendar_unavailable_markets:
                    row.status = "blocked"
                    row.reason = "calendar_unavailable:" + ",".join(sorted(calendar_unavailable_markets))
                else:
                    row.status = "skipped"
                    row.reason = "not_a_trading_day"
                row.account_count, row.valuation_count = 0, 0
                if not existing:
                    db.add(row)
                db.commit()
                return self._run_dict(row)

            if day != today:
                unsupported_history = {
                    normalize_market(account.market)
                    for account in accounts
                    if normalize_market(account.market) != A_SHARE
                    and account_positions(db, account.id)
                    and self.market_providers.get(normalize_market(account.market)) is not None
                    and not getattr(
                        self.market_providers[normalize_market(account.market)],
                        "supports_historical_dates",
                        False,
                    )
                }
                if unsupported_history:
                    raise ValueError(
                        "cross_market_scheduler_date_must_be_today:"
                        + ",".join(sorted(unsupported_history))
                    )

            # The A-share provider remains isolated from cross-market feeds.
            # Each non-A-share market is fetched independently so one
            # provider outage defers only that market's accounts.
            a_share_accounts = [
                account for account in accounts
                if normalize_market(account.market) == A_SHARE
            ]
            codes = sorted({position.code for account in a_share_accounts for position in account_positions(db, account.id)})
            quote_map: dict[str, MarketQuote] = {}
            deferred_markets: set[str] = set()
            def fetch_quote_batch(requested_codes: list[str]) -> dict[str, MarketQuote]:
                """Fetch and validate one batch, preserving fail-closed semantics."""
                if not requested_codes:
                    return {}
                if self.provider is None:
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": "provider_not_configured"}])
                try:
                    quotes = self.provider.fetch_quotes(requested_codes)
                except MarketDataUnavailableError:
                    raise
                except Exception as exc:
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": f"{type(exc).__name__}:{exc}"}]) from exc
                result: dict[str, MarketQuote] = {}
                for quote in quotes or []:
                    try:
                        code = normalize_symbol(A_SHARE, quote.code)
                        price = float(quote.price)
                        observed_at = datetime.fromisoformat(str(quote.as_of).replace("Z", "+00:00"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if observed_at.tzinfo is None:
                        observed_at = observed_at.replace(tzinfo=SHANGHAI_TZ)
                    observed_at = observed_at.astimezone(timezone.utc)
                    if (
                        code not in requested_codes
                        or not math.isfinite(price)
                        or not price > 0
                        or not quote.as_of
                        or str(quote.freshness or "").lower() not in {"fresh", "realtime", "delayed"}
                        or not accepts_quote_freshness(A_SHARE, quote.freshness)
                        or observed_at.astimezone(SHANGHAI_TZ).date() > day
                        or observed_at > datetime.now(timezone.utc) + timedelta(seconds=5)
                    ):
                        continue
                    result[code] = quote
                if any(code not in result for code in requested_codes):
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": "missing_fresh_quote"}])
                return result

            if codes and A_SHARE not in calendar_unavailable_markets:
                try:
                    quote_map.update(fetch_quote_batch(codes))
                except MarketDataUnavailableError as exc:
                    deferred_markets.add(A_SHARE)
                    logger.warning("A-share scheduler adapter deferred: %s", exc)
            cross_quote_maps: dict[str, dict[str, MarketQuote]] = {}
            for market in (CN_FUND, US_EQUITY):
                if market in calendar_unavailable_markets:
                    continue
                market_accounts = [account for account in accounts if normalize_market(account.market) == market]
                market_codes = sorted({position.code for account in market_accounts for position in account_positions(db, account.id)})
                if not market_codes:
                    continue
                try:
                    cross_quote_maps[market] = self._fetch_market_quotes(
                        db, market=market, requested_codes=market_codes, day=day
                    )
                except MarketDataUnavailableError as exc:
                    deferred_markets.add(market)
                    logger.warning("cross-market scheduler adapter deferred %s: %s", market, exc)

            row = existing or PaperSchedulerRun(run_date=day.isoformat(), status="running")
            row.status, row.account_count, row.valuation_count, row.reason = "running", len(accounts), 0, None
            if not existing:
                db.add(row)
            db.commit()
            # Apply observation targets before the closing mark-to-market so
            # fills and the daily report belong to the same trading checkpoint.
            # Normalize legacy account market values in Python instead of
            # comparing the raw database string.  Older rows may contain
            # casing or surrounding whitespace; they must still be routed to
            # the A-share observation engine, never silently skipped.
            running_observation_rows = (db.query(StrategyObservation, PaperAccount)
                                        .join(PaperAccount, PaperAccount.id == StrategyObservation.account_id)
                                        .filter(StrategyObservation.status == "running")
                                        .all())
            from server.services.observation import run_observation_tick
            running_observations = [
                (observation, normalize_market(account.market))
                for observation, account in running_observation_rows
                if normalize_market(account.market) in {A_SHARE, CN_FUND}
            ]
            for observation, observation_market in running_observations:
                if observation_market in deferred_markets or observation_market in calendar_unavailable_markets:
                    continue
                observation_provider = self.provider if observation_market == A_SHARE else self.market_providers.get(CN_FUND)
                if observation_provider is None:
                    continue
                try:
                    # Current checkpoints use the provider's live snapshot so
                    # the observer stages D-day targets and executes only the
                    # prior target. Historical replay remains explicit.
                    tick_date = None if day == today else day
                    run_observation_tick(
                        db,
                        account_id=observation.account_id,
                        observation_id=observation.id,
                        as_of=tick_date,
                        quote_provider=observation_provider,
                    )
                except Exception:
                    # A blocked strategy observation must never undo a
                    # successful mark-to-market run for another account.
                    logger.exception("strategy observation tick failed: %s", observation.id)
            # An observation can open a new position that was not present when
            # the scheduler collected its initial quote set. Refresh only the
            # newly required symbols before valuation; otherwise the valuation
            # loop would dereference quote_map[code] and fail with KeyError.
            post_tick_codes = sorted({position.code for account in a_share_accounts for position in account_positions(db, account.id)})
            missing_codes = [code for code in post_tick_codes if code not in quote_map]
            if missing_codes and A_SHARE not in deferred_markets and A_SHARE not in calendar_unavailable_markets:
                try:
                    quote_map.update(fetch_quote_batch(missing_codes))
                except MarketDataUnavailableError as exc:
                    deferred_markets.add(A_SHARE)
                    logger.warning("A-share post-observation quote refresh deferred: %s", exc)
            # A fund observation may have created its first position after the
            # initial cross-market quote batch. Fetch those NAVs before the
            # valuation loop, keeping the source/evidence checks identical.
            for observation_market in (CN_FUND, US_EQUITY):
                market_accounts = [account for account in accounts if normalize_market(account.market) == observation_market]
                post_codes = sorted({position.code for account in market_accounts for position in account_positions(db, account.id)})
                if not post_codes or observation_market in deferred_markets:
                    continue
                existing_quotes = cross_quote_maps.setdefault(observation_market, {})
                missing = [code for code in post_codes if code not in existing_quotes]
                if not missing:
                    continue
                try:
                    existing_quotes.update(self._fetch_market_quotes(db, market=observation_market, requested_codes=missing, day=day))
                except MarketDataUnavailableError as exc:
                    deferred_markets.add(observation_market)
                    logger.warning("%s post-observation quote refresh deferred: %s", observation_market, exc)
            valuation_count = 0
            for account in accounts:
                account_market = normalize_market(account.market)
                if session_statuses.get(account_market) == "calendar_unavailable":
                    deferred_markets.add("calendar_unavailable:" + account_market)
                    continue
                if session_statuses.get(account_market) != "open":
                    deferred_markets.add(account_market)
                    continue
                if account_market != A_SHARE:
                    account_positions_rows = account_positions(db, account.id)
                    if account_positions_rows:
                        account_codes = {position.code for position in account_positions_rows}
                        quotes_for_market = cross_quote_maps.get(account_market, {})
                        if any(code not in quotes_for_market for code in account_codes):
                            deferred_markets.add(account_market)
                            continue
                        market_quotes = [quotes_for_market[code] for code in account_codes]
                        prices = {code: float(quotes_for_market[code].price) for code in account_codes}
                        quote_times = [quote.as_of for quote in market_quotes]
                        sources = {quote.source for quote in market_quotes}
                        source = next(iter(sources)) if len(sources) == 1 else "scheduler:mixed"
                        freshness_values = {str(quote.freshness or "").lower() for quote in market_quotes}
                        freshness = next(iter(freshness_values)) if len(freshness_values) == 1 else "mixed"
                        quote_metadata = {
                            code: {
                                "source": quotes_for_market[code].source,
                                "as_of": quotes_for_market[code].as_of,
                                "freshness": quotes_for_market[code].freshness,
                            }
                            for code in account_codes
                        }
                        valuation = mark_to_market(
                            db, account_id=account.id, prices=prices, valuation_date=day,
                            price_source=source, price_as_of=_min_quote_as_of(quote_times),
                            price_freshness=freshness, price_metadata=quote_metadata,
                        )
                        build_daily_report(db, account_id=account.id, report_date=day)
                        self._publish_valuation(account.id, valuation)
                        valuation_count += 1
                        continue
                    # A cash-only account has no quote dependency and can be
                    # checkpointed without inventing a market price.
                    valuation = mark_to_market(
                        db, account_id=account.id, prices={}, valuation_date=day,
                        price_source="scheduler:cash", price_as_of=None,
                        price_freshness="fresh",
                    )
                    build_daily_report(db, account_id=account.id, report_date=day)
                    self._publish_valuation(account.id, valuation)
                    valuation_count += 1
                    continue
                account_codes = {position.code for position in account_positions(db, account.id)}
                if any(code not in quote_map for code in account_codes):
                    deferred_markets.add(A_SHARE)
                    continue
                prices = {code: float(quote_map[code].price) for code in account_codes}
                quote_times = [quote_map[code].as_of for code in account_codes]
                sources = {quote_map[code].source for code in account_codes}
                source = next(iter(sources)) if len(sources) == 1 else "scheduler:mixed"
                if not sources:
                    source = "scheduler:cash"
                as_of = _min_quote_as_of(quote_times)
                freshness_values = {str(quote_map[code].freshness or "").lower() for code in account_codes}
                freshness = "delayed" if "delayed" in freshness_values else (next(iter(freshness_values)) if account_codes else "fresh")
                quote_metadata = {
                    code: {
                        "source": quote_map[code].source,
                        "as_of": quote_map[code].as_of,
                        "freshness": quote_map[code].freshness,
                    }
                    for code in account_codes
                }
                valuation = mark_to_market(
                    db, account_id=account.id, prices=prices, valuation_date=day,
                    price_source=source, price_as_of=as_of, price_freshness=freshness,
                    price_metadata=quote_metadata,
                )
                build_daily_report(db, account_id=account.id, report_date=day)
                self._publish_valuation(account.id, valuation)
                valuation_count += 1
            calendar_deferred = sorted(item for item in deferred_markets if item.startswith("calendar_unavailable:"))
            if calendar_deferred and valuation_count == 0:
                row.status = "blocked"
                row.reason = ";".join(calendar_deferred)
            elif deferred_markets and valuation_count == 0:
                row.status = "skipped"
                row.reason = "market_data_unavailable:" + ",".join(sorted(deferred_markets))
            else:
                row.status = "completed"
                deferred_reasons = []
                if calendar_deferred:
                    deferred_reasons.append("calendar_unavailable:" + ",".join(item.split(":", 1)[1] for item in calendar_deferred))
                adapter_deferred = sorted(item for item in deferred_markets if not item.startswith("calendar_unavailable:"))
                if adapter_deferred:
                    deferred_reasons.append("deferred_market_adapters:" + ",".join(adapter_deferred))
                row.reason = ";".join(deferred_reasons) if deferred_reasons else None
            row.valuation_count = valuation_count
            row.last_run_at = datetime.now(timezone.utc).isoformat()
            db.commit()
            return self._run_dict(row)
        except MarketDataUnavailableError as exc:
            db.rollback()
            row = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first() or PaperSchedulerRun(run_date=day.isoformat())
            row.status, row.account_count, row.valuation_count, row.reason = "skipped", db.query(PaperAccount).filter(PaperAccount.status == "active").count(), 0, f"market_data_unavailable:{exc}"
            if row not in db:
                db.add(row)
            db.commit()
            return self._run_dict(row)
        except ValueError as exc:
            if str(exc).startswith("cross_market_scheduler_date_must_be_today"):
                db.rollback()
                raise
            db.rollback()
            logger.exception("paper scheduler run failed")
            row = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first() or PaperSchedulerRun(run_date=day.isoformat())
            row.status, row.reason = "failed", f"{type(exc).__name__}:{exc}"
            if row not in db:
                db.add(row)
            db.commit()
            return self._run_dict(row)
        except Exception as exc:
            db.rollback()
            logger.exception("paper scheduler run failed")
            row = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first() or PaperSchedulerRun(run_date=day.isoformat())
            row.status, row.reason = "failed", f"{type(exc).__name__}:{exc}"
            if row not in db:
                db.add(row)
            db.commit()
            return self._run_dict(row)
        finally:
            db.close()

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    # Await the worker.  This makes application shutdown
                    # deterministic: no background thread can outlive the
                    # lifespan and write to the database after shutdown.
                    await asyncio.to_thread(self.run_once)
        finally:
            self._loop = None
            self.close()

    async def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        closed: set[int] = set()
        for provider in [self.provider, *self.market_providers.values()]:
            if provider is None or id(provider) in closed:
                continue
            closed.add(id(provider))
            close = getattr(provider, "close", None)
            if close:
                close()

    @staticmethod
    def _run_dict(row: PaperSchedulerRun) -> dict:
        return {
            "run_date": row.run_date, "status": row.status,
            "account_count": row.account_count, "valuation_count": row.valuation_count,
            "reason": row.reason, "execution_mode": "paper_only",
            "paper_only": True, "live_execution": False,
            "created_at": row.created_at, "last_run_at": row.last_run_at,
        }


def account_positions(db, account_id: str):
    from server.models.schema import PaperAccountPosition
    return db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id, PaperAccountPosition.shares > 0).all()


def scheduler_runs(limit: int = 30) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(PaperSchedulerRun).order_by(PaperSchedulerRun.run_date.desc()).limit(limit).all()
        return [PaperDailyScheduler._run_dict(row) for row in rows]
    finally:
        db.close()
