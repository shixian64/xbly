"""SQLAlchemy 2 同步数据库基础设施。"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, get_settings


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_database_engine(settings: Settings | None = None, **overrides: Any) -> Engine:
    cfg = settings or get_settings()
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": cfg.db_pool_size,
        "max_overflow": cfg.db_max_overflow,
        "echo": cfg.sql_echo,
    }
    options.update(overrides)
    return create_engine(cfg.database_url, **options)


_engine: Engine | None = None
_session_factory = sessionmaker(autoflush=False, expire_on_commit=False, class_=Session)


def get_engine() -> Engine:
    """首次真正创建 Session/连接时才加载 psycopg。"""

    global _engine
    if _engine is None:
        _engine = create_database_engine()
    return _engine


def SessionLocal(**kwargs: Any) -> Session:
    """保持传统 ``SessionLocal()`` 调用形式，同时避免 import 阶段加载 DBAPI。"""

    kwargs.setdefault("bind", get_engine())
    return _session_factory(**kwargs)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖；调用方决定业务事务边界，异常时统一回滚。"""

    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Worker、CLI 和维护任务使用的自动提交事务。"""

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
