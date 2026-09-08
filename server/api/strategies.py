"""Strategy CRUD endpoints"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional

from server.models.database import get_db
from server.models.schema import Strategy
from server.services.paper_market_rules import normalize_market
from quant_engine.backtest.protocol import STRATEGY_PROTOCOL, StrategyProtocolError
from quant_engine.backtest.registry import RegisteredStrategy, strategy_registry

router = APIRouter(prefix="/strategies", tags=["strategies"])

# ---- Request/Response Models ----

class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    strategy_class: str = Field(..., min_length=1, max_length=255)
    params: dict = Field(default_factory=dict)
    market: str = Field(default="a-share", pattern=r"^(a-share|cn-fund|us-equity)$")

    @field_validator("strategy_class")
    @classmethod
    def local_protocol_class(cls, value: str) -> str:
        if not value.startswith("strategies.") or "." not in value[len("strategies."):]:
            raise ValueError("strategy_class must use the local strategies.<module>.<ClassName> protocol")
        return value

class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    strategy_class: Optional[str] = None
    params: Optional[dict] = None
    market: Optional[str] = Field(default=None, pattern=r"^(a-share|cn-fund|us-equity)$")

    @field_validator("strategy_class")
    @classmethod
    def local_protocol_class(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (not value.startswith("strategies.") or "." not in value[len("strategies."):]):
            raise ValueError("strategy_class must use the local strategies.<module>.<ClassName> protocol")
        return value

class StrategyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    strategy_class: str
    params: dict
    market: str = "a-share"
    protocol_version: Optional[str] = None
    spec_id: Optional[str] = None
    spec_version: Optional[str] = None
    spec: Optional[dict] = None
    protocol_compatible: bool = False
    compatibility_error: Optional[str] = None
    resolved_data_requirements: list[dict] = Field(default_factory=list)
    created_at: str
    updated_at: str

def _load_registered(implementation: str) -> RegisteredStrategy | None:
    try:
        return strategy_registry.load(implementation)
    except StrategyProtocolError:
        return None


def _strategy_response(strategy: Strategy) -> StrategyResponse:
    registered = _load_registered(strategy.strategy_class)
    spec = registered.spec if registered is not None else None
    compatibility_error = None
    params: dict = {}
    market = str(getattr(strategy, "market", "a-share") or "a-share").strip().lower()
    try:
        raw_params = json.loads(strategy.params) if strategy.params else {}
        if not isinstance(raw_params, dict):
            raise StrategyProtocolError("stored strategy parameters must be a JSON object")
        params = raw_params
        if spec is None:
            raise StrategyProtocolError("strategy implementation is not registered for protocol v2")
        market = normalize_market(market)
        if market not in spec.markets:
            raise StrategyProtocolError(f"strategy does not support stored market {market}")
        normalized_params = spec.validate_params(params)
    except (StrategyProtocolError, TypeError, ValueError) as exc:
        compatibility_error = str(exc)
        normalized_params = {}
    resolved_data_requirements = []
    if spec is not None and compatibility_error is None:
        resolved_data_requirements = [
            {
                **requirement.as_dict(),
                "required_bars": max(
                    spec.warmup_bars,
                    requirement.resolved_lookback(normalized_params),
                ),
            }
            for requirement in spec.data
        ]
    return StrategyResponse(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        strategy_class=strategy.strategy_class,
        params=params,
        market=market,
        protocol_version=spec.protocol_version if spec else None,
        spec_id=spec.id if spec else None,
        spec_version=spec.version if spec else None,
        spec=registered.as_dict() if registered is not None else None,
        protocol_compatible=compatibility_error is None,
        compatibility_error=compatibility_error,
        resolved_data_requirements=resolved_data_requirements,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
    )


def _validated_definition(
    implementation: str,
    market: str,
    params: dict,
) -> tuple[RegisteredStrategy, str, dict]:
    try:
        registered = strategy_registry.load(implementation)
        normalized_market = normalize_market(market)
        if normalized_market not in registered.spec.markets:
            supported = ", ".join(registered.spec.markets)
            raise StrategyProtocolError(
                f"strategy {registered.spec.id} does not support market {normalized_market}; "
                f"supported: {supported}"
            )
        normalized_params = registered.spec.validate_params(params)
    except StrategyProtocolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return registered, normalized_market, normalized_params


# ---- Endpoints ----

@router.get("", response_model=list[StrategyResponse])
def list_strategies(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List all strategies, optionally filtered by name/code search"""
    query = db.query(Strategy)
    if search:
        query = query.filter(
            (Strategy.name.ilike(f"%{search}%")) |
            (Strategy.strategy_class.ilike(f"%{search}%"))
        )
    strategies = query.order_by(Strategy.updated_at.desc()).all()
    return [_strategy_response(strategy) for strategy in strategies]


@router.get("/protocol")
def get_strategy_protocol():
    """Return the versioned strategy contract used by backtest and paper flows."""
    return STRATEGY_PROTOCOL


@router.get("/catalog")
def get_strategy_catalog():
    """Return auto-discovered strategy Specs for API and form generation."""
    try:
        entries = strategy_registry.discover()
    except StrategyProtocolError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "protocol_version": STRATEGY_PROTOCOL["version"],
        "strategies": [entry.as_dict() for entry in entries],
    }


@router.post("", response_model=StrategyResponse, status_code=201)
def create_strategy(
    data: StrategyCreate,
    db: Session = Depends(get_db),
):
    """Create a new strategy definition"""
    _, market, params = _validated_definition(
        data.strategy_class, data.market, data.params,
    )
    now = datetime.now().isoformat()
    strategy = Strategy(
        name=data.name,
        description=data.description,
        strategy_class=data.strategy_class,
        params=json.dumps(params, ensure_ascii=False, sort_keys=True),
        market=market,
        created_at=now,
        updated_at=now,
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    return _strategy_response(strategy)


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
):
    """Get strategy by ID"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return _strategy_response(strategy)


@router.put("/{strategy_id}", response_model=StrategyResponse)
def update_strategy(
    strategy_id: str,
    data: StrategyUpdate,
    db: Session = Depends(get_db),
):
    """Update an existing strategy (partial update)"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    update_data = data.model_dump(exclude_unset=True)
    implementation = update_data.get("strategy_class", strategy.strategy_class)
    market = update_data.get("market", getattr(strategy, "market", "a-share"))
    params = update_data.get(
        "params", json.loads(strategy.params) if strategy.params else {},
    )
    _, market, params = _validated_definition(implementation, market, params)
    update_data["strategy_class"] = implementation
    update_data["market"] = market
    update_data["params"] = json.dumps(params, ensure_ascii=False, sort_keys=True)

    update_data['updated_at'] = datetime.now().isoformat()

    for key, value in update_data.items():
        setattr(strategy, key, value)

    db.commit()
    db.refresh(strategy)

    return _strategy_response(strategy)


@router.delete("/{strategy_id}", status_code=204)
def delete_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
):
    """Delete a strategy"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    db.delete(strategy)
    db.commit()
