"""主题分组 Agent(方案 §3 "主题分组 Agent")。

输入: 已放行的 canonical_work 池(verification_status=PASS)
输出: theme_assignments(每篇 canonical_work 在一次综述任务下唯一分到 1 个主题)

设计:
- 复用 writing/classifier.classify()(LLM 驱动分主题),但本 Agent 负责:
  1. 把 classifier 结果中的 paper_lit_ids 解析为 canonical_id;
  2. 把分配落库 theme_assignments 表;
  3. 一文一主题硬约束(同一 canonical_id 在同一 task 下不可重复分配)。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Iterable

from db.models import CanonicalWorkModel, ThemeAssignmentModel
from db.session import SessionLocal

log = logging.getLogger(__name__)


def _new_assignment_id() -> str:
    return f"asn_{uuid.uuid4().hex[:12]}"


def assign_themes(
    *,
    review_task_id: str,
    theme_groups: list[dict],
) -> int:
    """根据 classifier 输出的 theme_groups 落库 theme_assignments。

    theme_groups 形态(与 writing.classifier.Group 一致):
      [
        {"name": "主题1", "lit_ids": ["lit_oa_xxx", "lit_cnki_xxx", ...]},
        {"name": "主题2", "lit_ids": [...]},
        ...
      ]

    v9.6:键名以 classifier.Group 的实际字段 lit_ids 为准;兼容旧文档写的
    paper_lit_ids(此前按 paper_lit_ids 读取,传入标准形态必然解析为空)。

    一文一主题约束:同一 canonical_id 在同一 review_task_id 下只能分到 1 个主题;
    重复时,以第一次出现的 theme 为准,其余忽略并 warning。

    返回: 实际成功落库的 assignment 行数。
    """
    assigned = 0
    seen_canonicals: set[str] = set()

    with SessionLocal() as db:
        for theme_idx, grp in enumerate(theme_groups, start=1):
            name = (grp.get("name") or f"主题{theme_idx}").strip()
            # v9.6:以 classifier.Group 的实际字段 lit_ids 为准,兼容旧键 paper_lit_ids
            lit_ids = list(grp.get("lit_ids") or grp.get("paper_lit_ids") or [])

            for lit_id in lit_ids:
                # 通过 lit_id 反查 canonical_id
                # 当前阶段 papers 表与 canonical_works 表并存,
                # 这里采用「先查 canonical;若无,查 papers 反推 identity」
                row = (
                    db.query(CanonicalWorkModel)
                    .filter_by(canonical_id=lit_id)
                    .one_or_none()
                )
                if row is None:
                    # 兜底:把 lit_id 当作 canonical_id 直接用(可能在迁移期)
                    canonical_id = lit_id
                else:
                    canonical_id = row.canonical_id

                if canonical_id in seen_canonicals:
                    log.warning(
                        "一文一主题冲突:canonical=%s 在 task=%s 已分配,跳过 theme=%s",
                        canonical_id, review_task_id, name,
                    )
                    continue
                seen_canonicals.add(canonical_id)

                a = ThemeAssignmentModel(
                    assignment_id=_new_assignment_id(),
                    review_task_id=review_task_id,
                    canonical_id=canonical_id,
                    theme_index=theme_idx,
                    theme_name=name,
                    evidence_summary=(grp.get("evidence_summary") or "")[:1000] or None,
                    assigned_at=datetime.now(timezone.utc),
                )
                db.add(a)
                assigned += 1
        db.commit()
    return assigned


def get_task_assignments(review_task_id: str) -> list[ThemeAssignmentModel]:
    """拉取一个综述任务下的所有主题分配。"""
    with SessionLocal() as db:
        return list(
            db.query(ThemeAssignmentModel)
            .filter_by(review_task_id=review_task_id)
            .order_by(ThemeAssignmentModel.theme_index.asc())
            .all()
        )


__all__ = ["assign_themes", "get_task_assignments"]