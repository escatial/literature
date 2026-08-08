"""统一检索自动工作流测试。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import automation as automation_api


@pytest.mark.asyncio
async def test_browser_session_rejects_english_databases(monkeypatch):
    session = SimpleNamespace(
        session_id="sid-1",
        verification=False,
        vtype="none",
        targets=["cn-url"],
        db_types=["cnki"],
    )
    create_session = AsyncMock(return_value=session)
    monkeypatch.setattr(automation_api.manager, "create_session", create_session)

    request = automation_api.SessionCreateRequest(
        keyword="中文检索式",
        keyword_en="english query",
        db_types=["cnki", "openalex", "pubmed"],
    )
    response = await automation_api.create_session(request)

    assert response.db_types == ["cnki"]
    assert create_session.await_args.kwargs["db_types"] == ["cnki"]


@pytest.mark.asyncio
async def test_chinese_workflow_fills_extracts_and_imports_automatically():
    session = SimpleNamespace(
        session_id="sid-2",
        verification=False,
        vtype="none",
        current_index=0,
        current_db="cnki",
    )
    browser_manager = SimpleNamespace(
        create_session=AsyncMock(return_value=session),
        fill_query_into_search_box=AsyncMock(return_value={"ok": True}),
        auto_extract=AsyncMock(return_value={
            "items": [{"lit_id": "cn-1", "title": "中文论文"}],
            "count": 1,
            "pages": 1,
            "stopped_reason": "no_next",
            "current_db": "cnki",
        }),
        switch_target=AsyncMock(return_value={"ok": True}),
        refresh_verification=AsyncMock(),
    )
    importer = AsyncMock(return_value={"inserted": 1, "updated": 0})
    service = automation_api.ChineseRetrievalWorkflowService(browser_manager, importer)

    task = await service.start(
        query="(人工智能)*(教育)",
        db_types=["cnki"],
        targets=["https://example.cn"],
        target_per_db=10,
        max_pages_per_db=3,
        run_inline=True,
    )

    browser_manager.create_session.assert_awaited_once()
    browser_manager.fill_query_into_search_box.assert_awaited_once_with(
        "sid-2",
        "(人工智能)*(教育)",
        submit=True,
        use_advanced=True,
        restrict_to_journals=True,
    )
    browser_manager.auto_extract.assert_awaited_once()
    importer.assert_awaited_once_with(
        [{"lit_id": "cn-1", "title": "中文论文"}], "cnki"
    )
    assert task.status == "succeeded"
    assert task.progress == 100


@pytest.mark.asyncio
async def test_chinese_workflow_waits_for_verification_then_resumes():
    session = SimpleNamespace(
        session_id="sid-3",
        verification=False,
        vtype="none",
        current_index=0,
        current_db="wanfang",
    )
    extraction_results = [
        {
            "items": [],
            "count": 0,
            "pages": 1,
            "stopped_reason": "verification",
            "current_db": "wanfang",
        },
        {
            "items": [{"lit_id": "wf-1", "title": "恢复后的论文"}],
            "count": 1,
            "pages": 1,
            "stopped_reason": "no_next",
            "current_db": "wanfang",
        },
    ]

    async def refresh_verification(current_session, _db_type):
        current_session.verification = False
        current_session.vtype = "none"

    async def extract(*_args, **_kwargs):
        result = extraction_results.pop(0)
        if result["stopped_reason"] == "verification":
            session.verification = True
            session.vtype = "slider"
        return result

    browser_manager = SimpleNamespace(
        create_session=AsyncMock(return_value=session),
        fill_query_into_search_box=AsyncMock(return_value={"ok": True}),
        auto_extract=AsyncMock(side_effect=extract),
        switch_target=AsyncMock(return_value={"ok": True}),
        refresh_verification=AsyncMock(side_effect=refresh_verification),
    )
    importer = AsyncMock(return_value={"inserted": 1, "updated": 0})
    service = automation_api.ChineseRetrievalWorkflowService(
        browser_manager,
        importer,
        verification_poll_interval=0,
    )

    task = await service.start(
        query="检索式",
        db_types=["wanfang"],
        targets=["https://example.cn"],
        run_inline=True,
    )

    statuses = [event.status for event in task.events]
    assert "waiting_verification" in statuses
    assert task.status == "succeeded"
    assert browser_manager.auto_extract.await_count == 2
    importer.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_progress_is_monotonic_and_finishes_at_100():
    session = SimpleNamespace(
        session_id="sid-4",
        verification=False,
        vtype="none",
        current_index=0,
        current_db="cqvip",
    )
    browser_manager = SimpleNamespace(
        create_session=AsyncMock(return_value=session),
        fill_query_into_search_box=AsyncMock(return_value={"ok": True}),
        auto_extract=AsyncMock(return_value={
            "items": [],
            "count": 0,
            "pages": 1,
            "stopped_reason": "no_next",
            "current_db": "cqvip",
        }),
        switch_target=AsyncMock(return_value={"ok": True}),
        refresh_verification=AsyncMock(),
    )
    service = automation_api.ChineseRetrievalWorkflowService(
        browser_manager,
        AsyncMock(return_value={"inserted": 0, "updated": 0}),
    )

    task = await service.start(
        query="检索式",
        db_types=["cqvip"],
        targets=["https://example.cn"],
        run_inline=True,
    )

    progresses = [event.progress for event in task.events]
    assert progresses == sorted(progresses)
    assert progresses[-1] == 100
    assert {event.stage for event in task.events} >= {
        "creating_session", "filling_query", "extracting", "importing", "completed"
    }
