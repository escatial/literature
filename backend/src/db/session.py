"""数据库连接 + Session 管理。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)

# SQLite 文件位置(backend/data/lit_review.db)
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DB_DIR / "lit_review.db"

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """每个新 SQLite 连接建立时执行 PRAGMA(根治并发读写锁冲突)。

    - journal_mode=WAL: 读写不再互斥,前端轮询读不会顶掉写提交
    - synchronous=NORMAL: WAL 下的推荐档位,兼顾安全与性能
    - busy_timeout=5000: 写锁被占用时最多等 5 秒而不是立刻报 database is locked
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _make_engine(url: str) -> Engine:
    """统一的 engine 工厂:SQLite 时挂载 PRAGMA 钩子。"""
    is_sqlite = url.startswith("sqlite")
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
        echo=False,
    )
    if is_sqlite:
        event.listen(eng, "connect", _sqlite_pragmas)
    return eng


engine: Engine = _make_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_active_connection = None


def reset_engine(database_url: str) -> Engine:
    """测试用:重新绑定 engine 到新 URL,确保每个测试文件隔离数据库。

    必须先 engine.dispose() 关闭旧连接池,然后重建 engine。
    """
    global engine, SessionLocal, DATABASE_URL
    engine.dispose()
    DATABASE_URL = database_url
    engine = _make_engine(database_url)
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
            if "identity_key" not in paper_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE papers ADD COLUMN identity_key VARCHAR(500)"
                )
                conn.commit()
            # v7.0 任务隔离:task_id 列必须先于 repair_database() 添加,
            # 否则 repair_database() 内 SELECT papers.task_id 会失败(老库无此列)。
            if "task_id" not in paper_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE papers ADD COLUMN task_id VARCHAR(36)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_papers_task_id ON papers (task_id)"
                )
                conn.commit()
            # v8.1 任务隔离(彻底版):papers 主键从全局 lit_id 改为代理自增 id,
            # 同一文献可在不同任务各存一行,(task_id, lit_id) 唯一。
            # SQLite 不支持改主键,只能整表重建:papers_new(id 主键) ← 旧表全量拷贝
            # (task_id 空值统一填 __legacy__)→ DROP 旧表 → RENAME。
            # 注意:重建表不带 CHECK 约束 —— 脏数据清理在本函数后段执行,
            # 若新表带 CHECK,INSERT SELECT 会在脏行上失败导致启动中断。
            if "id" not in paper_cols:
                conn.exec_driver_sql(
                    "CREATE TABLE papers_new ("
                    "  id INTEGER NOT NULL,"
                    "  lit_id VARCHAR(32) NOT NULL,"
                    "  identity_key VARCHAR(500),"
                    "  source VARCHAR(32) NOT NULL,"
                    "  title VARCHAR(500) NOT NULL,"
                    "  authors JSON,"
                    "  journal VARCHAR(200),"
                    "  year INTEGER,"
                    "  volume VARCHAR(50),"
                    "  issue VARCHAR(50),"
                    "  pages VARCHAR(50),"
                    "  abstract TEXT,"
                    "  doi VARCHAR(200),"
                    "  source_url VARCHAR(500),"
                    "  cited_by_count INTEGER,"
                    "  journal_level VARCHAR(50),"
                    "  relevance_score FLOAT,"
                    "  provenance JSON,"
                    "  raw_citation TEXT,"
                    "  quote_text TEXT,"
                    "  abstract_text TEXT,"
                    "  selected BOOLEAN,"
                    "  created_at DATETIME,"
                    "  task_id VARCHAR(36),"
                    "  PRIMARY KEY (id)"
                    ")"
                )
                conn.exec_driver_sql(
                    "INSERT INTO papers_new ("
                    "  lit_id, identity_key, source, title, authors, journal, year,"
                    "  volume, issue, pages, abstract, doi, source_url, cited_by_count,"
                    "  journal_level, relevance_score, provenance, raw_citation,"
                    "  quote_text, abstract_text, selected, created_at, task_id"
                    ") SELECT "
                    "  lit_id, identity_key, source, title, authors, journal, year,"
                    "  volume, issue, pages, abstract, doi, source_url, cited_by_count,"
                    "  journal_level, relevance_score, provenance, raw_citation,"
                    "  quote_text, abstract_text, selected, created_at,"
                    "  COALESCE(task_id, '__legacy__')"
                    " FROM papers"
                )
                conn.exec_driver_sql("DROP TABLE papers")
                conn.exec_driver_sql("ALTER TABLE papers_new RENAME TO papers")
                # 重建业务索引(原索引随 DROP TABLE 一并删除)
                for _ix_sql in (
                    "CREATE INDEX IF NOT EXISTS ix_papers_lit_id ON papers (lit_id)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_identity_key ON papers (identity_key)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_source ON papers (source)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_title ON papers (title)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_year ON papers (year)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_doi ON papers (doi)",
                    "CREATE INDEX IF NOT EXISTS ix_papers_task_id ON papers (task_id)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_task_lit "
                    "ON papers (task_id, lit_id)",
                ):
                    conn.exec_driver_sql(_ix_sql)
                conn.commit()
                log.warning(
                    "init_db: v8.1 papers 表重建完成(主键 lit_id → id,(task_id, lit_id) 唯一)"
                )
            # 老数据回填 __legacy__ 占位(迁移期间可见,但不会被新任务看到)
            from sqlalchemy import update as _sa_update
            from db.models import PaperModel as _PM
            legacy_updated = conn.execute(
                _sa_update(_PM).where(_PM.task_id.is_(None)).values(task_id="__legacy__")
            ).rowcount
            conn.commit()
            if legacy_updated:
                log.warning(
                    "init_db: 为 %d 条历史 paper 补 task_id='__legacy__'(迁移占位,不会被新任务看到)",
                    legacy_updated,
                )
            from retrieval.paper_identity import repair_database
            repair_database()
            with engine.begin() as repair_conn:
                # v7.0 任务隔离:identity_key 的唯一性只能在「同一 task 内」生效。
                # 跨 task 同 identity 视为不同的论文(每个 task 独立的文献池)。
                # 先删旧全局唯一索引(如果存在),再加组合唯一索引。
                repair_conn.exec_driver_sql(
                    "DROP INDEX IF EXISTS uq_papers_identity_key"
                )
                repair_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_identity_key_per_task "
                    "ON papers (identity_key, task_id) WHERE identity_key IS NOT NULL"
                )
            # v6.0:统一检索历史的 run_id(前端启动时分配的 UUID,聚合中文/英文两边)
            hist_cols = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(retrieval_history)").fetchall()
            }
            if "run_id" not in hist_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE retrieval_history ADD COLUMN run_id VARCHAR(36)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_retrieval_history_run_id "
                    "ON retrieval_history (run_id)"
                )
                conn.commit()
            # v6.x:核心期刊清单 core_journals 表 + 去重唯一索引
            core_cols = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(core_journals)").fetchall()
            }
            if "list_source" not in core_cols:
                # 表不存在(Base.metadata.create_all 已经创建,这里是为了幂等)
                conn.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS core_journals ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  list_source VARCHAR(32),"
                    "  name VARCHAR(200),"
                    "  name_key VARCHAR(200),"
                    "  created_at DATETIME"
                    ")"
                )
                conn.commit()
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_core_journals_src_key "
                "ON core_journals (list_source, name_key)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_core_journals_key "
                "ON core_journals (name_key)"
            )
            # v6.x:清理历史脏数据(title 空 / year <= 0 / abstract 与 abstract_text 都空)
            # 应用层加严校验后,这些行不应再被 screening / 写作链路使用。
            # 删除是安全操作:.runs/ 目录有 papers.jsonl 持久化备份。
            from sqlalchemy import delete as _sa_delete, or_
            from db.models import PaperModel as _PM
            deleted_title = conn.execute(
                _sa_delete(_PM).where(
                    or_(_PM.title.is_(None), _PM.title == "")
                )
            ).rowcount
            deleted_year = conn.execute(
                _sa_delete(_PM).where(_PM.year <= 0)
            ).rowcount
            deleted_abstract = conn.execute(
                _sa_delete(_PM).where(
                    or_(_PM.abstract.is_(None), _PM.abstract == ""),
                    or_(_PM.abstract_text.is_(None), _PM.abstract_text == ""),
                )
            ).rowcount
            if deleted_title or deleted_year or deleted_abstract:
                log.warning(
                    "init_db: 清理脏数据 title=%d year=%d abstract=%d",
                    deleted_title, deleted_year, deleted_abstract,
                )
