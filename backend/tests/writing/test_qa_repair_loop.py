# -*- coding: utf-8 -*-
"""终局 QA 修复闭环(失败→确定性+LLM 定向重写→重编号→复查)专项回归。

用户策略:QA FAIL 必须先自动修复,修不掉的才上报人工复核——
不是把 84 项体检问题原样抛给用户。

覆盖:
- 段落引用密度不足 → LLM 带问题清单重写 → 复查通过,auto_repairs 留痕;
- 修复稿引用塌方/非正文 → 拒绝,保留原稿(不退化);
- QA 首轮通过 → 零 LLM 修复调用;
- 池治理:同题同年跨源重复条目剔除(ACCURACY_DUPLICATE 源头)、
  用户导入缺 raw_citation 自动合成。
"""
import json

import pytest

import writing.orchestrator as orch
import writing.section_writer as sw
from retrieval.types import Paper, Source
from writing.orchestrator import SectionResult


@pytest.fixture(autouse=True)
def _small_pool_ruleset(monkeypatch):
    """9 篇的小池子过不了默认 quota(70-90 篇);放宽阈值专测修复闭环。"""
    monkeypatch.setenv("WRITING_QA_RULESET_OVERRIDES", json.dumps({
        "required_pass_rate": 0.0,
        "quota": {
            "total_min": 1, "total_max": 100,
            "core_ratio_min": 0.0, "recent_ratio_min": 0.0,
            "chinese_ratio_min": 0.0,
        },
    }))


_CN_NAMES = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十", "郑十一"]


def _paper(i: int, title: str | None = None) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=title or f"协同治理机制研究{i}",
        authors=[_CN_NAMES[i - 1]],
        journal="测试学报",
        year=2023,
        abstract=f"本文研究场景{i % 3}下的治理机制与协同问题。",
        raw_citation=f"{_CN_NAMES[i - 1]}. 协同治理机制研究{i}[J]. 测试学报, 2023.",
    )


# 三段正文,每段只引 1 篇 → article_lint 的段落密度检查(≥3 篇)必然 FAIL
_THIN_CONTENT = (
    "段一讨论治理结构。[lit_cnki_{:016x}] 张三(2023)认为结构变量重要。\n\n"
    "段二讨论过程变量。[lit_cnki_{:016x}] 李四(2023)关注动态演化。\n\n"
    "段三讨论绩效差异。[lit_cnki_{:016x}] 王五(2023)比较了不同情境。"
).format(1, 2, 3)

# 修复稿:每段 3 篇,作者年份夹注+锚点;作者名必须与池内真实作者一致
# (编号阶段的可见性检查按真实作者名+年份识别,认不出会注入元数据标签)
_FIXED_CONTENT = (
    "段一比较了治理结构的三种解释。张三(2023)[lit_cnki_{:016x}]强调结构变量,"
    "李四(2023)[lit_cnki_{:016x}]关注制度背景,赵六(2023)[lit_cnki_{:016x}]则补充了资源约束。\n\n"
    "段二比较过程变量的动态演化。李四(2023)[lit_cnki_{:016x}]与钱七(2023)[lit_cnki_{:016x}]"
    "结论相左,孙八(2023)[lit_cnki_{:016x}]提供了调和证据。\n\n"
    "段三比较绩效差异。王五(2023)[lit_cnki_{:016x}]与周九(2023)[lit_cnki_{:016x}]"
    "的分歧尚未弥合,吴十(2023)[lit_cnki_{:016x}]给出新的边界条件。"
).format(1, 2, 4, 2, 5, 6, 3, 7, 8)

# 密度达标的正文(每段 3 篇,真实作者名)
_RICH_CONTENT = (
    "段一比较三个流派。张三(2023)[lit_cnki_{:016x}]强调结构,李四(2023)[lit_cnki_{:016x}]"
    "关注制度,王五(2023)[lit_cnki_{:016x}]补充资源约束。\n\n"
    "段二比较方法差异。赵六(2023)[lit_cnki_{:016x}]用问卷,钱七(2023)[lit_cnki_{:016x}]"
    "用案例,孙八(2023)[lit_cnki_{:016x}]用仿真。\n\n"
    "段三比较结论边界。周九(2023)[lit_cnki_{:016x}]与吴十(2023)[lit_cnki_{:016x}]"
    "分歧明显,郑十一(2023)[lit_cnki_{:016x}]给出调和框架。"
).format(1, 2, 3, 4, 5, 6, 7, 8, 9)


def _sections(papers: list[Paper], content: str) -> list[SectionResult]:
    cited = list(dict.fromkeys(sw.CITE_RE.findall(content)))
    return [SectionResult(key="theme_1", title="协同治理机制",
                          content=content, citations=cited)]


