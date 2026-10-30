# -*- coding: utf-8 -*-
"""写作链策略矛盾回归(2026-10-30 今晚跑批,4 组 90 篇)。

事故:34/35 篇组配额要求 24-25(70% 比例),物理不可达(单章正文窗口
2000-3200 字的自然容量 12-16 篇);诚实正文(2000 字、10-22 引用)被判
不足 → `_retry_section_quality` 自动重写仍不足 → 走题录恢复稿整段替换
→ 恢复稿占比 3/5 ≥ 0.5 触发全场交付门禁。而 9 篇组(theme_2)同样密度
不足却是「保留诚实内容 + 密度警告」。

修复 = ①重写仍不达标时保留诚实正文 + 密度警告(theme_2 先例同一出口,
且择优保留引用更多的重写稿),题录恢复稿仅留给 LLM 硬失败(无正文/非
文章/兜底墙);②配额公式上限封顶 48 → 16(要求上限封顶而非放宽底线,
≤22 篇组要求值完全不变)。全模板墙交付门禁的阈值与判定逻辑不动。
"""
import writing.orchestrator as orch
import writing.section_writer as sw
from retrieval.types import Paper, Source
from writing.section_writer import SectionResult
from writing.templates import SectionSpec


def _paper(i: int, zh: bool = True) -> Paper:
    if zh:
        return Paper(
            lit_id=f"lit_cnki_{i:016x}", source=Source.CNKI,
            title=f"水产品营销问题{i}的机制与对策研究",
            authors=[f"作者{i}"], journal="测试学报", year=2023,
            abstract=f"本文研究水产品营销场景{i}的渠道与消费行为。",
        )
    return Paper(
        lit_id=f"lit_openalex_{i:016x}", source=Source.OPENALEX,
        title=f"Seafood marketing study {i}",
        authors=[f"Author {i}"], journal="Aquaculture", year=2023,
        abstract=f"Study {i} on seafood marketing channels.",
    )


def _honest_content(n_chars: int) -> str:
    """带真实论述形态的正文(非占位/非兜底句),可循环到指定长度。"""
    base = (
        "现有研究围绕水产品营销的核心议题展开了多角度讨论。张三（2023）指出渠道变革"
        "深刻影响了终端效率，李四（2022）则比较了不同电商模式的边际成本差异，"
        "王五（2021）进一步补充了品牌溢价的形成条件与区域市场约束。"
    )
    out = base
    while len(out) < n_chars:
        out += base
    return out


def _spec() -> SectionSpec:
    return SectionSpec(key="theme_3", title="三、水产品营销策略", instruction="")


# ---------- 用例 A:密度不足保留诚实正文,不产恢复稿 ----------

def test_retry_keeps_honest_draft_when_density_short(monkeypatch):
    """34 篇组要求 16(封顶后),原稿 10 引用、重写 15 引用仍 <16 →
    保留诚实正文 + 密度警告标记,不产生题录恢复稿;且择优保留引用
    更多的重写稿(15/16 优于 10/16)。"""
    papers = [_paper(i, zh=(i % 2 == 0)) for i in range(1, 35)]
    assert sw._min_citation_target(len(papers)) == 16

    original = SectionResult(
        key="theme_3", title="三、水产品营销策略",
        content=_honest_content(1600),
        citations=[p.lit_id for p in papers[:10]],
        density_warning=True,  # 10 < 16
    )
    retry_result = SectionResult(
        key="theme_3", title="三、水产品营销策略",
        content=_honest_content(1500),
        citations=[p.lit_id for p in papers[:15]],
        density_warning=True,  # 15 < 16 仍不足,但优于原稿
    )

    monkeypatch.setattr(sw, "write_section", lambda *a, **k: retry_result)
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    out = orch._retry_section_quality(
        spec=_spec(), topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=original,
    )
    assert out is retry_result, "择优保留引用更多的诚实重写稿"
    assert out.density_warning is True, "密度警告标记必须保留"
    assert "以下研究均来自当前文献池" not in out.content
    assert "题录已核验" not in out.content
    assert orch._recovery_draft_ratio([out]) == 0.0, "不产生题录恢复稿"


