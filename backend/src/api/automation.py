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

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.session import get_db

from src.automation.remote_browser import manager, FRAME_INTERVAL

log = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["automation"])

SEARCH_URLS = {
    # 知网:默认进"高级检索"页面(用户硬性要求)
    "cnki": "https://kns.cnki.net/kns8s/AdvSearch",
    "cqvip": "http://qikan.cqvip.com/Qikan/Search/Index?key={kw}",
    "wanfang": "https://s.wanfangdata.com.cn/paper?q={kw}",
    "openalex": "https://openalex.org/search?search={kw}",
    "pubmed": "https://pubmed.ncbi.nlm.nih.gov/?term={kw}",
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
    db_types = req.db_types or [req.db_type]
    # 构造每个库的 URL
    urls = []
    for db in db_types:
        urls.append(_build_url(db, req.keyword, req.keyword_en))

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
