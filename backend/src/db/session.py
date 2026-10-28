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


# B6(2026-10-27) 数据归属回填:owner_user_id 为 NULL 的存量行 →
# 首个 admin 的 user_id;users 空表兜底 '__legacy__'(与 papers.task_id
# 的迁移占位同一约定,不会被新任务看到)。幂等:已有归属的行不动。
# 抽为模块级函数:init_db 启动时调用;测试可直接对临时库验证。
_OWNER_BACKFILL_TABLES = (
    "papers", "retrieval_tasks", "chinese_workflow_tasks",
    "retrieval_history", "reviews",
)

_OWNER_BACKFILL_SQL = (
    "UPDATE {table} SET owner_user_id = COALESCE("
    "  (SELECT user_id FROM users WHERE role = 'admin'"
    "    ORDER BY created_at ASC LIMIT 1),"
    "  '__legacy__')"
    " WHERE owner_user_id IS NULL"
)


def backfill_owner_columns(conn) -> None:
    """五张业务表幂等补列 owner_user_id 并回填存量行(conn 由调用方提交)。"""
    for table in _OWNER_BACKFILL_TABLES:
        cols = {
            row[1] for row in
            conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        }
        if not cols:
            continue  # 表尚未创建(create_all 会带全列)
        if "owner_user_id" not in cols:
            conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN owner_user_id VARCHAR(36)"
            )
        conn.exec_driver_sql(_OWNER_BACKFILL_SQL.format(table=table))


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


# ============================================================================
# C8(2026-10-27) init_db 轻量治理:内联 DDL 重排为模块级有序迁移列表。
# - 不引 Alembic(现有部署多为本地 SQLite 文件);
# - 每段执行前 log.info("apply migration %s"),启动日志可审计;
# - 「查列→ALTER」重复模式收敛到 _ensure_columns;
# - 行为与重排前完全一致(同段同序,仅容器变化);
# - 启动期脏数据 DELETE 是数据修复而非 schema 迁移,置于列表最后并单独注明。
# ============================================================================

from sqlalchemy.engine import Connection as _Connection  # noqa: E402


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    """幂等补列:{列名: ADD COLUMN DDL 片段}。表不存在时跳过(create_all 会建)。"""
    cols = {
        row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    }
    if not cols:
        return
    for col, ddl in columns.items():
        if col not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _mig_retrieval_tasks_events(conn: _Connection) -> None:
    """v4.1:english 检索任务的 events JSON 字段。"""
    _ensure_columns(conn, "retrieval_tasks", {
        "events": "events JSON DEFAULT '[]'",
    })
    conn.commit()


def _mig_papers_add_columns(conn: _Connection) -> None:
    """v7.0:papers 补 provenance / identity_key / task_id(task_id 须先于
    repair_database() 添加,否则其 SELECT papers.task_id 会失败)。"""
    _ensure_columns(conn, "papers", {
        "provenance": "provenance JSON",
        "identity_key": "identity_key VARCHAR(500)",
        "task_id": "task_id VARCHAR(36)",
    })
    paper_cols = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(papers)").fetchall()
    }
    if paper_cols and "task_id" not in paper_cols:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_papers_task_id ON papers (task_id)"
        )
    conn.commit()


def _mig_papers_rebuild_pk(conn: _Connection) -> None:
    """v8.1 任务隔离(彻底版):papers 主键从全局 lit_id 改为代理自增 id,
    同一文献可在不同任务各存一行,(task_id, lit_id) 唯一。
    SQLite 不支持改主键,只能整表重建:papers_new(id 主键) ← 旧表全量拷贝
    (task_id 空值统一填 __legacy__)→ DROP 旧表 → RENAME。
    注意:重建表不带 CHECK 约束 —— 脏数据清理在迁移列表末段执行,
    若新表带 CHECK,INSERT SELECT 会在脏行上失败导致启动中断。"""
    paper_cols = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(papers)").fetchall()
    }
    if not paper_cols or "id" in paper_cols:
        return
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


def _mig_papers_legacy_task_backfill(conn: _Connection) -> None:
    """v8.1:老数据回填 __legacy__ 占位(迁移期间可见,但不会被新任务看到)。"""
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


def _mig_repair_identity_index(conn: _Connection) -> None:
    """v7.0:identity_key 唯一性限定在「同一 task 内」—— 先 repair_database
    (依赖 task_id 列已存在),再换组合唯一索引。"""
    from retrieval.paper_identity import repair_database
    repair_database()
    with engine.begin() as repair_conn:
        repair_conn.exec_driver_sql(
            "DROP INDEX IF EXISTS uq_papers_identity_key"
        )
        repair_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_identity_key_per_task "
            "ON papers (identity_key, task_id) WHERE identity_key IS NOT NULL"
        )


def _mig_history_run_id(conn: _Connection) -> None:
    """v6.0:统一检索历史的 run_id(前端启动时分配的 UUID,聚合中文/英文两边)。"""
    _ensure_columns(conn, "retrieval_history", {"run_id": "run_id VARCHAR(36)"})
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_retrieval_history_run_id "
        "ON retrieval_history (run_id)"
    )
    conn.commit()


