# -*- coding: utf-8 -*-
"""数据归属(owner)隔离与存量回填回归(Batch B6)。

- 文献池/检索历史/综述:普通用户仅见自己 owner 名下,跨用户 404/空列表
  (防存在性泄漏);admin 全量;
- 检索停止:非 admin 只能停自己名下任务,全停请求收窄为「名下全部」;
- 存量回填:owner=NULL 行 → 首个 admin;users 空表兜底 '__legacy__'。

owner 与 X-Task-Id 双层正交:task_id 是业务域隔离,owner_user_id 是安全域。
凭据全部运行时随机生成。
"""
import secrets

import pytest

import db.session as db_session
from db.models import (
    PaperModel,
    RetrievalHistoryModel,
    RetrievalTaskModel,
    ReviewModel,
    UserModel,
)
from db.session import Base

# 三个测试身份:首个注册者=admin(A),其后普通用户 B/C(经 admin 审核)
PW_A = "pw-" + secrets.token_hex(8)
PW_B = "pw-" + secrets.token_hex(8)
PW_C = "pw-" + secrets.token_hex(8)


@pytest.fixture()
def three_users(client):
    """admin A + 已审核普通用户 B、C;返回 (admin_tok, b_tok, c_tok, ids)。"""
    def _register(username, password):
        r = client.post("/api/auth/register", json={"username": username, "password": password})
        assert r.status_code == 200

    def _login(username, password):
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.json()
        return r.json()["token"]

    _register("管理员甲", PW_A)
    _register("用户乙", PW_B)
    _register("用户丙", PW_C)
    admin_tok = _login("管理员甲", PW_A)
    # admin 审核 B/C
    users = client.get(
        "/api/auth/admin/users", headers={"X-Auth-Token": admin_tok}
    ).json()["users"]
    ids = {u["username"]: u["user_id"] for u in users}
    for uid in (ids["用户乙"], ids["用户丙"]):
        client.post(
            f"/api/auth/admin/users/{uid}/approve", headers={"X-Auth-Token": admin_tok}
        )
    b_tok = _login("用户乙", PW_B)
    c_tok = _login("用户丙", PW_C)
    return {
        "admin_tok": admin_tok, "b_tok": b_tok, "c_tok": c_tok,
        "admin_id": ids["管理员甲"], "b_id": ids["用户乙"], "c_id": ids["用户丙"],
    }


def _paper_payload(title: str) -> dict:
    """构造可通过身份指纹校验的 paper(lit_id 由 build_lit_id 派生)。"""
    from retrieval.paper_identity import build_lit_id

    base = {
        "source": "user_imported", "title": title,
        "authors": ["张三"], "journal": "测试学报", "year": 2024,
        "abstract": "测试摘要", "selected": True,
    }
    return {**base, "lit_id": build_lit_id(
        source=base["source"], title=base["title"],
        authors=base["authors"], year=base["year"],
    )}