def test_retry_keeps_original_honest_draft_when_retry_not_better(monkeypatch):
    """重写没有更好时保留原稿(诚实正文),同样不走恢复稿。"""
    papers = [_paper(i) for i in range(1, 35)]
    original = SectionResult(
        key="theme_4", title="四、水产品营销策略",
        content=_honest_content(2000),
        citations=[p.lit_id for p in papers[:10]],
        density_warning=True,
    )
    monkeypatch.setattr(
        sw, "write_section",
        lambda *a, **k: SectionResult(
            key="theme_4", title="四、水产品营销策略",
            content=_honest_content(300),  # 更短更差
            citations=[p.lit_id for p in papers[:8]],
            density_warning=True,
        ),
    )
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    out = orch._retry_section_quality(
        spec=_spec(), topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=original,
    )
    assert out is original, "重写不优于原稿时保留原诚实正文"
    assert orch._recovery_draft_ratio([out]) == 0.0


# ---------- 用例 B:LLM 硬失败仍走恢复稿(防线不回退) ----------

def test_retry_still_uses_recovery_draft_on_llm_hard_failure(monkeypatch):
    """原稿无正文(硬失败)+ 重写连续异常 → 题录恢复稿接管,防线不回退。"""
    papers = [_paper(i) for i in range(1, 35)]
    hard_failed = SectionResult(
        key="theme_3", title="三、水产品营销策略", content="", citations=[],
    )

    def boom(*args, **kwargs):
        raise RuntimeError("simulated LLM stream failure")

    monkeypatch.setattr(sw, "write_section", boom)
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    # 2026-10-30 D2:硬失败分支新增节级重试(歇 20s),测试中置零
    monkeypatch.setattr(orch, "_SECTION_HARD_RETRY_SLEEP_SECONDS", 0)
    monkeypatch.setattr(orch.time, "sleep", lambda s: None)
    out = orch._retry_section_quality(
        spec=_spec(), topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=hard_failed,
    )
    assert "以下研究均来自当前文献池" in out.content, "硬失败仍由题录恢复稿接管"
    assert len(out.citations) == 34, "恢复稿绑定全组题录"
    assert orch._recovery_draft_ratio([out]) == 1.0


# ---------- D2(2026-10-30 第七轮):整节恢复稿墙 → 节级重试 ----------

def test_hard_failed_initial_rewrite_recovers_honest_draft(monkeypatch):
    """硬失败原稿 → 初始重写循环内就捞回诚实正文 → 直接保留,无需
    节级重试(不交付整节恢复稿墙)。"""
    papers = [_paper(i) for i in range(1, 35)]
    hard_failed = SectionResult(
        key="theme_2", title="二、水产品营销策略国际研究", content="", citations=[],
    )
    good = SectionResult(
        key="theme_2", title="二、水产品营销策略国际研究",
        content=_honest_content(1200),
        citations=[p.lit_id for p in papers[:10]],
        density_warning=True,  # 诚实薄稿(密度不足)也好于整节模板墙
    )
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_write(*args, **kwargs):
        calls["n"] += 1
        return good

    monkeypatch.setattr(sw, "write_section", fake_write)
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    monkeypatch.setattr(orch.time, "sleep", lambda s: sleeps.append(s))
    out = orch._retry_section_quality(
        spec=SectionSpec(key="theme_2", title="二、水产品营销策略国际研究", instruction=""),
        topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=hard_failed,
    )
    assert out is good, "重写循环捞回的诚实正文必须保留"
    assert "以下研究均来自当前文献池" not in out.content
    assert calls["n"] == 2, "初始重写循环 2 次,未进入节级重试"
    assert sleeps == [], "循环已捞回正文,不需要节级重试歇脚"


