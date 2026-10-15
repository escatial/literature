# -*- coding: utf-8 -*-
"""论断-证据审计与因果语言 lint 测试(纯规则,无 LLM)。"""
from qa.article_lint import check_article_text
from qa.claims import check_claim_audit, extract_claims
from retrieval.types import Paper, Source
from writing.orchestrator import SectionResult


def _paper(lit_id: str, authors=("张三",), year=2023) -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"研究{lit_id[-2:]}",
        authors=list(authors), journal="测试学报", year=year, abstract="摘要",
    )


def _section(content: str, citations: list[str], key: str = "theme_1") -> SectionResult:
    return SectionResult(key=key, title="章节", content=content, citations=citations)


# ---------- claim 解析 ----------

def test_extract_claims_maps_numbers_to_lit_ids():
    s = _section(
        "张三(2023)指出A有效[1]。李四(2024)证明B决定C[2]。\n王五(2025)发现两者相关[1][2]。",
        ["lit_a", "lit_b"],
    )
    claims = extract_claims([s], [])
    assert len(claims) == 3
    assert claims[0]["supporting_lit_ids"] == ["lit_a"]
    assert claims[1]["supporting_lit_ids"] == ["lit_b"]
    assert claims[2]["supporting_lit_ids"] == ["lit_a", "lit_b"]
    assert claims[1]["is_key_claim"] is True          # 含"证明""决定"
    assert claims[0]["is_key_claim"] is False         # 仅"指出"


# ---------- 审计:单一支撑/低严谨性 ----------

def test_audit_warns_on_single_source_key_claim():
    papers = [_paper("lit_a"), _paper("lit_b")]
    s = _section(
        "张三(2023)指出A有效[1]。李四(2024)证明B显著决定C[2]。",
        ["lit_a", "lit_b"],
    )
    result = check_claim_audit([s], papers)
    codes = [i.code for i in result.issues]
    assert "CLAIM_SINGLE_SOURCE_KEY" in codes
    assert result.status.value == "warn"
    assert result.metrics["single_source_key_claims"] == 1


def test_audit_clean_when_key_claims_multi_supported():
    papers = [_paper("lit_a"), _paper("lit_b")]
    s = _section("张三(2023)证明A显著有效[1][2]。", ["lit_a", "lit_b"])
    result = check_claim_audit([s], papers)
    assert result.issues == []
    assert result.status.value == "pass"
    assert result.metrics["claims_total"] == 1


def test_audit_flags_low_rigor_single_support():
    papers = [_paper("lit_a"), _paper("lit_b")]
    # citations 只含 lit_b → 它是全局第 1 条,正文锚点应为 [1]
    s = _section("李四(2024)证明B显著决定C[1]。", ["lit_b"])
    result = check_claim_audit([s], papers, rigor_map={"lit_b": 40.0})
    codes = [i.code for i in result.issues]
    assert "CLAIM_LOW_RIGOR_SUPPORT" in codes
    assert result.metrics["low_rigor_single_support"] == 1


def test_audit_never_fails_delivery():
    # 审计是建议不是门禁:再糟的情形也只有 WARN
    s = _section("李四(2024)证明B显著决定C[2]。", ["lit_b"])
    result = check_claim_audit([s], [_paper("lit_b")])
    assert result.status.value in {"warn", "pass"}


# ---------- 因果语言 lint ----------

def test_causal_overreach_warns():
    s = _section(
        "现有研究证明了数字技术导致绩效提升[1]。",
        ["lit_a"],
    )
    result = check_article_text([s], [_paper("lit_a")])
    codes = [i.code for i in result.issues]
    assert "ARTICLE_CAUSAL_OVERREACH" in codes
    issue = next(i for i in result.issues if i.code == "ARTICLE_CAUSAL_OVERREACH")
    assert issue.severity.value == "warn"             # 告警不阻断


def test_causal_reverse_form_also_warns():
    s = _section("数字化转型与绩效的因果关系得到了验证[1]。", ["lit_a"])
    result = check_article_text([s], [_paper("lit_a")])
    codes = [i.code for i in result.issues]
    assert "ARTICLE_CAUSAL_OVERREACH" in codes


def test_plain_correlation_language_not_flagged():
    s = _section("多项研究发现数字技术使用与绩效存在正相关[1]。", ["lit_a"])
    result = check_article_text([s], [_paper("lit_a")])
    codes = [i.code for i in result.issues]
    assert "ARTICLE_CAUSAL_OVERREACH" not in codes


def test_causal_check_skips_comment_section():
    s = _section("综合来看,现有研究证明了因果机制的存在。", [], key="comment")
    result = check_article_text([s], [_paper("lit_a")])
    codes = [i.code for i in result.issues]
    assert "ARTICLE_CAUSAL_OVERREACH" not in codes
