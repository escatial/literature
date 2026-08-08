"""数据库连接 + Session 管理。"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# SQLite 文件位置(backend/data/lit_review.db)
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DB_DIR / "lit_review.db"

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_active_connection = None


def connect_db() -> None:
    """应用启动时验证数据库连接可用。"""
    global _active_connection
    if _active_connection is None:
        _active_connection = engine.connect()


def close_db() -> None:
    """应用关闭时释放数据库连接和连接池。"""
    global _active_connection
    if _active_connection is not None:
        _active_connection.close()
        _active_connection = None
    engine.dispose()


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


def get_db() -> Session:
    """FastAPI 依赖注入:yield 一个 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """创建所有表(应用启动时调用)。"""
    # 需要先 import 所有模型,确保它们注册到 Base.metadata
    from db import models  # noqa: F401
    Base.metadata.create_all(bind=engine)