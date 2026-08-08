"""学术数据库自动化访问 API(远程交互式浏览器架构)。

服务器端永远无头,通过 WebSocket 把截图推给前端,
用户在前端画布上操作,事件回传后端执行。
部署到 Linux 服务器时,用户无需接触服务器本身。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import ChineseWorkflowTaskModel, PaperModel
from db.session import SessionLocal, get_db

from src.automation.remote_browser import manager, FRAME_INTERVAL

log = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["automation"])

CHINESE_DATABASES = {"cnki", "cqvip", "wanfang"}
SEARCH_URLS = {
    # 知网:默认进"高级检索"页面(用户硬性要求)
    "cnki": "https://kns.cnki.net/kns8s/AdvSearch",
    "cqvip": "http://qikan.cqvip.com/Qikan/Search/Index?key={kw}",
    "wanfang": "https://s.wanfangdata.com.cn/paper?q={kw}",
}


def _build_url(db_type: str, keyword: str, keyword_en: str | None = None) -> str:
    template = SEARCH_URLS.get(db_type)
    if not template:
        raise HTTPException(400, f"不支持的数据库类型: {db_type}")
    # 英文库(OpenAlex/PubMed)优先用 keyword_en;其他库(包括中文)用 keyword
    if db_type in {"openalex", "pubmed"}:
        kw = keyword_en or keyword
    else:
        kw = keyword
    return template.format(kw=urllib.parse.quote(kw))


class SessionCreateRequest(BaseModel):
    """创建远程浏览器会话。

    支持两种调用:
    - 单库:keyword + db_type
    - 多库:keyword + db_types=[...],keyword_en(可选,英文检索式)
    """
    keyword: str = Field(..., min_length=1)
    keyword_en: str | None = Field(None, description="英文检索式,优先用于英文库")
    db_type: str = Field("cnki", description="单库模式:cnki/cqvip/wanfang")
    db_types: list[str] | None = Field(None, description="多库模式:同时设置多个")
    sources: list[str] | None = Field(
        None,
        description="可选,每个库是否英文(影响入库 source:openalex vs user_imported)",
    )


class SessionCreateResponse(BaseModel):
    session_id: str
    ws_url: str
    verification: bool
    vtype: str
    targets: list[str] = Field(default_factory=list)
    db_types: list[str] = Field(default_factory=list)


class SessionStatusResponse(BaseModel):
    session_id: str
    url: str
    title: str
    verification: bool
    vtype: str
    targets: list[str] = Field(default_factory=list)
    db_types: list[str] = Field(default_factory=list)
    current_index: int = 0
    current_db: str | None = None


@router.post("/session", response_model=SessionCreateResponse)
async def create_session(req: SessionCreateRequest):
    """创建一个远程浏览器会话并导航到目标数据库搜索页。

    接受多库轮询:在 db_types 中传多个库名,会话默认轮询切换。
    """
    db_types = [db for db in (req.db_types or [req.db_type]) if db in CHINESE_DATABASES]
    if not db_types:
        raise HTTPException(400, "至少需要一个中文数据库: cnki/cqvip/wanfang")
    # 构造每个中文库的 URL
    urls = [_build_url(db, req.keyword) for db in db_types]

    session = await manager.create_session(targets=urls, db_types=db_types)
    return SessionCreateResponse(
        session_id=session.session_id,
        ws_url=f"/api/automation/ws/{session.session_id}",
        verification=session.verification,
        vtype=session.vtype,
        targets=session.targets,
        db_types=session.db_types,
    )


class SwitchRequest(BaseModel):
    session_id: str
    index: int = Field(0, ge=0)


@router.post("/switch")
async def switch_session_target(req: SwitchRequest):
    """手动切换到指定 URL(同一会话内,画面立即跳转)。"""
    result = await manager.switch_target(req.session_id, req.index)
    return result


class FillQueryRequest(BaseModel):
    session_id: str
    query: str = Field(..., min_length=1, description="要填到检索输入框的检索式")
    submit: bool = Field(True, description="填完后是否按回车提交")
    use_advanced: bool = Field(True, description="中文库是否进入高级检索")
    restrict_to_journals: bool = Field(True, description="中文库是否只检索期刊")


@router.post("/fill_query")
async def fill_query_into_current_box(req: FillQueryRequest):
    """把检索式填入当前库页面的检索输入框,可选择是否提交。

    中文库(cnki/cqvip/wanfang):默认会进入高级检索 + 只勾选期刊。
    """
    return await manager.fill_query_into_search_box(
        req.session_id,
        req.query,
        submit=req.submit,
        use_advanced=req.use_advanced,
        restrict_to_journals=req.restrict_to_journals,
    )


class AutoExtractRequest(BaseModel):
    session_id: str
    target: int = Field(30, ge=1, le=200, description="目标条目数")
    max_pages: int = Field(10, ge=1, le=30)
    db_type: str | None = Field(None, description="默认使用会话当前库")


class AutoExtractResponse(BaseModel):
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    pages: int = 0
    stopped_reason: str
    current_db: str
    items_by_page: list[int] = Field(default_factory=list, description="每页实际抽取条数")


@router.post("/auto_extract", response_model=AutoExtractResponse)
async def auto_extract(req: AutoExtractRequest):
    """自动循环翻页抽取当前库的文献条目。

    用户会话保持在线,浏览器会跟着翻页;前端只需轮询当前会话即可看到。
    """
    session = manager.get(req.session_id)
    if not session:
        raise HTTPException(404, f"session {req.session_id} not found")

    async def _on_progress(pages, count, _url):
        # 通过 WS 通道推一帧(让前端感觉到浏览器在动)
        # 注意:该函数在主线程被调用,推送是 fire-and-forget
        pass

    result = await manager.auto_extract(
        session,
        target=req.target,
        max_pages=req.max_pages,
        db_type=req.db_type,
        on_progress=_on_progress,
    )
    return AutoExtractResponse(
        items=result["items"],
        count=result["count"],
        pages=result["pages"],
        stopped_reason=result["stopped_reason"],
        current_db=result["current_db"],
    )


@dataclass
class ChineseRetrievalWorkflowEvent:
    stage: str
    status: str
    progress: int
    detail: str | None = None


@dataclass
class ChineseRetrievalWorkflowTask:
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "pending"
    progress: int = 0
    events: list[ChineseRetrievalWorkflowEvent] = field(default_factory=list)
    error: str | None = None
    session_id: str | None = None
    items: list[dict] = field(default_factory=list)


class ChineseRetrievalWorkflowService:
    """中文数据库统一检索、抽取并自动入库的异步工作流。"""

    def __init__(self, browser_manager, importer=None, verification_poll_interval: float = 2.0):
        self.browser_manager = browser_manager
        self.importer = importer or self._empty_importer
        self.verification_poll_interval = verification_poll_interval
        self.tasks: dict[str, ChineseRetrievalWorkflowTask] = {}

    def _persist(self, task: ChineseRetrievalWorkflowTask, query: str = "") -> None:
        """把任务当前状态落库,后端重启后可恢复。"""
        try:
            with SessionLocal() as db:
                row = db.get(ChineseWorkflowTaskModel, task.task_id)
                if row is None:
                    row = ChineseWorkflowTaskModel(task_id=task.task_id, query=query)
                    db.add(row)
                if query:
                    row.query = query
                row.status = task.status
                row.progress = task.progress
                row.session_id = task.session_id or ""
                row.events = [e.__dict__ for e in task.events]
                row.items = task.items
                row.error = task.error
                row.updated_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            log.warning("中文任务持久化失败: %s", e)

    def get_active_tasks(self) -> list[ChineseRetrievalWorkflowTask]:
        """返回仍在执行的任务:内存优先,内存没有(后端重启过)则从 DB 恢复。"""
        active_status = {"pending", "running", "waiting_verification"}
        result: dict[str, ChineseRetrievalWorkflowTask] = {
            tid: t for tid, t in self.tasks.items() if t.status in active_status
        }
        try:
            with SessionLocal() as db:
                rows = (
                    db.query(ChineseWorkflowTaskModel)
                    .filter(ChineseWorkflowTaskModel.status.in_(active_status))
                    .all()
                )
                for row in rows:
                    if row.task_id in result:
                        continue  # 内存里已有,以内存为准
                    task = ChineseRetrievalWorkflowTask(
                        task_id=row.task_id,
                        status=row.status,
                        progress=row.progress,
                        events=[ChineseRetrievalWorkflowEvent(**e) for e in (row.events or [])],
                        error=row.error,
                        session_id=row.session_id or None,
                        items=row.items or [],
                    )
                    self.tasks[task.task_id] = task
                    result[task.task_id] = task
        except Exception as e:
            log.warning("从 DB 恢复中文任务失败: %s", e)
        return list(result.values())

    @staticmethod
    async def _empty_importer(items, db_type):
        return {"inserted": 0, "updated": 0}

    async def start(
        self,
        query: str,
        db_types: list[str],
        targets: list[str],
        target_per_db: int = 15,
        max_pages_per_db: int = 5,
        run_inline: bool = True,
        **kwargs,
    ) -> ChineseRetrievalWorkflowTask:
        del kwargs
        task = ChineseRetrievalWorkflowTask()
        self.tasks[task.task_id] = task
        task._query = query  # 供 _event 落库
        self._persist(task, query)
        if run_inline:
            await self._run(task, query, db_types, targets, target_per_db, max_pages_per_db)
        else:
            asyncio.create_task(
                self._run(task, query, db_types, targets, target_per_db, max_pages_per_db)
            )
        return task

    def _event(self, task, stage: str, status: str, progress: int, detail=None):
        progress = max(task.progress, min(progress, 100))
        task.progress = progress
        task.status = status
        task.events.append(ChineseRetrievalWorkflowEvent(stage, status, progress, detail))
        self._persist(task, getattr(task, "_query", ""))

    async def _refresh_verification(self, session, db_type: str):
        refresh = getattr(self.browser_manager, "refresh_verification", None)
        if refresh is None:
            refresh = getattr(self.browser_manager, "_refresh_verification", None)
        if refresh is not None:
            await refresh(session, db_type)

    async def _run(self, task, query, db_types, targets, target_per_db, max_pages_per_db):
        db_types = [db for db in db_types if db in CHINESE_DATABASES]
        if not db_types:
            task.status = "failed"
            task.error = "至少需要一个中文数据库: cnki/cqvip/wanfang"
            self._event(task, "completed", "failed", task.progress, task.error)
            return task
        try:
            self._event(task, "creating_session", "running", 10)
            session = await self.browser_manager.create_session(targets=targets, db_types=db_types)
            task.session_id = session.session_id
            all_items = []
            for index, db_type in enumerate(db_types):
                if index:
                    await self.browser_manager.switch_target(session.session_id, index)
                self._event(task, "filling_query", "running", 20 + index * 50)
                await self.browser_manager.fill_query_into_search_box(
                    session.session_id,
                    query,
                    submit=True,
                    use_advanced=True,
                    restrict_to_journals=True,
                )
                while True:
                    self._event(task, "extracting", "running", 35 + index * 50)
                    result = await self.browser_manager.auto_extract(
                        session,
                        target=target_per_db,
                        max_pages=max_pages_per_db,
                        db_type=db_type,
                    )
                    if result.get("stopped_reason") != "verification":
                        break
                    self._event(task, "extracting", "waiting_verification", task.progress)
                    await self._refresh_verification(session, db_type)
                    login_attempted = False
                    while getattr(session, "verification", False):
                        # 登录类验证:先尝试自动填账号密码提交,验证码仍留人工
                        if (
                            getattr(session, "vtype", "") == "login"
                            and not login_attempted
                        ):
                            login_attempted = True
                            login_fn = getattr(self.browser_manager, "try_auto_login", None)
                            if login_fn is not None:
                                login_result = await login_fn(session, db_type)
                                log.info("[workflow] auto_login db=%s -> %s", db_type, login_result)
                                await self._refresh_verification(session, db_type)
                                if not getattr(session, "verification", False):
                                    break  # 登录成功,直接继续抽取
                        await asyncio.sleep(self.verification_poll_interval)
                        await self._refresh_verification(session, db_type)
                items = result.get("items", [])
                self._event(task, "importing", "running", 70 + index * 20)
                await self.importer(items, db_type)
                all_items.extend(items)
            task.items = all_items
            task.status = "succeeded"
            self._event(task, "completed", "succeeded", 100)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            self._event(task, "completed", "failed", task.progress, task.error)
        return task


async def import_chinese_workflow_items(items: list[dict], db_type: str) -> dict:
    """将中文工作流抽取结果按 lit_id 写入文献池。"""
    del db_type
    inserted = updated = skipped = 0
    model_fields = {
        column.name for column in PaperModel.__table__.columns
    } - {"lit_id", "source", "created_at"}
    with SessionLocal() as db:
        for item in items:
            lit_id = item.get("lit_id")
            if not lit_id:
                skipped += 1
                continue
            values = {key: value for key, value in item.items() if key in model_fields}
            values.setdefault("title", "")
            values["source"] = "user_imported"
            existing = db.get(PaperModel, lit_id)
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                db.add(PaperModel(lit_id=lit_id, **values))
                inserted += 1
        db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


chinese_workflow_service = ChineseRetrievalWorkflowService(
    manager,
    import_chinese_workflow_items,
)


class ChineseWorkflowRequest(BaseModel):
    query: str = Field(..., min_length=1)
    db_types: list[str] = Field(default_factory=lambda: ["cnki"])
    target_per_db: int = Field(15, ge=1, le=100)
    max_pages_per_db: int = Field(5, ge=1, le=20)


class ChineseWorkflowEventResponse(BaseModel):
    stage: str
    status: str
    progress: int
    detail: str | None = None


class ChineseWorkflowResponse(BaseModel):
    task_id: str
    status: str
    progress: int
    events: list[ChineseWorkflowEventResponse] = Field(default_factory=list)
    session_id: str | None = None
    items: list[dict] = Field(default_factory=list)
    error: str | None = None


def _workflow_response(task: ChineseRetrievalWorkflowTask) -> ChineseWorkflowResponse:
    return ChineseWorkflowResponse(
        task_id=task.task_id,
        status=task.status,
        progress=task.progress,
        events=[ChineseWorkflowEventResponse(**event.__dict__) for event in task.events],
        session_id=task.session_id,
        items=task.items,
        error=task.error,
    )


@router.post("/workflow/chinese", response_model=ChineseWorkflowResponse)
async def start_chinese_workflow(req: ChineseWorkflowRequest):
    db_types = [db_type for db_type in req.db_types if db_type in CHINESE_DATABASES]
    if not db_types:
        raise HTTPException(400, "至少需要一个中文数据库: cnki/cqvip/wanfang")
    targets = [_build_url(db_type, req.query) for db_type in db_types]
    task = await chinese_workflow_service.start(
        query=req.query,
        db_types=db_types,
        targets=targets,
        target_per_db=req.target_per_db,
        max_pages_per_db=req.max_pages_per_db,
        run_inline=False,
    )
    return _workflow_response(task)


@router.get("/workflow/chinese/active", response_model=list[ChineseWorkflowResponse])
async def get_active_chinese_workflows():
    """返回当前进程中仍在执行的中文检索任务。"""
    return [
        _workflow_response(task)
        for task in chinese_workflow_service.get_active_tasks()
    ]


@router.get("/workflow/chinese/{task_id}", response_model=ChineseWorkflowResponse)
async def get_chinese_workflow(task_id: str):
    task = chinese_workflow_service.tasks.get(task_id)
    if not task:
        # 内存没有(后端重启过)则从 DB 恢复
        with SessionLocal() as db:
            row = db.get(ChineseWorkflowTaskModel, task_id)
        if not row:
            raise HTTPException(404, f"workflow task {task_id} not found")
        task = ChineseRetrievalWorkflowTask(
            task_id=row.task_id,
            status=row.status,
            progress=row.progress,
            events=[ChineseRetrievalWorkflowEvent(**e) for e in (row.events or [])],
            error=row.error,
            session_id=row.session_id or None,
            items=row.items or [],
        )
        chinese_workflow_service.tasks[task.task_id] = task
    return _workflow_response(task)


class MultiExtractRequest(BaseModel):
    """跨多个库自动抽取,失败/验证自动跳下一个库。"""
    session_id: str
    target_per_db: int = Field(15, ge=1, le=100)
    max_pages_per_db: int = Field(5, ge=1, le=20)
    overall_target: int = Field(50, ge=1, le=300)


class MultiExtractResponse(BaseModel):
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    per_db: list[dict] = Field(default_factory=list, description="每个库的抽取明细")
    exhausted: bool = False
    stopped_reason: str = ""


@router.post("/multi_extract", response_model=MultiExtractResponse)
async def multi_extract(req: MultiExtractRequest):
    """按库顺序自动跳:每个库内部翻页抽取,触验证/无下一页则跳下一个。"""
    session = manager.get(req.session_id)
    if not session:
        raise HTTPException(404, f"session {req.session_id} not found")

    collected: list[dict] = []
    seen: set[str] = set()
    per_db: list[dict] = []
    exhausted = False
    overall_reason = "overall_target"

    while len(collected) < req.overall_target:
        # 当前库抽取
        sub = await manager.auto_extract(
            session,
            target=req.target_per_db,
            max_pages=req.max_pages_per_db,
        )
        added = 0
        for it in sub["items"]:
            title = it.get("title", "")
            if title and title not in seen:
                seen.add(title)
                collected.append(it)
                added += 1
        per_db.append({
            "db": sub["current_db"],
            "pages": sub["pages"],
            "added": added,
            "stopped_reason": sub["stopped_reason"],
        })
        if len(collected) >= req.overall_target:
            overall_reason = "overall_target"
            break
        # 跳下一个库
        nxt = await manager.advance_to_next_db(req.session_id)
        if not nxt.get("ok"):
            exhausted = True
            overall_reason = "exhausted"
            break

    return MultiExtractResponse(
        items=collected,
        count=len(collected),
        per_db=per_db,
        exhausted=exhausted,
        stopped_reason=overall_reason,
    )


@router.get("/session/{session_id}", response_model=SessionStatusResponse)
async def session_status(session_id: str):
    session = manager.get(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    return SessionStatusResponse(
        session_id=session.session_id,
        url=session.page.url,
        title=await session.page.title(),
        verification=session.verification,
        vtype=session.vtype,
        targets=session.targets,
        db_types=session.db_types,
        current_index=session.current_index,
        current_db=session.current_db,
    )


@router.delete("/session/{session_id}", status_code=204)
async def close_session(session_id: str):
    await manager.close_session(session_id)


class ExtractResponse(BaseModel):
    """浏览器当前页中可提取的候选文献条目。"""
    session_id: str
    url: str
    items: list[dict] = Field(default_factory=list)
    count: int = 0


class ImportFromBrowserRequest(BaseModel):
    """把用户勾选的条目批量入库。"""
    session_id: str
    db_type: str = "cnki"
    chosen: list[dict] = Field(default_factory=list, description="用户勾选的条目")


class ImportResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int = 0


@router.post("/extract", response_model=ExtractResponse)
async def extract_candidates(session_id: str, db_type: str = "cnki"):
    """从指定会话的浏览器当前页面抽取候选文献条目。"""
    session = manager.get(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    items = await manager.fetch_candidates(session, db_type=db_type)
    return ExtractResponse(
        session_id=session_id,
        url=session.page.url,
        items=items,
        count=len(items),
    )


@router.post("/import", response_model=ImportResponse)
async def import_from_browser(req: ImportFromBrowserRequest, db: Session = Depends(get_db)):
    """把浏览器中勾选的条目写入文献池。"""
    from db.models import PaperModel

    if not req.chosen:
        return ImportResponse(inserted=0, updated=0)
    inserted = updated = 0
    for it in req.chosen:
        lit_id = it.get("lit_id") or it.get("title", "")
        if not lit_id:
            continue
        existing = db.get(PaperModel, lit_id)
        meta = {k: v for k, v in it.items() if k not in ("lit_id", "created_at", "selected")}
        if existing:
            for k, v in meta.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(PaperModel(lit_id=lit_id, selected=it.get("selected", True), **meta))
            inserted += 1
    db.commit()
    return ImportResponse(inserted=inserted, updated=updated)


@router.websocket("/ws/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str):
    """会话 WebSocket:下行推帧,上行执行操作。"""
    await websocket.accept()
    session = manager.get(session_id)
    if not session:
        await websocket.send_json({"type": "error", "data": "session not found"})
        await websocket.close()
        return

    session.streaming = True
    stop = asyncio.Event()

    async def push_frames():
        """周期性截图推送给前端。"""
        while not stop.is_set():
            try:
                frame = await manager.capture_frame(session)
                frame["verification"] = session.verification
                frame["vtype"] = session.vtype
                await websocket.send_json(frame)
            except WebSocketDisconnect:
                break
            except Exception as e:
                log.warning("推帧失败: %s", e)
                break
            await asyncio.sleep(FRAME_INTERVAL)

    async def recv_actions():
        """接收并执行用户操作。"""
        while not stop.is_set():
            try:
                raw = await websocket.receive_text()
                action = json.loads(raw)
                await manager.execute(session, action)
                # 操作后立刻补一帧,提升交互跟手性
                frame = await manager.capture_frame(session)
                frame["verification"] = session.verification
                frame["vtype"] = session.vtype
                await websocket.send_json(frame)
            except WebSocketDisconnect:
                break
            except Exception as e:
                log.warning("执行操作失败: %s", e)
                try:
                    await websocket.send_json({"type": "error", "data": str(e)})
                except Exception:
                    break

    push_task = asyncio.create_task(push_frames())
    recv_task = asyncio.create_task(recv_actions())
    try:
        done, _ = await asyncio.wait({push_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for t in (push_task, recv_task):
            t.cancel()
        session.streaming = False


@router.get("/status")
async def automation_status():
    return {
        "available": True,
        "mode": "remote_interactive",
        "supported_databases": list(SEARCH_URLS.keys()),
        "verification_types": ["captcha", "slider", "login", "face", "sms"],
    }
