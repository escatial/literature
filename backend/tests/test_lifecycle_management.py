"""应用与检索任务生命周期测试。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from main import _lifespan
from src.api import automation as automation_api
from src.db import session as db_session


@pytest.mark.asyncio
async def test_lifespan_connects_and_closes_database_and_browser(monkeypatch):
    init_db = Mock()
    connect_db = Mock()
    dispose_db = Mock()
    shutdown_browser = AsyncMock()
    monkeypatch.setattr("main.init_db", init_db)
    monkeypatch.setattr("main.connect_db", connect_db)
    monkeypatch.setattr("main.close_db", dispose_db)
    monkeypatch.setattr(automation_api.manager, "shutdown", shutdown_browser)

    async with _lifespan(SimpleNamespace()):
        init_db.assert_called_once_with()
        connect_db.assert_called_once_with()
        shutdown_browser.assert_not_awaited()

    shutdown_browser.assert_awaited_once_with()
    dispose_db.assert_called_once_with()


@pytest.mark.asyncio
async def test_completed_workflow_is_not_reloaded_as_active_task():
    service = automation_api.ChineseRetrievalWorkflowService(
        SimpleNamespace(),
        AsyncMock(return_value={"inserted": 0, "updated": 0}),
    )
    task = automation_api.ChineseRetrievalWorkflowTask(
        task_id="done-task", status="succeeded", progress=100
    )
    service.tasks[task.task_id] = task

    active = service.get_active_tasks()

    assert active == []
    assert service.tasks[task.task_id] is task


def test_active_workflow_endpoint_excludes_completed_tasks(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    running = automation_api.ChineseRetrievalWorkflowTask(
        task_id="running-task", status="running", progress=35
    )
    completed = automation_api.ChineseRetrievalWorkflowTask(
        task_id="done-task", status="succeeded", progress=100
    )
    monkeypatch.setattr(
        automation_api.chinese_workflow_service,
        "tasks",
        {running.task_id: running, completed.task_id: completed},
    )

    response = TestClient(app).get("/api/automation/workflow/chinese/active")

    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == ["running-task"]


def test_database_lifecycle_connects_and_disposes_engine(monkeypatch):
    connect = Mock()
    connection = SimpleNamespace(close=Mock())
    connect.return_value = connection
    dispose = Mock()
    monkeypatch.setattr(db_session.engine, "connect", connect)
    monkeypatch.setattr(db_session.engine, "dispose", dispose)

    db_session.connect_db()
    db_session.close_db()

    connect.assert_called_once_with()
    connection.close.assert_called_once_with()
    dispose.assert_called_once_with()