def test_hard_failed_section_gets_one_full_retry(monkeypatch):
    """初始重写循环连续硬失败(如流式超时)→ 歇 20s 节级重试一次完整
    写作 → 成功则恢复真实正文,不交付整节题录恢复稿墙。"""
    papers = [_paper(i) for i in range(1, 35)]
    hard_failed = SectionResult(
        key="theme_2", title="二、水产品营销策略国际研究", content="", citations=[],
    )
    good = SectionResult(
        key="theme_2", title="二、水产品营销策略国际研究",
        content=_honest_content(1200),
        citations=[p.lit_id for p in papers[:10]],
        density_warning=True,
    )
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_write(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return SectionResult(
                key="theme_2", title="二、水产品营销策略国际研究",
                content="", citations=[],  # 初始重写循环:连续硬失败
            )
        return good  # 节级重试:分钟级抖动过去,完整写作成功

    monkeypatch.setattr(sw, "write_section", fake_write)
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    monkeypatch.setattr(orch.time, "sleep", lambda s: sleeps.append(s))
    out = orch._retry_section_quality(
        spec=SectionSpec(key="theme_2", title="二、水产品营销策略国际研究", instruction=""),
        topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=hard_failed,
    )
    assert out is good, "节级重试成功必须恢复真实正文"
    assert "以下研究均来自当前文献池" not in out.content
    assert calls["n"] == 3, "初始重写 2 次 + 节级重试 1 次"
    assert sleeps == [20.0], "节级重试前歇 _SECTION_HARD_RETRY_SLEEP_SECONDS"


def test_hard_failed_retry_exhausted_keeps_recovery_draft(monkeypatch):
    """节级重试仍产硬失败形态 → 保留题录恢复稿(内容含恢复稿指纹,
    QA 经 article_lint 的 FALLBACK 词表以 ARTICLE_FALLBACK_ONLY 如实标注);
    重试恰好一次(初始重写循环 2 次 + 节级重试 1 次 = 3 次写作调用)。"""
    papers = [_paper(i) for i in range(1, 35)]
    hard_failed = SectionResult(
        key="theme_2", title="二、水产品营销策略国际研究", content="", citations=[],
    )
    calls = {"n": 0}

    def fake_write(*args, **kwargs):
        calls["n"] += 1
        return SectionResult(
            key="theme_2", title="二、水产品营销策略国际研究",
            content="", citations=[],  # 恒硬失败
        )

    monkeypatch.setattr(sw, "write_section", fake_write)
    monkeypatch.setattr(sw, "ensure_section_evidence", lambda *a, **k: None)
    sleeps: list[float] = []
    monkeypatch.setattr(orch.time, "sleep", lambda s: sleeps.append(s))
    out = orch._retry_section_quality(
        spec=SectionSpec(key="theme_2", title="二、水产品营销策略国际研究", instruction=""),
        topic="水产品营销策略", groups=[], papers=papers,
        grades={}, result=hard_failed,
    )
    assert "以下研究均来自当前文献池" in out.content, "重试仍失败保留恢复稿"
    assert calls["n"] == 3, "初始重写 2 次 + 节级重试 1 次"
    assert len(sleeps) == 1, "节级重试只歇一次"
    assert orch._recovery_draft_ratio([out]) == 1.0


def test_hard_failed_detection_splits_honest_from_wall():
    """硬失败判定拆分:诚实正文(仅密度不足)不算硬失败;空正文/兜底墙算。"""
    honest = SectionResult(
        key="theme_1", title="t", content=_honest_content(300),
        citations=["lit_a"], density_warning=True,
    )
    assert not orch._section_hard_failed(honest)
    assert orch._section_requires_quality_retry(honest), "密度不足仍需重写判定"
    assert orch._section_hard_failed(SectionResult(
        key="theme_1", title="t", content="", citations=[],
    ))
    wall = SectionResult(
        key="theme_1", title="t",
        content="补充证据显示某研究与此相关。\n补充证据显示另一研究与此相关。",
        citations=["lit_a"],
    )
    assert orch._section_hard_failed(wall), "兜底句墙按硬失败处理"


# ---------- 用例 C:配额公式新旧对照 ----------

def test_citation_target_cap_new_vs_old():
    """配额公式上限封顶 48 → 16:中小组(≤22 篇)要求值不变,大组按单章
    容量封顶;下限 12 与 70% 比例保留(只封顶、不放宽底线)。"""
    f = sw._min_citation_target
    # 小池子既有规则不变
    assert f(0) == 0
    assert f(3) == 2
    assert f(4) == 3
    assert f(5) == 5
    # 中小组:新旧一致(今晚 theme_1/theme_2 场景)
    assert f(9) == 5     # 旧 5(不变)
    assert f(13) == 7    # 旧 7(不变)
    assert f(22) == 16   # 旧 16(不变,ceil(15.4)=16)
    # 大组:封顶生效
    assert f(23) == 16   # 旧 17
    assert f(34) == 16   # 旧 24(今晚 theme_3,物理不可达)
    assert f(35) == 16   # 旧 25(今晚 theme_4,实得 22 只差 2-3)
    assert f(48) == 16   # 旧 34
    assert f(90) == 16   # 旧 48
