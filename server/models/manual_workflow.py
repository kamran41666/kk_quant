"""Independent persistence models for the engineering workflow demo.

These rows are deliberately outside the manual production schema and are not
included by the manual backup service.
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from server.models.database import Base
from server.models.schema import manual_now_str, uuid4_str


class WorkflowRun(Base):
    __tablename__ = "workflow_run"
    __table_args__ = (
        CheckConstraint("mode = 'engineering_demo'", name="ck_workflow_run_mode"),
        CheckConstraint("revision >= 0", name="ck_workflow_run_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    mode: Mapped[str] = mapped_column(String(40), nullable=False, default="engineering_demo")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)
    updated_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


class WorkflowAction(Base):
    __tablename__ = "workflow_action"
    __table_args__ = (
        UniqueConstraint("key", name="uq_workflow_action_key"),
        CheckConstraint("from_revision >= 0 AND to_revision = from_revision + 1", name="ck_workflow_action_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    from_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    to_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=manual_now_str)


__all__ = ["WorkflowAction", "WorkflowRun"]
