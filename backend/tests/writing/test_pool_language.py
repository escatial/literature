# -*- coding: utf-8 -*-
"""文献池语言判定回归(2026-10-26 事故根因 #1)。

事故形态:54 中文 + 36 英文的混合池,旧「题名字符占比」口径被长英文
题名稀释(cn_share≈0.24 → 误判 en 池),主题轴被要求英文命名。
修复后按题名篇数判定:含 CJK 字符的题名篇数 / 有题名总篇数 ≥ 0.5
判 zh;题名全缺失回退来源库判定;env WRITING_POOL_LANGUAGE 可强制
覆盖(auto|zh|en,默认 auto)。
"""
import writing.classifier as clf
from retrieval.types import Paper, Source


_ZH_TITLES = [
    "水产养殖户绿色生产技术采纳行为研究",
    "生鲜电商平台消费者购买意愿影响因素研究",
    "区域公用品牌建设路径与主体协同机制研究",
    "水产品冷链物流配送网络优化研究",
    "农民专业合作社社员参与行为及其影响因素分析",
]
_EN_TITLES = [
    "Consumer preferences for traceable seafood products in retail markets",
    "Digital transformation and omnichannel marketing in the seafood industry",
    "Sustainable supply chain governance under institutional pressures",
]


def _paper(i: int, title: str, source: Source) -> Paper:
    return Paper(
        lit_id=f"lit_{source.value}_{i:016x}",
        source=source,
        title=title,
        authors=["作者" if source == Source.CNKI else "Author"],
        journal="测试学报",
        year=2024,
        abstract="本文研究相关问题。"
        if source == Source.CNKI else "This paper studies related questions.",
    )


def _mixed_pool(n_zh: int, n_en: int) -> list[Paper]:
    papers = [
        _paper(i, f"{_ZH_TITLES[i % len(_ZH_TITLES)]}{i}", Source.CNKI)
        for i in range(n_zh)
    ]
    papers += [
        _paper(1000 + i, f"{_EN_TITLES[i % len(_EN_TITLES)]} study number {i}",
               Source.OPENALEX)
        for i in range(n_en)
    ]
    return papers


def _legacy_char_share_verdict(papers: list[Paper]) -> str:
    """复刻事故版「题名字符占比 + 来源占比」旧口径,证明测试池确为
    旧口径误判的形态(回归价值自证)。"""
    cn_chars = 0
    total_chars = 0
    for paper in papers:
        text = (paper.title or "").strip()
        if not text:
            continue
        total_chars += len(text)
        cn_chars += sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    cn_share = (cn_chars / total_chars) if total_chars else 0
    src_share = sum(
        1 for paper in papers
        if paper.source in (Source.CNKI, Source.USER_IMPORTED)
    ) / len(papers)
    return "zh" if 0.6 * cn_share + 0.4 * src_share >= 0.4 else "en"


def test_incident_pool_54_zh_36_en_is_zh(monkeypatch):
    """事故池复现:54/90 中文篇数、长英文题名稀释字符占比,必须判 zh。"""
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    papers = _mixed_pool(54, 36)
    assert _legacy_char_share_verdict(papers) == "en", (
        "测试池必须构造成旧口径误判 en 的形态,否则无回归价值"
    )
    assert clf._pool_dominant_language(papers) == "zh"


def test_boundary_ratio_counts_as_zh(monkeypatch):
    """45/90 = 0.50 恰在边界:≥0.5 含入,判 zh(宁 zh 勿 en —— 英文池
    误判 zh 只影响措辞,中文池误判 en 会让轴命名整体脱轨)。"""
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    assert clf._pool_dominant_language(_mixed_pool(45, 45)) == "zh"


def test_below_boundary_is_en(monkeypatch):
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    assert clf._pool_dominant_language(_mixed_pool(44, 46)) == "en"


def test_all_english_pool_is_en(monkeypatch):
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    assert clf._pool_dominant_language(_mixed_pool(0, 10)) == "en"
    assert clf._pool_dominant_language(_mixed_pool(10, 0)) == "zh"


def test_fallback_to_source_when_no_titles(monkeypatch):
    """题名全缺失时回退来源库判定(CNKI/用户导入视为中文)。"""
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    cn = [
        Paper(lit_id=f"lit_cnki_{i:016x}", source=Source.CNKI, title="",
              authors=["作者"], journal="j", year=2024, abstract="x")
        for i in range(5)
    ]
    en = [
        Paper(lit_id=f"lit_openalex_{i:016x}", source=Source.OPENALEX, title="",
              authors=["Author"], journal="j", year=2024, abstract="x")
        for i in range(5)
    ]
    assert clf._pool_dominant_language(cn) == "zh"
    assert clf._pool_dominant_language(en) == "en"


def test_env_override_forces_language(monkeypatch):
    """env 三种取值:zh/en 强制覆盖,auto 与未设置按篇数自动判定。"""
    en_pool = _mixed_pool(0, 10)
    zh_pool = _mixed_pool(10, 0)
    mixed_pool = _mixed_pool(54, 36)

    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    assert clf._pool_dominant_language(en_pool) == "en", "未设置 env 时按自动判定"

    monkeypatch.setenv("WRITING_POOL_LANGUAGE", "zh")
    assert clf._pool_dominant_language(en_pool) == "zh", "强制 zh 覆盖自动判定"

    monkeypatch.setenv("WRITING_POOL_LANGUAGE", "en")
    assert clf._pool_dominant_language(zh_pool) == "en", "强制 en 覆盖自动判定"

    monkeypatch.setenv("WRITING_POOL_LANGUAGE", "auto")
    assert clf._pool_dominant_language(mixed_pool) == "zh"
    assert clf._pool_dominant_language(en_pool) == "en"

    monkeypatch.setenv("WRITING_POOL_LANGUAGE", "bogus-value")
    assert clf._pool_dominant_language(mixed_pool) == "zh", "非法取值回退自动判定"
