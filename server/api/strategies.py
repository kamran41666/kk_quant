"""Strategy CRUD endpoints"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from server.models.database import get_db
from server.models.schema import Strategy

router = APIRouter(prefix="/strategies", tags=["strategies"])

# ---- Request/Response Models ----

class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    strategy_class: str = Field(..., min_length=1, max_length=255)
    params: dict = Field(default_factory=dict)

class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    strategy_class: Optional[str] = None
    params: Optional[dict] = None

class StrategyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    strategy_class: str
    params: dict
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True

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
    # Parse JSON params for response
    result = []
    for s in strategies:
        d = StrategyResponse(
            id=s.id, name=s.name, description=s.description,
            strategy_class=s.strategy_class,
            params=json.loads(s.params) if s.params else {},
            created_at=s.created_at, updated_at=s.updated_at,
        )
        result.append(d)
    return result


@router.post("", response_model=StrategyResponse, status_code=201)
def create_strategy(
    data: StrategyCreate,
    db: Session = Depends(get_db),
):
    """Create a new strategy definition"""
    now = datetime.now().isoformat()
    strategy = Strategy(
        name=data.name,
        description=data.description,
        strategy_class=data.strategy_class,
        params=json.dumps(data.params),
        created_at=now,
        updated_at=now,
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    return StrategyResponse(
        id=strategy.id, name=strategy.name, description=strategy.description,
        strategy_class=strategy.strategy_class,
        params=json.loads(strategy.params) if strategy.params else {},
        created_at=strategy.created_at, updated_at=strategy.updated_at,
    )


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
):
    """Get strategy by ID"""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return StrategyResponse(
        id=strategy.id, name=strategy.name, description=strategy.description,
        strategy_class=strategy.strategy_class,
        params=json.loads(strategy.params) if strategy.params else {},
        created_at=strategy.created_at, updated_at=strategy.updated_at,
    )


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
    if 'params' in update_data:
        update_data['params'] = json.dumps(update_data['params'])

    update_data['updated_at'] = datetime.now().isoformat()

    for key, value in update_data.items():
        setattr(strategy, key, value)

    db.commit()
    db.refresh(strategy)

    return StrategyResponse(
        id=strategy.id, name=strategy.name, description=strategy.description,
        strategy_class=strategy.strategy_class,
        params=json.loads(strategy.params) if strategy.params else {},
        created_at=strategy.created_at, updated_at=strategy.updated_at,
    )


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
