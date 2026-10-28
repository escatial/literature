"""ORM 模型。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .session import Base


def _utcnow() -> datetime:
    """统一的 UTC 当前时间(Python 3.12+ datetime.utcnow 已弃用)。"""
    return datetime.now(timezone.utc)


class PaperModel(Base):
    """文献池条目(中英文统一存储)。

    v8.1 任务隔离(彻底版):主键从全局 lit_id 改为代理自增 id,
    同一文献可在不同任务各存一行(task_id, lit_id) —— 任务间零互通。
    lit_id 不再是主键,只是文献指纹哈希(同源同 identity_key 必同 lit_id)。
    """
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lit_id: Mapped[str] = mapped_column(String(32), index=True)
    # v7.0/v8.1:identity_key 唯一性限定在「同一 task 内」,由 init_db 创建的
    # uq_papers_identity_key_per_task (identity_key, task_id) 组合唯一索引维护。
    identity_key: Mapped[str | None] = mapped_column(String(500), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # openalex/crossref/user_imported
    title: Mapped[str] = mapped_column(String(500), index=True)
    authors: Mapped[list] = mapped_column(JSON, default=list)   # ["A", "B"]
    journal: Mapped[str] = mapped_column(String(200), default="")
    year: Mapped[int] = mapped_column(Integer, default=0, index=True)
    volume: Mapped[str | None] = mapped_column(String(50), nullable=True)
    issue: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pages: Mapped[str | None] = mapped_column(String(50), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    cited_by_count: Mapped[int] = mapped_column(Integer, default=0)
    journal_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_citation: Mapped[str | None] = mapped_column(Text, nullable=True)  # 中文 GB/T 7714 原文
    quote_text: Mapped[str | None] = mapped_column(Text, nullable=True)   # v4.0 知网导出页引文
    abstract_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # v4.0 摘要原文
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # 任务隔离(task isolation):每个 task 有自己的论文池,跨 task 不可见。
    # 老数据/迁移期用 "__default__" 占位;新建 paper 必须带 task_id。
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # B6(2026-10-27) 数据归属:NULL = 存量行(启动时回填首个 admin 或 '__legacy__')。
    # 与 task_id 双层正交:task_id 是业务域隔离(同用户多任务),
    # owner_user_id 是安全域隔离(跨用户不可见)。
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # 入库硬约束:title / year / abstract 缺一不可(SQLite + PostgreSQL 双方言生效)。
    # 历史脏数据采用应用层校验拦截,不依赖 DB CHECK(已存在行不会回填约束)。
    __table_args__ = (
        CheckConstraint(
            "length(title) > 0",
            name="ck_paper_title_nonempty",
        ),
        CheckConstraint(
            "year > 0",
            name="ck_paper_year_positive",
        ),
        CheckConstraint(
            "length(coalesce(abstract, '')) > 0 OR length(coalesce(abstract_text, '')) > 0",
            name="ck_paper_abstract_nonempty",
        ),
    )


class RetrievalTaskModel(Base):
    """英文检索后台任务。"""
    __tablename__ = "retrieval_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str] = mapped_column(String(500), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    year_start: Mapped[int] = mapped_column(Integer, default=0)
    year_end: Mapped[int] = mapped_column(Integer, default=0)
    min_citations: Mapped[int] = mapped_column(Integer, default=0)
    limit: Mapped[int] = mapped_column(Integer, default=50)
    use_rerank: Mapped[bool] = mapped_column(Boolean, default=True)
    topic_summary: Mapped[str] = mapped_column(Text, default="")
    query_used: Mapped[str] = mapped_column(Text, default="")
    total_before_filter: Mapped[int] = mapped_column(Integer, default=0)
    total_after_filter: Mapped[int] = mapped_column(Integer, default=0)
    papers: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v4.1 英文检索过程日志(供前端按 db 拆分展示)
    # 每条: {"stage": ..., "source": ..., "page": ..., "added": ..., "total": ..., "message": ...}
    events: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # B6 数据归属:创建任务的登录用户;NULL = 存量行(启动回填)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ChineseWorkflowTaskModel(Base):
    """中文统一检索工作流任务(持久化,后端重启后可恢复进度)。"""
    __tablename__ = "chinese_workflow_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    query: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    session_id: Mapped[str] = mapped_column(String(36), default="")
    events: Mapped[list] = mapped_column(JSON, default=list)
    items: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # B6 数据归属:创建工作流的登录用户;NULL = 存量行(启动回填)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ReviewModel(Base):
    """综述生成记录(每次生成一条)。"""
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(500), index=True)
    classify_mode: Mapped[str] = mapped_column(String(20))  # locale/theme
    sections: Mapped[list] = mapped_column(JSON, default=list)  # [{key, title, content, citations}]
    reference_list: Mapped[str] = mapped_column(Text, default="")
    screened_out_ids: Mapped[list] = mapped_column(JSON, default=list)
    dropped_citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # B6 数据归属:发起写作的登录用户;NULL = 存量行(启动回填)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class RetrievalHistoryModel(Base):
    """统一检索历史(最近 5 条,超过按 created_at 自动淘汰)。

    v5.0 需求4:统一检索模块保留最近 5 条历史记录,字段包含
    检索时间、检索关键词、检索总数量、文献元数据(快照)。
    """
    __tablename__ = "retrieval_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(500), index=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_sources: Mapped[dict] = mapped_column(JSON, default=dict)
    papers_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 一次「启动自动检索」聚合后的 run_id;中文 + 英文两边共享同一个 run_id,
    # 由前端启动时分配,后端在两边都完成时合并写一条检索历史。
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    # B6 数据归属:发起检索的登录用户;NULL = 存量行(启动回填)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class NotifyContactModel(Base):
    """通知联系人配置。

    邮箱即联系人;usage 区分用途:
      - api:   OpenAlex 礼貌池识别(OPENALEX_MAILTO)
      - report: 关键报告接收
      - alert:  告警信息接收
      - all:    全部
    未配置 SMTP 授权码时仅作登记,不发信。
    """
    __tablename__ = "notify_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    usage: Mapped[str] = mapped_column(String(50), default="api")  # api/report/alert/all
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CoreJournalModel(Base):
    """核心期刊清单(数据库存储)。

    多套来源列于 list_source 枚举:
      - pku_core:    北大《中文核心期刊要目总览》
      - cscd_core:   中国科学引文数据库核心库
      - cscd_ext:    中国科学引文数据库扩展库
      - cssci_core:  南京大学 CSSCI 来源期刊

    name_key 是 name 归一化后的可匹配字符串(用于 /papers 查询时的 in_core 过滤)。
    """
    __tablename__ = "core_journals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_source: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        # 同一 list_source 下刊名去重
        {"sqlite_with_rowid": True},
    )


# ============================================================================
# 2026-10-27 C4 死表裁决:方案 §4 蓝面的 10 个零引用模型已删除
# (CanonicalWork/SourceSnapshot/VerificationRun/ThemeAssignment/Claim/
#  CitationLink/DraftVersion/QualityGate/Artifact/EvalSet)。
# 全库 grep 留痕:业务/测试零引用,仅 theme/verification 两个死模块引用
# (模块同批删除)。存量库中的同名表不写 DROP,自然闲置,安全。
# TaskStageModel 保留:statemachine/controller.py 真实读写(C3 接线)。
# ============================================================================


class TaskStageModel(Base):
    """任务状态机持久化(方案 §5)。

    替代/增强原 task.status 简单字符串,记录状态机当前阶段 + 历史。
    C3(2026-10-27) 收敛为 7 节点链:PLANNING / SCREENED / OUTLINED /
    DRAFTED / QA_VERIFIED / RENDERED / DELIVERED(见 statemachine/controller.py)。
    """
    __tablename__ = "task_stages"

    stage_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16))  # running / pass / fail / skipped
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================================
# 生产化扩展:
# User / RBAC / AuditLog
# ============================================================================


class UserModel(Base):
    """用户(方案 §9 生产化:多租户;2026-10 扩展:注册审核 + 按用户 LLM 配置)。"""
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # 密码哈希:PBKDF2-SHA256(security/rbac.py),格式 pbkdf2_sha256$iter$salt$digest
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    # user / reviewer / admin
    # 注册审核:pending(待管理员审核) / approved(通过) / rejected(拒绝)。
    # 老库迁移补列时 DEFAULT 'approved',存量用户不受影响;新注册显式写入。
    status: Mapped[str] = mapped_column(String(16), default="approved", index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ── 按用户 LLM 配置(用户自定义 provider;空 = 使用系统 .env 默认) ──
    # kind: minimax(官方/兼容接口,启用 reasoning_split) / openai(通用 OpenAI 兼容)
    llm_kind: Mapped[str] = mapped_column(String(16), default="minimax")
    llm_base_url: Mapped[str] = mapped_column(String(500), default="")
    # 明文存于本地 sqlite:本工具为局域网/单机部署,密钥由用户自行填入;
    # API 返回一律脱敏(mask_key),绝不回传完整 key。
    llm_api_key: Mapped[str] = mapped_column(Text, default="")
    llm_model: Mapped[str] = mapped_column(String(200), default="")


class AuthTokenModel(Base):
    """登录令牌(库里只存 SHA-256 哈希,原文仅下发一次给前端)。

    B1(2026-10-27):expires_at 可空 —— NULL = 存量令牌(首次校验时回填
    TTL,不踢存量会话);非 NULL 过期即删行拒绝,剩余 < TTL/2 时滑动顺延。
    """
    __tablename__ = "auth_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLogModel(Base):
    """审计日志(方案 §9 生产化:审计)。"""
    __tablename__ = "audit_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    # e.g. "review.create", "verify.run", "render.deliver"
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# EvalSetModel 已于 2026-10-27 C4 删除(零引用;存量 eval_sets 表自然闲置)。


class WritingPlanModel(Base):
    """两阶段写作的阶段1规划快照(C5 断点恢复)。

    - plan_id 代理主键;task_id UNIQUE(一次规划对应一个任务档案,重复规划覆盖);
    - user_id:owner(B6 语义,查询按归属过滤);
    - payload:groups/section_titles/screened_lit_ids/relevance_report/blueprint/
      research_questions/review_goal —— lit_ids 只存引用,papers 表是唯一真源。
    """
    __tablename__ = "writing_plans"

    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    topic: Mapped[str] = mapped_column(String(500), default="")
    classify_mode: Mapped[str] = mapped_column(String(20), default="theme")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