def _mig_core_journals(conn: _Connection) -> None:
    """v6.x:核心期刊清单 core_journals 表 + 去重唯一索引。"""
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


def _mig_users_auth_columns(conn: _Connection) -> None:
    """v9.x 多用户:users 表补列(注册审核 + 按用户 LLM 配置)与
    B1 auth_tokens.expires_at;存量 llm_kind 归一化为协议维度。"""
    user_cols = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
    }
    if user_cols:
        _ensure_columns(conn, "users", {
            "status": "status VARCHAR(16) DEFAULT 'approved'",
            "reviewed_at": "reviewed_at DATETIME",
            "llm_kind": "llm_kind VARCHAR(16) DEFAULT 'minimax'",
            "llm_base_url": "llm_base_url VARCHAR(500) DEFAULT ''",
            "llm_api_key": "llm_api_key TEXT DEFAULT ''",
            "llm_model": "llm_model VARCHAR(200) DEFAULT ''",
        })
        # 接口类型改为协议维度(OpenAI/Anthropic 兼容)后的存量归一化:
        # minimax 端点本就是 OpenAI 兼容,统一记 openai
        conn.exec_driver_sql(
            "UPDATE users SET llm_kind='openai' WHERE llm_kind='minimax'"
        )
        conn.commit()
    # B1 多用户令牌过期:auth_tokens 幂等补列 expires_at。
    # NULL = 存量令牌(首次校验时由 security.auth 回填 TTL,不踢存量会话)。
    _ensure_columns(conn, "auth_tokens", {"expires_at": "expires_at DATETIME"})
    conn.commit()


def _mig_owner_backfill(conn: _Connection) -> None:
    """B6 数据归属:五张业务表幂等补列 owner_user_id + 存量回填。"""
    backfill_owner_columns(conn)
    conn.commit()


def _mig_purge_dirty_papers(conn: _Connection) -> None:
    """v6.x 清理历史脏数据(title 空 / year<=0 / 双摘要都空)。

    ── 数据修复,非 schema 迁移 ──
    D0 裁决(2026-10-27,主理人定):默认跳过。历史实现因缺 commit 处于
    隐式事务回滚中,"清理"从未真正生效 —— 现状的事实行为就是"不清理";
    补 commit 使其生效反而引入启动期静默删用户数据的风险(评估 M-5 原始
    问题)。因此:默认空转(打一行 info),设 INIT_DB_PURGE=1 显式启用;
    启用时补 commit 使删除真正落库。迁移列表仍登记本段(默认空转)。
    """
    if os.environ.get("INIT_DB_PURGE") != "1":
        log.info("脏数据清理已跳过,设 INIT_DB_PURGE=1 启用")
        return
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
    # D0:启用时真正提交(历史实现缺 commit,删除被隐式回滚、日志虚报)
    conn.commit()
    if deleted_title or deleted_year or deleted_abstract:
        log.warning(
            "init_db: 清理脏数据 title=%d year=%d abstract=%d",
            deleted_title, deleted_year, deleted_abstract,
        )


# 有序迁移列表:段序即执行序(与重排前 init_db 的执行顺序一一对应)。
# C8 修正:owner/users 补列必须先于 v7.0 repair_identity_index ——
# repair_database 经 ORM 全列 SELECT papers,存量库若此时尚无
# owner_user_id 列会直接炸(重排时对真实存量库拷贝实测暴露,B6 起潜伏)。
_MIGRATIONS: list[tuple[str, "callable"]] = [
    ("v4.1_retrieval_tasks_events", _mig_retrieval_tasks_events),
    ("v7.0_papers_add_columns", _mig_papers_add_columns),
    ("v8.1_papers_rebuild_pk", _mig_papers_rebuild_pk),
    ("v8.1_papers_legacy_task_backfill", _mig_papers_legacy_task_backfill),
    ("v9.x_users_auth_columns", _mig_users_auth_columns),
    ("b6_owner_backfill", _mig_owner_backfill),
    ("v7.0_repair_identity_index", _mig_repair_identity_index),
    ("v6.0_history_run_id", _mig_history_run_id),
    ("v6.x_core_journals", _mig_core_journals),
    ("data_repair_purge_dirty_papers(数据修复,非schema)", _mig_purge_dirty_papers),
]


def init_db() -> None:
    """创建所有表 + 依序执行轻量迁移(应用启动时调用)。

    C8:内联 DDL 已重排为模块级 _MIGRATIONS 有序列表,每段执行前打
    info 日志;行为与重排前完全一致。
    """
    # 需要先 import 所有模型,确保它们注册到 Base.metadata
    from db import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # 轻量迁移:为已存在的表补加 model 中新增的列/索引。
    # SQLite 的 create_all 不会 ALTER 已存在的表,
    # 而现有部署多为本地 SQLite 文件,不便走 Alembic,
    # 因此在这里对已知的 schema 漂移做幂等补丁。
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            for version_tag, migrate in _MIGRATIONS:
                log.info("apply migration %s", version_tag)
                migrate(conn)
