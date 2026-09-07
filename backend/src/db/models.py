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
# 方案 §4 数据模型扩展(本轮 Phase 3 落地):
# canonical_work / source_snapshot / verification_run / theme_assignment /
# claim / citation_link / draft_version / quality_gate / artifact
# canonical_work 是唯一书目主键;claim 必须经 citation_link 指向
# verification_status=PASS 的 canonical_work,实现引用门禁。
# ============================================================================


class CanonicalWorkModel(Base):
    """唯一可信书目记录(方案 §4 核心表)。

    替代原 papers 表中的"脏"记录(多源未合并、未核验)。
    一篇 canonical_work 可以关联多个 source_snapshot(同一文献的多源原始记录)。
    """
    __tablename__ = "canonical_works"

    canonical_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    journal: Mapped[str] = mapped_column(String(200), default="", index=True)
    year: Mapped[int] = mapped_column(Integer, default=0, index=True)
    volume: Mapped[str | None] = mapped_column(String(50), nullable=True)
    issue: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pages: Mapped[str | None] = mapped_column(String(50), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    work_type: Mapped[str] = mapped_column(String(32), default="journal")  # journal / preprint / unknown
    verification_status: Mapped[str] = mapped_column(
        String(16), default="PENDING", index=True
    )  # PENDING / PASS / FAIL
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SourceSnapshotModel(Base):
    """原始检索响应快照(方案 §4)。

    每条原始记录(无论来源)对应一个 snapshot;
    多次重抓可保留历史,proof 元数据来源。
    """
    __tablename__ = "source_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_record_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_html_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    retrieved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VerificationRunModel(Base):
    """三重核验运行记录(方案 §4)。

    每条 canonical_work 在每次核验后产生一条 VerificationRun。
    """
    __tablename__ = "verification_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(36), index=True)
    rule_version: Mapped[str] = mapped_column(String(16), default="v1")
    field_results: Mapped[dict] = mapped_column(JSON, default=dict)
    # 字典结构:{"is_journal": "pass", "year_ok": "pass", "metadata_complete": "pass",
    #          "source_reachable": "pass"}
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", index=True
    )  # PENDING / PASS / FAIL
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class ThemeAssignmentModel(Base):
    """主题分组 Agent 的分配结果(方案 §4)。

    每篇 canonical_work 在一次综述任务下只能分到一个主题。
    """
    __tablename__ = "theme_assignments"

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    canonical_id: Mapped[str] = mapped_column(String(36), index=True)
    theme_index: Mapped[int] = mapped_column(Integer)
    theme_name: Mapped[str] = mapped_column(String(200))
    evidence_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ClaimModel(Base):
    """写作论断与文献绑定(方案 §4)。

    claim 必须经 citation_link 指向 verification_status=PASS 的 canonical_work,
    实现「引用门禁」。
    """
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    section_key: Mapped[str] = mapped_column(String(64), index=True)
    theme_index: Mapped[int] = mapped_column(Integer)
    claim_text: Mapped[str] = mapped_column(Text)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CitationLinkModel(Base):
    """正文夹注、脚注、参考文献三方映射(方案 §4)。

    每一个正文 draft_span(段落)中的 [lit_xxx] 锚点对应一个 citation_link,
    其 cite_id 必须指向 PASS 的 canonical_work。
    """
    __tablename__ = "citation_links"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    canonical_id: Mapped[str] = mapped_column(String(36), index=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    section_key: Mapped[str] = mapped_column(String(64))
    draft_span: Mapped[str] = mapped_column(String(500))  # 段落内的标记区间
    anchor_text: Mapped[str] = mapped_column(String(64))  # [lit_xxx] 或 [N]
    footnote_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    citation_status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending / locked / dropped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DraftVersionModel(Base):
    """草稿版本快照(方案 §4)。

    每次写作 / 润色 / 修订产生一个 draft_version,保留历史可回滚。
    """
    __tablename__ = "draft_versions"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(32))  # outlined / drafted / humanized_1 / rendered / delivered
    sections_json: Mapped[list] = mapped_column(JSON, default=list)
    reference_list: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class QualityGateModel(Base):
    """质量门禁执行记录(方案 §4)。

    每类核查(accuracy/binding/quota/citation_format)的执行结果落库,
    对应方案 §5 状态机中每个阶段的 quality_gate。
    """
    __tablename__ = "quality_gates"

    gate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    gate_type: Mapped[str] = mapped_column(String(32), index=True)
    # accuracy / binding / quota / citation_format / render_check
    stage: Mapped[str] = mapped_column(String(32), index=True)
    # verify_1 / verify_2 / verify_3 / final
    status: Mapped[str] = mapped_column(String(16))  # pass / warn / fail / skipped
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ArtifactModel(Base):
    """交付产物记录(方案 §4)。

    每个最终交付的 docx/pdf/快照包对应一条 artifact。
    """
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_task_id: Mapped[str] = mapped_column(String(36), index=True)
    artifact_type: Mapped[str] = mapped_column(String(16))  # docx / pdf / bundle
    file_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class TaskStageModel(Base):
    """任务状态机持久化(方案 §5)。

    替代/增强原 task.status 简单字符串,记录 13 节点状态机的当前阶段 + 历史。
    """
    __tablename__ = "task_stages"

    stage_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    # PLANNING / RETRIEVING / NORMALIZED / VERIFY_1 / SCREENED / VERIFY_2 /
    # OUTLINED / DRAFTED / VERIFY_3 / CITATION_LOCKED /
    # HUMANIZED_1 / HUMANIZED_2 / HUMANIZED_3 / RENDERED / DELIVERED
    status: Mapped[str] = mapped_column(String(16))  # running / pass / fail / skipped
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================================
# 生产化扩展(本轮 Phase 7 落地):
# User / RBAC / AuditLog / EvalSet
# ============================================================================


class UserModel(Base):
    """用户(方案 §9 生产化:多租户)。"""
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # 密码哈希(bcrypt/argon2);开发环境可用 sha256+salt
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    # user / reviewer / admin
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


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


class EvalSetModel(Base):
    """评测集(方案 §9 生产化:评测集)。"""
    __tablename__ = "eval_sets"

    case_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    topic: Mapped[str] = mapped_column(String(500))
    expected_paper_count_min: Mapped[int] = mapped_column(Integer, default=70)
    expected_paper_count_max: Mapped[int] = mapped_column(Integer, default=90)
    expected_chinese_ratio_min: Mapped[float] = mapped_column(Float, default=2/3)
    expected_keywords: Mapped[list] = mapped_column(JSON, default=list)
    # 期望正文必须出现的核心词
    reference_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 期望黄金对照 docx 路径(可选)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