def _bulk(client, token: str, task_id: str, papers: list[dict]) -> int:
    r = client.post(
        "/api/papers/bulk", json={"papers": papers},
        headers={"X-Auth-Token": token, "X-Task-Id": task_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["inserted"]


def _list_titles(client, token: str, task_id: str) -> list[str]:
    r = client.get(
        "/api/papers", headers={"X-Auth-Token": token, "X-Task-Id": task_id},
    )
    assert r.status_code == 200
    return [it["title"] for it in r.json()["items"]]


def test_paper_owner_isolation_across_users(client, three_users):
    """B 上传 → B 可见;admin 全见;C 列表/详情/导出/改删全部不可触达。"""
    u = three_users
    task = f"task-{secrets.token_hex(6)}"
    inserted = _bulk(client, u["b_tok"], task, [
        _paper_payload("乙的文献一"),
        _paper_payload("乙的文献二"),
    ])
    assert inserted == 2

    # B 自己可见(顺序无关)
    assert set(_list_titles(client, u["b_tok"], task)) == {"乙的文献一", "乙的文献二"}
    # admin 全见(同 task 下没有任何归属过滤)
    assert len(_list_titles(client, u["admin_tok"], task)) == 2
    # C 完全不可见
    assert _list_titles(client, u["c_tok"], task) == []

    # C 读/改/删 B 的单条:一律 404(不泄漏存在性)
    with db_session.SessionLocal() as session:
        lit_id = session.query(PaperModel).filter_by(task_id=task).first().lit_id
    h = {"X-Auth-Token": u["c_tok"], "X-Task-Id": task}
    assert client.get(f"/api/papers/{lit_id}", headers=h).status_code == 404
    assert client.patch(
        f"/api/papers/{lit_id}", headers=h, json={"selected": False}
    ).status_code == 404
    assert client.delete(f"/api/papers/{lit_id}", headers=h).status_code == 404
    # C 的导出预览:全池范围 0
    r = client.post(
        "/api/papers/export-preview", headers={"X-Auth-Token": u["c_tok"], "X-Task-Id": task},
        json={"cn": {"limit": 0, "year_start": 0, "year_end": 0},
              "en": {"limit": 0, "year_start": 0, "year_end": 0},
              "cn_core_only": False, "cn_core_limit": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pool"]["cn"]["total"] == 0 and body["pool"]["en"]["total"] == 0

    # C 的清空操作清不掉 B 的行(admin/C 都清,B 的行仍在)
    client.delete("/api/papers", headers={"X-Auth-Token": u["c_tok"], "X-Task-Id": task})
    assert len(_list_titles(client, u["b_tok"], task)) == 2


def test_bulk_write_owner_guard(client, three_users):
    """M1(QA P1):bulk 写入端 owner 校验 —— 写入侧与读路径同权。

    - 空池首写:普通用户认领放行;
    - 他人既有池:普通用户 404(防存在性泄漏),且不产生任何行/更新;
    - 本人既有池:insert/update 均放行;
    - admin bypass;NULL owner 存量池只允许 admin 触达(不炸)。
    """
    u = three_users
    task_c = f"task-m1-c-{secrets.token_hex(5)}"
    task_b = f"task-m1-b-{secrets.token_hex(5)}"

    # C 首写自己的新 task:空池放行(行级 stamp 认领)
    assert _bulk(client, u["c_tok"], task_c, [_paper_payload("丙的正常文献")]) == 1

    # B 向 C 的既有 task 灌数据:404,且零写入(不污染、不泄漏存在性)
    r = client.post(
        "/api/papers/bulk",
        json={"papers": [_paper_payload("乙越权灌入")]},
        headers={"X-Auth-Token": u["b_tok"], "X-Task-Id": task_c},
    )
    assert r.status_code == 404
    assert set(_list_titles(client, u["admin_tok"], task_c)) == {"丙的正常文献"}

    # B 对 C 的池也走不到 update 路径(同一拒绝)
    r = client.post(
        "/api/papers/bulk",
        json={"papers": [_paper_payload("丙的正常文献")]},
        headers={"X-Auth-Token": u["b_tok"], "X-Task-Id": task_c},
    )
    assert r.status_code == 404

    # C 对自己的既有池:insert 与 update(同 lit_id 重复提交)均放行
    assert _bulk(client, u["c_tok"], task_c, [_paper_payload("丙的正常文献")]) == 0

    # B 自己的 task 正常首写
    assert _bulk(client, u["b_tok"], task_b, [_paper_payload("乙的正常文献")]) == 1

    # admin bypass:向任意既有池写入放行
    assert _bulk(client, u["admin_tok"], task_c, [_paper_payload("管理员补充文献")]) == 1

    # 存量 NULL owner 池:普通用户 404;admin 可写且不炸(兼容存量)
    task_legacy = f"task-m1-legacy-{secrets.token_hex(5)}"
    with db_session.SessionLocal() as session:
        session.add(PaperModel(
            lit_id=f"lit_{secrets.token_hex(8)}", source="user_imported",
            title="无主存量文献", authors=[], journal="", year=2024,
            abstract="摘要", task_id=task_legacy, owner_user_id=None,
        ))
        session.commit()
    r = client.post(
        "/api/papers/bulk",
        json={"papers": [_paper_payload("乙碰存量池")]},
        headers={"X-Auth-Token": u["b_tok"], "X-Task-Id": task_legacy},
    )
    assert r.status_code == 404
    assert _bulk(client, u["admin_tok"], task_legacy, [_paper_payload("管理员认领存量")]) == 1


def test_stop_is_owner_scoped(client, three_users):
    """B 只能停自己名下任务;全停请求收窄为名下全部;admin 全量可停。"""
    from retrieval.task_service import _CANCEL_EVENTS, stop_tasks

    u = three_users
    task_b = f"task-b-{secrets.token_hex(5)}"
    task_c = f"task-c-{secrets.token_hex(5)}"
    with db_session.SessionLocal() as session:
        session.add(RetrievalTaskModel(
            task_id=task_b, topic="乙的任务", status="running",
            owner_user_id=u["b_id"], updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        session.add(RetrievalTaskModel(
            task_id=task_c, topic="丙的任务", status="running",
            owner_user_id=u["c_id"], updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        session.commit()
    # 注册取消事件,模拟任务运行中
    with __import__("threading").Lock():
        _CANCEL_EVENTS[task_b] = __import__("threading").Event()
        _CANCEL_EVENTS[task_c] = __import__("threading").Event()

    # B 显式停 C 的任务:越界剔除 → stopped 空,事件未置位
    r = client.post(
        "/api/retrieval/stop", headers={"X-Auth-Token": u["b_tok"]},
        json={"task_ids": [task_c]},
    )
    assert r.status_code == 200 and r.json()["stopped"] == []
    assert not _CANCEL_EVENTS[task_c].is_set()

    # B 全停(不带 task_ids):收窄为名下全部 —— 只停 task_b
    r = client.post(
        "/api/retrieval/stop", headers={"X-Auth-Token": u["b_tok"]}, json={},
    )
    stopped = set(r.json()["stopped"])
    assert task_b in stopped and task_c not in stopped
    assert _CANCEL_EVENTS[task_b].is_set() and not _CANCEL_EVENTS[task_c].is_set()

    # admin 显式停 C 的任务:放行
    r = client.post(
        "/api/retrieval/stop", headers={"X-Auth-Token": u["admin_tok"]},
        json={"task_ids": [task_c]},
    )
    assert r.json()["stopped"] == [task_c]
    assert _CANCEL_EVENTS[task_c].is_set()


def test_history_owner_isolation(client, three_users):
    """检索历史:B 可见自己的;C 不可见且 replay/restore/delete 一律 404。"""
    u = three_users
    with db_session.SessionLocal() as session:
        session.add(RetrievalHistoryModel(
            topic="乙的检索历史", sources=["openalex", "pubmed"],
            total_count=3, failed_sources={}, papers_snapshot=[],
            owner_user_id=u["b_id"],
        ))
        session.commit()

    # B 可见 1 条
    r = client.get("/api/retrieval/history", headers={"X-Auth-Token": u["b_tok"]})
    assert r.status_code == 200 and len(r.json()) == 1
    hist_id = r.json()[0]["id"]
    # C 列表为空;replay/restore/delete 一律 404
    r = client.get("/api/retrieval/history", headers={"X-Auth-Token": u["c_tok"]})
    assert r.status_code == 200 and r.json() == []
    ch = {"X-Auth-Token": u["c_tok"]}
    assert client.post(f"/api/retrieval/history/{hist_id}/replay", headers=ch).status_code == 404
    assert client.post(f"/api/retrieval/history/{hist_id}/restore", headers=ch).status_code == 404
    assert client.delete(f"/api/retrieval/history/{hist_id}", headers=ch).status_code == 404
    # admin 全量可见
    r = client.get("/api/retrieval/history", headers={"X-Auth-Token": u["admin_tok"]})
    assert any(row["id"] == hist_id for row in r.json())


def test_review_owner_isolation(client, three_users):
    """综述:B 保存 → B 可见可删;C 列表/详情/导出/删除不可触达。"""
    u = three_users
    r = client.post(
        "/api/reviews", headers={"X-Auth-Token": u["b_tok"]},
        json={
            "topic": "乙的综述", "classify_mode": "theme",
            "sections": [{"key": "theme_1", "title": "一、主题", "content": "正文内容。",
                          "citations": []}],
        },
    )
    assert r.status_code == 201
    review_id = r.json()["id"]

    r = client.get("/api/reviews", headers={"X-Auth-Token": u["b_tok"]})
    assert len(r.json()) == 1
    # C 不可见
    r = client.get("/api/reviews", headers={"X-Auth-Token": u["c_tok"]})
    assert r.json() == []
    ch = {"X-Auth-Token": u["c_tok"]}
    assert client.get(f"/api/reviews/{review_id}", headers=ch).status_code == 404
    assert client.delete(f"/api/reviews/{review_id}", headers=ch).status_code == 404
    # admin 可见可读
    assert client.get(
        f"/api/reviews/{review_id}", headers={"X-Auth-Token": u["admin_tok"]}
    ).status_code == 200


def test_owner_backfill_admin_then_legacy(tmp_path):
    """存量回填:owner=NULL → 首个 admin;users 空表 → '__legacy__'。"""
    from datetime import datetime, timezone

    from db.session import backfill_owner_columns, reset_engine

    # 场景 1:有 admin → 认领
    url1 = f"sqlite:///{tmp_path / 'backfill_admin.db'}"
    reset_engine(url1)
    Base.metadata.create_all(bind=db_session.engine)
    with db_session.SessionLocal() as session:
        session.add(UserModel(
            user_id="usr_admin_backfill", username="回填管理员",
            email="backfill-admin@local.dev", password_hash="x",
            role="admin", status="approved", enabled=True,
        ))
        session.add(PaperModel(
            lit_id="lit_bf_0001", source="user_imported", title="存量文献",
            authors=[], journal="", year=2024, abstract="摘要",
        ))
        session.commit()
    with db_session.engine.connect() as conn:
        backfill_owner_columns(conn)
        conn.commit()
    with db_session.SessionLocal() as session:
        row = session.query(PaperModel).filter_by(lit_id="lit_bf_0001").one()
        assert row.owner_user_id == "usr_admin_backfill"

    # 场景 2:users 空表 → '__legacy__'
    url2 = f"sqlite:///{tmp_path / 'backfill_legacy.db'}"
    reset_engine(url2)
    Base.metadata.create_all(bind=db_session.engine)
    with db_session.SessionLocal() as session:
        session.add(RetrievalTaskModel(
            task_id=f"task-{secrets.token_hex(4)}", topic="无主任务",
            status="pending", owner_user_id=None,
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()
    with db_session.engine.connect() as conn:
        backfill_owner_columns(conn)
        conn.commit()
    with db_session.SessionLocal() as session:
        row = session.query(RetrievalTaskModel).one()
        assert row.owner_user_id == "__legacy__"
    db_session.engine.dispose()


# ---------- D0:INIT_DB_PURGE 门控(默认跳过/启用生效) ----------

# 迁移后真实形态(v8.1 整表重建 DDL,无三重 CHECK;id 保留自增主键)。
# create_all 建出的新表带 ck_paper_title_nonempty 等约束,而 purge 的目标
# 脏行(title 空 / year<=0 / 双摘要空)只可能存在于历史迁移表 —— v8.1 重建
# 时有意不带 CHECK(见 db/session._mig_papers_rebuild_pk)。注意不能用
# CREATE TABLE AS SELECT:它不复制主键,ORM 插入拿到 NULL id 后,SQLAlchemy
# 对主键为 NULL 的行静默跳过实体加载,后续 filter_by 永远查不到。
_LEGACY_PAPERS_DDL = (
    "CREATE TABLE papers ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
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
    "  owner_user_id VARCHAR(36)"
    ")"
)


def _rebuild_papers_as_legacy_shape():
    """把 papers 换成迁移后真实形态(空表,测试尚无数据需要保留)。"""
    with db_session.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE papers")
        conn.exec_driver_sql(_LEGACY_PAPERS_DDL)


def _insert_dirty_and_clean_papers():
    """裸 SQL 灌 1 条历史脏行(title 空)+ ORM 灌 1 条好行,返回好行 lit_id。"""
    lit_good = f"lit_good_{secrets.token_hex(4)}"
    _rebuild_papers_as_legacy_shape()
    with db_session.SessionLocal() as session:
        session.add(PaperModel(
            lit_id=lit_good, source="user_imported", title="正常文献",
            authors=["张三"], journal="学报", year=2024, abstract="摘要",
        ))
        session.commit()
    with db_session.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO papers (lit_id, source, title, authors, journal,"
            " year, abstract, abstract_text, selected, task_id)"
            " VALUES (?, 'user_imported', '', '[]', '', 0, NULL, NULL, 1,"
            " '__legacy__')",
            (f"lit_dirty_{secrets.token_hex(4)}",),
        )
    return lit_good


def _good_row_alive(lit_good: str) -> bool:
    with db_session.SessionLocal() as session:
        return session.query(PaperModel).filter_by(lit_id=lit_good).one_or_none() is not None


def test_purge_skipped_by_default(monkeypatch, tmp_path):
    """默认环境(INIT_DB_PURGE 未设):脏行保留,行为与历史事实一致(不清理)。"""
    from db.session import backfill_owner_columns  # noqa: F401  确认同模块可用
    from db.session import _mig_purge_dirty_papers, reset_engine

    monkeypatch.delenv("INIT_DB_PURGE", raising=False)
    reset_engine(f"sqlite:///{tmp_path / 'purge_off.db'}")
    Base.metadata.create_all(bind=db_session.engine)
    lit_good = _insert_dirty_and_clean_papers()

    with db_session.engine.connect() as conn:
        _mig_purge_dirty_papers(conn)
    with db_session.SessionLocal() as session:
        dirty_alive = session.query(PaperModel).filter(
            (PaperModel.title == "") | (PaperModel.title.is_(None))
        ).count()
    assert dirty_alive == 1, "默认必须跳过清理(脏行保留)"
    assert _good_row_alive(lit_good)


def test_purge_enabled_removes_dirty_rows_only(monkeypatch, tmp_path):
    """INIT_DB_PURGE=1:脏行被真正清除(补 commit 生效),好行无恙。"""
    from db.session import _mig_purge_dirty_papers, reset_engine

    monkeypatch.setenv("INIT_DB_PURGE", "1")
    reset_engine(f"sqlite:///{tmp_path / 'purge_on.db'}")
    Base.metadata.create_all(bind=db_session.engine)
    lit_good = _insert_dirty_and_clean_papers()

    with db_session.engine.connect() as conn:
        _mig_purge_dirty_papers(conn)
    with db_session.SessionLocal() as session:
        dirty_alive = session.query(PaperModel).filter(
            (PaperModel.title == "") | (PaperModel.title.is_(None))
        ).count()
    assert dirty_alive == 0, "启用后脏行必须真正落库删除(含 commit)"
    assert _good_row_alive(lit_good)
