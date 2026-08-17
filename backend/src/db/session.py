"""数据库连接 + Session 管理。"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# SQLite 文件位置(backend/data/lit_review.db)
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DB_DIR / "lit_review.db"

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine: Engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_active_connection = None


def reset_engine(database_url: str) -> Engine:
    """测试用:重新绑定 engine 到新 URL,确保每个测试文件隔离数据库。

    必须先 engine.dispose() 关闭旧连接池,然后重建 engine。
    """
    global engine, SessionLocal, DATABASE_URL
    engine.dispose()
    DATABASE_URL = database_url
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        echo=False,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # 更新 get_db 的 closure:重新指向新 SessionLocal
    globals()["get_db"] = _make_get_db()
    return engine


def _make_get_db():
    """生成新的 get_db 闭包,引用最新 SessionLocal。"""
    from typing import Generator

    def _get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    return _get_db


def reset_pool_for_test() -> None:
    """测试用:丢弃 engine 池中的所有连接(不重建 engine)。

    用于 SQLAlchemy 缓存了连接但目标 db 文件被替换的场景。
    """
    engine.dispose()


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
    # 轻量迁移:为已存在的表补加 model 中新增的 JSON 列。
    # SQLite 的 create_all 不会 ALTER 已存在的表,
    # 而现有部署多为本地 SQLite 文件,不便走 Alembic,
    # 因此在这里对已知的 schema 漂移做幂等补丁。
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            # v4.1:english 检索任务的 events JSON 字段
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(retrieval_tasks)").fetchall()}
            if "events" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE retrieval_tasks ADD COLUMN events JSON DEFAULT '[]'"
                )
                conn.commit()
            paper_cols = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(papers)").fetchall()
            }
            if "provenance" not in paper_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE papers ADD COLUMN provenance JSON"
                )
                conn.commit()