def _patch_llm(monkeypatch, reply: str) -> list[dict]:
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return reply

    import llm.client as client
    monkeypatch.setattr(client, "messages_create", fake_create)
    return calls


def test_llm_repair_rewrites_thin_paragraphs_and_qa_converges(monkeypatch):
    """段落密度 FAIL → LLM 定向重写 → 复查通过;auto_repairs 留痕。"""
    papers = [_paper(i) for i in range(1, 10)]
    sections = _sections(papers, _THIN_CONTENT)
    llm_calls = _patch_llm(monkeypatch, _FIXED_CONTENT)

    payload = orch._run_post_write_qa(
        papers=papers, sections=sections,
        reference_list=orch.render_reference_list(papers), grade_map={},
    )
    assert llm_calls, "段落密度 FAIL 必须触发 LLM 定向修复"
    # 修复提示词必须带具体问题与文献清单
    assert "体检发现的问题" in llm_calls[0]["user"]
    assert "本章文献清单" in llm_calls[0]["user"]
    summary = payload["summary"]
    assert summary["overall"] != "fail", (
        "LLM 修复后复查应通过,实际: "
        + json.dumps(summary.get("issues", [])[:3], ensure_ascii=False)
    )
    assert summary.get("auto_repairs"), "修复记录必须进报告留痕"
    # 重写稿经编号回写,正文应已是 [N] 形态
    assert "[lit_" not in sections[0].content


def test_llm_repair_rejects_collapsed_rewrite(monkeypatch):
    """修复稿把引用改丢(塌方)→ 拒绝,保留原稿,问题如实上报。"""
    papers = [_paper(i) for i in range(1, 10)]
    sections = _sections(papers, _THIN_CONTENT)
    llm_calls = _patch_llm(
        monkeypatch,
        "修复稿丢了所有引用锚点,只剩光秃秃的论述,没有任何标注与夹注。",
    )

    payload = orch._run_post_write_qa(
        papers=papers, sections=sections,
        reference_list=orch.render_reference_list(papers), grade_map={},
    )
    assert llm_calls, "塌方测试也必须先尝试 LLM 修复"
    # 塌方稿被拒绝:原文(经确定性修复+编号的形态)仍在,塌方文本未混入
    assert "没有任何标注与夹注" not in sections[0].content, "塌方修复稿不得替换原稿"
    assert "讨论治理结构" in sections[0].content, "原稿正文必须保留"
    assert len(sections[0].citations) == 3, "原稿引用必须保留"
    assert payload["summary"]["overall"] == "fail", "修不掉的问题必须如实上报"


def test_llm_repair_not_invoked_when_qa_passes(monkeypatch):
    """QA 首轮通过 → 零 LLM 修复调用(闭环只在失败时启动)。"""
    papers = [_paper(i) for i in range(1, 10)]
    sections = _sections(papers, _RICH_CONTENT)
    calls = _patch_llm(monkeypatch, _RICH_CONTENT)

    payload = orch._run_post_write_qa(
        papers=papers, sections=sections,
        reference_list=orch.render_reference_list(papers), grade_map={},
    )
    assert calls == [], "QA 通过时不得触发任何 LLM 修复调用"
    assert payload["summary"]["overall"] != "fail"


def test_dedupe_pool_drops_same_title_year():
    """同题同年跨源重复 → 只留一条(ACCURACY_DUPLICATE 源头治理)。"""
    a = _paper(1)
    b = _paper(2)
    b.title = a.title  # 同题同年,不同 lit_id
    c = _paper(3, title="完全不同的另一篇")
    kept, dups = orch._dedupe_pool([a, b, c])
    assert [p.lit_id for p in kept] == [a.lit_id, c.lit_id]
    assert dups == [b.lit_id]


def test_backfill_user_raw_citation_synthesizes_from_metadata():
    """USER_IMPORTED 缺引文串 → 用自身元数据合成,不编造;已有不动。"""
    p1 = Paper(
        lit_id="lit_user_1", source=Source.USER_IMPORTED,
        title="水产品冷链物流研究", authors=["张三", "李四", "王五", "赵六"],
        journal="中国渔业经济", year=2024, raw_citation="",
    )
    p2 = Paper(
        lit_id="lit_user_2", source=Source.USER_IMPORTED,
        title="已有引文的", authors=["张三"], journal="学报", year=2024,
        raw_citation="张三. 已有引文的[J]. 学报, 2024.",
    )
    filled = orch._backfill_user_raw_citation([p1, p2])
    assert filled == 1
    assert p1.raw_citation == (
        "张三, 李四, 王五等. 水产品冷链物流研究[J]. 中国渔业经济, 2024."
    )
    assert p2.raw_citation.startswith("张三. 已有引文的"), "已有引文串不得改写"
