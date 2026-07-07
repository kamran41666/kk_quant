from datetime import date
import warnings
import pandas as pd
import akshare as ak

from quant_engine.data.fetcher.base import DataSource


class AKShareAdapter(DataSource):
    @property
    def source_name(self) -> str:
        return "akshare"

    def fetch_stock_list(self) -> pd.DataFrame:
        try:
            df = ak.stock_info_a_code_name()
            df = df.rename(columns={"code": "code", "name": "name"})
            df["code"] = df["code"].astype(str).str.zfill(6)
            df["exchange"] = df["code"].apply(
                lambda x: "SSE" if x.startswith(("6", "9")) else "SZSE"
            )
            df["full_code"] = df.apply(
                lambda r: f"{r['code']}.{'SH' if r['exchange'] == 'SSE' else 'SZ'}",
                axis=1
            )
            return df
        except Exception as e:
            warnings.warn(f"AKShare stock list fetch failed: {e}")
            return pd.DataFrame(columns=["code", "name", "exchange", "full_code"])

    def fetch_daily(
        self, codes: list[str], start: date, end: date
    ) -> pd.DataFrame:
        frames = []
        for code in codes:
            try:
                symbol = code.replace(".SH", "").replace(".SZ", "")
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                )
                if df is not None and len(df) > 0:
                    df["code"] = code
                    frames.append(df)
            except Exception:
                continue

        if not frames:
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True)
        result = result.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover_rate",
            "涨跌幅": "pct_change", "涨跌额": "change",
        })
        result["date"] = pd.to_datetime(result["date"])
        return result

    def fetch_index_components(
        self, index_code: str, dt: date
    ) -> pd.DataFrame:
        try:
            if index_code in ("000300", "000300.SH"):
                df = ak.index_stock_cons_weight_csindex("000300")
            elif index_code in ("000905", "000905.SH"):
                df = ak.index_stock_cons_weight_csindex("000905")
            else:
                warnings.warn(f"Unsupported index: {index_code}")
                return pd.DataFrame()
            return df.rename(columns={
                "成分券代码": "code", "成分券名称": "name", "权重": "weight",
            })
        except Exception as e:
            warnings.warn(f"AKShare index fetch failed: {e}")
            return pd.DataFrame()
