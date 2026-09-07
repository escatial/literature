"""文献池 CRUD API(需求5:服务端分页 + v7 任务隔离)。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import PaperModel
from retrieval.paper_identity import build_identity_key, repair_paper_fields, validate_paper_identity
from db.schemas import (
    BatchSelectRequest,
    BatchSelectResponse,
    ExportPoolRange,
    ExportPreviewRequest,
    ExportPreviewResponse,
    ExportPreviewStats,
    LangPoolRange,
    PaperBulkCreate,
    PaperBulkCreateResponse,
    PaperListResponse,
    PaperOut,
    PaperUpdate,
    YearDistItem,
)
from db.session import get_db

# 核心期刊匹配只在中文文献(source ∈ {user_imported, cnki})生效。
_CN_SOURCES = ("user_imported", "cnki")
# 英文来源(OpenAlex / PubMed)
_EN_SOURCES = ("openalex", "pubmed")

router = APIRouter(prefix="/papers", tags=["papers"])


# 需求5:服务端分页白名单(防止 -1/99999 之类越界)
_ALLOWED_PAGE_SIZES = (10, 20, 50, 100)
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE = 10_000  # 防止过深的 page 拖垮 SQLite


# ----------------------------------------------------------------------------
# v7.0 任务隔离:每个 task 拥有独立的文献池。
# - 前端启动一次新检索时,先调 POST /papers/new-session 拿到 task_id(36 字节 UUID),
#   后续所有 /papers 请求通过 X-Task-Id header 带回后端。
# - 后端按 task_id 严格过滤:不同 task 的 papers 互不可见。
# - 老数据 / 没有 task_id 的请求用 "__legacy__" 占位(迁移期可见,但不主动创建新数据)。
# - bulk_upsert 写入时强制打 task_id 标签,避免漏写穿透。
# ----------------------------------------------------------------------------
_HEADER_TASK_ID = "x-task-id"
_DEFAULT_TASK_ID = "__default__"


def _resolve_task_id(x_task_id: str | None) -> str:
    """解析请求头中的 task_id。空 / 缺失时回退默认(迁移期)。"""
    if x_task_id and x_task_id.strip():
        return x_task_id.strip()
    return _DEFAULT_TASK_ID


@router.get("", response_model=PaperListResponse)
def list_papers(
    source: str | None = None,
    selected_only: bool = False,
    # in_core: true = 仅命中北大/中国科学引文核心库的「中文文献」
    #          false = 中文非核心 + 全部英文
    #          all(默认) = 不过滤
    in_core: str = Query("all", pattern="^(true|false|all)$"),
    # 年份:对所有 source 生效(全语种)
    year_start: int = Query(0, ge=0, le=2100),
    year_end: int = Query(0, ge=0, le=2100),
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    page_size: int = Query(_DEFAULT_PAGE_SIZE),
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """服务端分页获取文献池。

    - page 从 1 起,page_size 必须在白名单 {10,20,50,100},默认 20。
    - 核心期刊筛选(in_core)只对中文文献(user_imported/cnki)生效。
      英文文献始终不受 in_core 影响。
    - 年份筛选(year_start/year_end)对所有语种生效;0 = 不约束。
    """
    if page_size not in _ALLOWED_PAGE_SIZES:
        page_size = _DEFAULT_PAGE_SIZE

    # 防御:in_core 对英文 source 无意义,客户端误传时静默忽略 + warning。
    # 避免「仅核心」筛选作用于英文文献造成「全部 tab 下出现一堆英文」的 UX 误会。
    sources_in_param: list[str] = []
    if source:
        sources_in_param = [s.strip() for s in source.split(",") if s.strip()]
    only_en = bool(sources_in_param) and all(
        s not in _CN_SOURCES for s in sources_in_param
    )
    if only_en and in_core != "all":
        from logging import getLogger
        getLogger(__name__).warning(
            "in_core=%s 在仅英文请求中被忽略(核心期刊仅对中文文献生效)",
            in_core,
        )
        in_core = "all"
    # v7 任务隔离:严格按 task_id 过滤,跨 task 不可见。
    task_id = _resolve_task_id(x_task_id)
    base = select(PaperModel).where(PaperModel.task_id == task_id)
    count_base = select(func.count()).select_from(PaperModel).where(PaperModel.task_id == task_id)
    if source:
        # 前端传逗号分隔多值(如 user_imported,cnki / openalex,pubmed)
        sources = [s.strip() for s in source.split(",") if s.strip()]
        if sources:
            base = base.where(PaperModel.source.in_(sources))
            count_base = count_base.where(PaperModel.source.in_(sources))
    if selected_only:
        base = base.where(PaperModel.selected.is_(True))
        count_base = count_base.where(PaperModel.selected.is_(True))
    # 年份筛选(年份=0 视为缺失值,默认不过滤)
    if year_start > 0 or year_end > 0:
        if year_start > 0:
            base = base.where(PaperModel.year >= year_start)
            count_base = count_base.where(PaperModel.year >= year_start)
        if year_end > 0:
            base = base.where(PaperModel.year <= year_end)
            count_base = count_base.where(PaperModel.year <= year_end)
    # 统计/翻页前先拉到 paper 列(只有 in_core 真过滤时需要回查内存里的核心期刊集合)
    total = db.execute(count_base).scalar_one()
    if in_core == "all":
        items = db.execute(
            base.order_by(PaperModel.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars().all()
        total_pages = (total + page_size - 1) // page_size if page_size else 1
        return PaperListResponse(
            items=list(items),
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages or 1,
        )
    # in_core ∈ {true, false}:拿到 SQL 过滤后的 rows,
    # 再用内存核心期刊集合二次过滤中文条目(英文文献不受核心期刊影响,直接保留)。
    rows = db.execute(
        base.order_by(PaperModel.created_at.desc())
    ).scalars().all()
    import core_journals as _cj
    keep: list[PaperModel] = []
    want = in_core == "true"
    for r in rows:
        if r.source not in _CN_SOURCES:
            # 英文不受 in_core 影响,直接保留
            keep.append(r)
            continue
        is_core = bool(_cj.classify_journal(r.journal or "")["in_core"])
        if (want and is_core) or (not want and not is_core):
            keep.append(r)
    total_filtered = len(keep)
    page_items = keep[(page - 1) * page_size: page * page_size]
    total_pages = (total_filtered + page_size - 1) // page_size if page_size else 1
    return PaperListResponse(
        items=page_items,
        total=total_filtered,
        page=page,
        page_size=page_size,
        total_pages=total_pages or 1,
    )


@router.get("/{lit_id}", response_model=PaperOut)
def get_paper(
    lit_id: str,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """读取单条:仅当前 task 可见。"""
    task_id = _resolve_task_id(x_task_id)
    p = db.query(PaperModel).filter(
        PaperModel.lit_id == lit_id,
        PaperModel.task_id == task_id,
    ).first()
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    return p


@router.post("/bulk", response_model=PaperBulkCreateResponse)
def bulk_upsert(
    req: PaperBulkCreate,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """批量插入/更新(以 lit_id 为唯一键)。

    - 已有记录:更新可变字段,但保留 created_at(由首次插入时间决定)
    - 新记录:按 payload 插入,created_at 由数据库 default 填充
    - v7 任务隔离:每条 paper 强制打 task_id 标签,缺 X-Task-Id 默认写到 "__default__"
    """
    task_id = _resolve_task_id(x_task_id)
    inserted = updated = skipped = 0
    # 排除 lit_id(主键)、created_at(只读时间戳)、task_id(由 header 注入,禁止客户端覆盖)
    _EXCLUDE = {"lit_id", "created_at", "task_id"}
    for p in req.papers:
        payload = repair_paper_fields(p.model_dump(exclude=_EXCLUDE))
        payload["identity_key"] = build_identity_key(
            source=p.source, title=p.title, authors=p.authors,
            year=payload.get("year"), doi=p.doi,
        )
        candidate = {**payload, "lit_id": p.lit_id}
        try:
            validate_paper_identity(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # 严格隔离:按 (identity_key + task_id) 判重,跨 task 同 identity 视为新数据。
        existing = db.query(PaperModel).filter(
            PaperModel.identity_key == payload["identity_key"],
            PaperModel.task_id == task_id,
        ).first()
        if existing:
            # 已存在(同 task 内):更新关键字段(不要覆盖 created_at)
            for k, v in payload.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(PaperModel(lit_id=p.lit_id, task_id=task_id, **payload))
            inserted += 1
    db.commit()
    return PaperBulkCreateResponse(inserted=inserted, updated=updated, skipped=skipped)


@router.patch("/{lit_id}", response_model=PaperOut)
def update_paper(
    lit_id: str,
    req: PaperUpdate,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """更新单条(主要改 selected)。仅当前 task 可见。"""
    task_id = _resolve_task_id(x_task_id)
    p = db.query(PaperModel).filter(
        PaperModel.lit_id == lit_id,
        PaperModel.task_id == task_id,
    ).first()
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{lit_id}", status_code=204)
def delete_paper(
    lit_id: str,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    task_id = _resolve_task_id(x_task_id)
    p = db.query(PaperModel).filter(
        PaperModel.lit_id == lit_id,
        PaperModel.task_id == task_id,
    ).first()
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    db.delete(p)
    db.commit()


@router.delete("", status_code=204)
def clear_papers(
    source: str | None = None,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """清空当前 task 的文献池。传 source 时只清该来源(如 user_imported=中文, openalex=英文)。

    v7 任务隔离:不传 task_id 时只清当前 task,不污染其他 task。
    """
    task_id = _resolve_task_id(x_task_id)
    q = db.query(PaperModel).filter(PaperModel.task_id == task_id)
    if source:
        q = q.filter(PaperModel.source == source)
    q.delete(synchronize_session=False)
    db.commit()


# ----------------------------------------------------------------------------
# 写作导入筛选(去写作):中英文独立的数量/年份筛选 + 统计预览 + 批量勾选。
# 默认(全部条件不限)= 全选池内所有文献。
# ----------------------------------------------------------------------------


def _lang_pool_range(db: Session, task_id: str, sources: tuple[str, ...]) -> LangPoolRange:
    """全池范围统计(不受筛选影响):总数 + 年份 min/max,供前端生成年份下拉选项。"""
    row = db.execute(
        select(func.count(), func.min(PaperModel.year), func.max(PaperModel.year))
        .where(PaperModel.task_id == task_id, PaperModel.source.in_(sources))
    ).one()
    return LangPoolRange(
        total=int(row[0] or 0),
        min_year=int(row[1] or 0),
        max_year=int(row[2] or 0),
    )


def _pick_by_filter(
    db: Session,
    task_id: str,
    sources: tuple[str, ...],
    year_start: int,
    year_end: int,
    limit: int,
    core_only: bool = False,
) -> tuple[list[PaperModel], bool]:
    """按年份/核心条件取候选集,再按 limit(created_at 倒序)截断。

    返回 (截断后的行, 是否发生了截断)。limit=0 表示不限。
    """
    rows = db.execute(
        select(PaperModel)
        .where(PaperModel.task_id == task_id, PaperModel.source.in_(sources))
        .order_by(PaperModel.created_at.desc())
    ).scalars().all()
    import core_journals as _cj
    candidates: list[PaperModel] = []
    for r in rows:
        y = r.year or 0
        if year_start > 0 and y < year_start:
            continue
        if year_end > 0 and y > year_end:
            continue
        # 核心期刊只对中文来源有意义;英文候选集 core_only 恒为 False
        if core_only and not bool(_cj.classify_journal(r.journal)["in_core"]):
            continue
        candidates.append(r)
    if limit > 0 and len(candidates) > limit:
        return candidates[:limit], True
    return candidates, False


@router.post("/export-preview", response_model=ExportPreviewResponse)
def export_preview(
    req: ExportPreviewRequest,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """「去写作」筛选预览:返回全池范围、筛选后统计(核心/非核心/年份分布)与 lit_ids。"""
    task_id = _resolve_task_id(x_task_id)
    import core_journals as _cj

    # 中文候选集:cn_core_only=true 时数量截断交给 cn_core_limit,cn.limit 不生效
    cn_rows, cn_trunc = _pick_by_filter(
        db, task_id, _CN_SOURCES,
        req.cn.year_start, req.cn.year_end,
        0 if req.cn_core_only else req.cn.limit,
        core_only=req.cn_core_only,
    )
    if req.cn_core_only and req.cn_core_limit > 0 and len(cn_rows) > req.cn_core_limit:
        cn_rows = cn_rows[:req.cn_core_limit]
        cn_trunc = True
    en_rows, en_trunc = _pick_by_filter(
        db, task_id, _EN_SOURCES,
        req.en.year_start, req.en.year_end, req.en.limit,
    )
    # 中文核心/非核心细分(对截断后的导入集合统计)
    cn_core = sum(1 for r in cn_rows if _cj.classify_journal(r.journal)["in_core"])
    # 年份分布(合并中英文;year<=0 视为未知不入图)
    dist: dict[int, YearDistItem] = {}
    for r in cn_rows:
        y = r.year or 0
        if y > 0:
            item = dist.setdefault(y, YearDistItem(year=y))
            item.cn += 1
    for r in en_rows:
        y = r.year or 0
        if y > 0:
            item = dist.setdefault(y, YearDistItem(year=y))
            item.en += 1
    distribution = [dist[k] for k in sorted(dist, reverse=True)]
    return ExportPreviewResponse(
        pool=ExportPoolRange(
            cn=_lang_pool_range(db, task_id, _CN_SOURCES),
            en=_lang_pool_range(db, task_id, _EN_SOURCES),
        ),
        filtered=ExportPreviewStats(
            total=len(cn_rows) + len(en_rows),
            cn_total=len(cn_rows),
            cn_core=cn_core,
            cn_non_core=len(cn_rows) - cn_core,
            en_total=len(en_rows),
            truncated=cn_trunc or en_trunc,
            year_distribution=distribution,
            lit_ids=[r.lit_id for r in cn_rows] + [r.lit_id for r in en_rows],
        ),
    )


@router.post("/batch-select", response_model=BatchSelectResponse)
def batch_select(
    req: BatchSelectRequest,
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """批量勾选/取消勾选(任务隔离)。

    mode=replace:先清空当前 task 全部勾选,再把 lit_ids 置为勾选 —— 「去写作」导入用。
    mode=add:仅把 lit_ids 置为勾选。
    """
    task_id = _resolve_task_id(x_task_id)
    if len(req.lit_ids) > 10_000:
        raise HTTPException(422, "lit_ids 数量超过上限 10000")
    cleared = 0
    if req.mode == "replace":
        cleared = db.query(PaperModel).filter(
            PaperModel.task_id == task_id,
            PaperModel.selected.is_(True),
        ).update({PaperModel.selected: False}, synchronize_session=False)
    # 去重保序 + 分批更新(防 SQLite 绑定变量上限)
    ids = list(dict.fromkeys(i for i in req.lit_ids if i))
    updated = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        updated += db.query(PaperModel).filter(
            PaperModel.task_id == task_id,
            PaperModel.lit_id.in_(chunk),
        ).update({PaperModel.selected: True}, synchronize_session=False)
    db.commit()
    return BatchSelectResponse(updated=updated, cleared=cleared)


@router.post("/new-session")
def new_session(
    x_task_id: str | None = Header(None, alias=_HEADER_TASK_ID),
    db: Session = Depends(get_db),
):
    """开一个新任务(检索任务)。

    行为:
      1. 若 X-Task-Id 已存在 → 直接返回(幂等),告知这是已存在 task。
      2. 否则生成新的 UUID,清空当前内存池(不动数据库,但下次写入会用新 task_id),
         并返回新 task_id。
      3. 清空数据库里当前 task 的所有 papers(避免历史数据穿透)。
    """
    from logging import getLogger
    log = getLogger(__name__)
    current = _resolve_task_id(x_task_id)
    if current != _DEFAULT_TASK_ID:
        # 复用现有 task_id,只清这个 task 的论文
        deleted = db.query(PaperModel).filter(
            PaperModel.task_id == current
        ).delete(synchronize_session=False)
        db.commit()
        log.info("new-session: 复用 task_id=%s 清空 %d 条", current, deleted)
        return {"task_id": current, "reused": True, "cleared": deleted}
    # 新建 task
    new_id = str(uuid.uuid4())
    log.info("new-session: 生成新 task_id=%s", new_id)
    return {"task_id": new_id, "reused": False, "cleared": 0}
