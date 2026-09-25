"""SQLite 存储（sqlmodel）。

M0 只有框架通用的三张表：accounts、runs、audit_events。
平台相关的表（conversations、messages 等）在 M1 随适配器一起加入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

from agent_accounts.core import paths

AccountStatus = Literal["active", "frozen"]
RunStatus = Literal["running", "ok", "failed", "blocked"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Account(SQLModel, table=True):
    __tablename__ = "accounts"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    handle: str | None = None  # 平台上的账号标识，登录后补全
    owner: str | None = None  # 归属人
    status: str = "active"  # AccountStatus；frozen 时一切自动化操作都会被拒绝
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    command: str
    status: str = "running"  # RunStatus
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    run_id: str | None = Field(default=None, index=True)
    platform: str = Field(index=True)
    action: str
    detail: str = "{}"  # 脱敏后的 JSON


@cache
def _engine_for(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    path.chmod(0o600)
    return engine


def engine() -> Engine:
    return _engine_for(paths.db_path())


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def get_account(platform: str) -> Account:
    """MVP 每个平台只有一个账号；不存在时自动创建。"""
    with session() as s:
        account = s.exec(select(Account).where(Account.platform == platform)).first()
        if account is None:
            account = Account(platform=platform)
            s.add(account)
            s.commit()
            s.refresh(account)
        return account


def set_account_status(platform: str, status: AccountStatus) -> Account:
    with session() as s:
        account = s.exec(select(Account).where(Account.platform == platform)).first()
        if account is None:
            account = Account(platform=platform)
        account.status = status
        account.updated_at = utcnow()
        s.add(account)
        s.commit()
        s.refresh(account)
        return account
