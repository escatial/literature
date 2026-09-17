"""知网入库按稳定 identity_key 去重，不受会话 URL/lit_id 变化影响。"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from automation import cnki_adapter
from db.models import PaperModel
from db.session import Base


def _record(lit_id: str, url: str, title: str = "同一篇文献") -> dict:
    return {
        "lit_id": lit_id,
        "identity_key": "cnki|title|same|author|tester|year|2024",
        "source": "cnki",
        "title": title,
        "authors": ["测试者"],
        "journal": "测试期刊",
        "year": 2024,
        "abstract": "有效摘要",
        "source_url": url,
        "selected": True,
    }


def _isolated_session(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'papers.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_test_identity_task "
            "ON papers (identity_key, task_id) WHERE identity_key IS NOT NULL"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_test_lit_task ON papers (task_id, lit_id)"
        ))
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    import db.session as db_session
    import retrieval.provenance as provenance
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(provenance, "validate_paper_provenance", lambda *_args: None)
    return factory


def test_existing_identity_with_new_lit_id_updates_instead_of_inserting(monkeypatch, tmp_path):
    factory = _isolated_session(monkeypatch, tmp_path)
    task_id = "task-1"
    assert cnki_adapter._persist_record(_record("lit_cnki_old", "https://old"), task_id)
    assert cnki_adapter._persist_record(
        _record("lit_cnki_new", "https://new", title="更新后的标题"), task_id
    )

    with factory() as db:
        rows = db.query(PaperModel).filter(PaperModel.task_id == task_id).all()
        assert len(rows) == 1
        assert rows[0].lit_id == "lit_cnki_old"
        assert rows[0].title == "更新后的标题"
        assert rows[0].source_url == "https://new"


def test_batch_dedupes_same_identity_before_commit(monkeypatch, tmp_path):
    factory = _isolated_session(monkeypatch, tmp_path)
    flags = cnki_adapter._persist_records([
        _record("lit_cnki_one", "https://one"),
        _record("lit_cnki_two", "https://two"),
    ], "task-2")

    assert flags == [True, False]
    with factory() as db:
        assert db.query(PaperModel).filter(PaperModel.task_id == "task-2").count() == 1
