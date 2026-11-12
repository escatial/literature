# -*- coding: utf-8 -*-
"""2026-11-12 事故守护:glm-5.3 英文元推理混入正文通道(评审42第3章)。

事故形态(三层防线各拦一段):
1. 非流式重写/润色调用返回里,英文自我分析与中文正文交错(41 处英文块
   + Paragraph N final: 结构标签)→ normalize 层交错剥离;
2. 章节质量关卡词表漏检新词形(We need/Let's/Polish:)→
   ``_looks_like_non_article`` 词表扩容 + 结构标签/英文主导判据;
3. 润色轮把草稿过程当结果返回且引用锚点全保留 → humanizer 草稿倾倒回滚;
4. 并行流式写入收到网关缓冲单块(45766 字符英文回声+错位重复)→
   ``_section_stream_anomaly`` 长度/构成异常门 + 整段重试。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from llm.client import normalize_model_output  # noqa: E402
from humanizer.agent import (  # noqa: E402
    _looks_like_draft_dump,
    humanize_one_round,
)
from writing.section_writer import (  # noqa: E402
    _looks_like_non_article,
    _section_stream_anomaly,
    _strip_obvious_meta_lines,
)

# ---- 真实事故语料(评审42第3章,节选自 DB)----

_COT_HEAD = (
    "We need answer in Chinese, pure body text, academic polishing, tighten "
    "precision, no changing citations/data/years. Need infer request is revise "
    "provided text. Need preserve all citation markers exactly."
)
_MUSING_BLOCKS = [
    "Let's draft with precise academic language.",
    "Question: They asked only supplied text, likely wants polished version. "
    "We need output pure body, no intro. Fine.",
    "Let's ensure no citations moved away from claims. Keep exact citation "
    "marker sequence and association.",
]
_FINAL_PARAS = [
    "消费者支付意愿首先受可感知产品属性的影响，但这种关系随品类与消费情境而变化。"
    "围绕上述关系，侯子楠等（2019）对上海小龙虾消费者的调查表明，加工卫生、新鲜程度"
    "和味道等外观感知信息较易被纳入评价，质量安全认证的识别度则相对有限。",
    "当水产品质量难以仅凭外观判断时，标签与追溯信息可构成支付溢价形成的重要机制条件。"
    "Galati et al.（2021）在意大利与西班牙的比较中发现，消费者对鱼类生态标签的关注"
    "程度还受到利他倾向与社会文化语境的影响。",
    "品牌信任与渠道体验是产品信息转化为支付意愿的重要条件。Liu et al.（2023）基于"
    "544份消费者问卷，检验品牌信任、态度、主观规范与感知行为控制对区域公用品牌购买"
    "意向的影响。",
]
_POLLUTED_REPAIR_OUTPUT = "\n\n".join(
    [_COT_HEAD] + _MUSING_BLOCKS + _FINAL_PARAS
    + ["Potential concern: \"支付溢价成立的机制条件\" is a causal claim. "
       "Studies support conditional wording.",
       "Polish:\n- \"围绕这一关系\" maybe \"围绕上述关系\".",
       "Paragraph 1 final:\n" + _FINAL_PARAS[0],
       "Second final:\n" + _FINAL_PARAS[1]]
)
_CLEAN_SECTION = (
    "水产品品牌建设的研究大体沿三条路径展开。李燕（2013）认为北部湾经济区应通过"
    "品牌意识与科技支撑推动渔业品牌化。侯子楠等（2019）则从消费端验证了质量认证"
    "对支付意愿的支撑作用。综合来看,既有文献在行动主体与实施次序上各有侧重。"
)


class TestNormalizeInterleavedStrip:
    """normalize 层:英文元推理交错剥离。"""

    def test_strips_interleaved_cot_keeps_final_prose(self):
        cleaned = normalize_model_output(_POLLUTED_REPAIR_OUTPUT)
        # 全部英文推理块被剥离
        assert "We need answer" not in cleaned
        assert "Let's draft" not in cleaned
        assert "Potential concern" not in cleaned
        assert "Paragraph 1 final" not in cleaned
        assert "Second final" not in cleaned
        # 中文成稿正文保留
        for para in _FINAL_PARAS:
            assert para in cleaned

    def test_clean_output_untouched(self):
        assert normalize_model_output(_CLEAN_SECTION) == _CLEAN_SECTION

    def test_legacy_prefix_still_stripped(self):
        text = (
            "Let me think about this carefully.\n\nI need to revise the text.\n\n"
            + _CLEAN_SECTION
        )
        cleaned = normalize_model_output(text)
        assert "Let me think" not in cleaned
        assert _CLEAN_SECTION in cleaned

    def test_single_block_with_english_terms_untouched(self):
        """单块正文含英文术语(作者名等)不被误伤。"""
        text = (
            "Galati et al.（2021）在意大利与西班牙的比较中发现,消费者对鱼类生态"
            "标签的关注程度还受到利他倾向与社会文化语境的影响,这为追溯信息的"
            "支付溢价提供了跨市场证据。"
        )
        assert normalize_model_output(text) == text


class TestSectionNonArticleDetection:
    """章节质量关卡:新词形与结构标签识别。"""

    def test_we_need_prefix_detected(self):
        assert _looks_like_non_article(
            "We need answer in Chinese, pure body text, academic polishing."
        )

    def test_structural_labels_detected(self):
        dump = "\n\n".join([
            "Paragraph 1:\n候选句甲。",
            "Paragraph 2:\n候选句乙。",
            "Paragraph 3:\n候选句丙。",
            "Polish:\n- \"候选句甲\" maybe better.",
        ])
        assert _looks_like_non_article(dump)

    def test_english_dominant_detected(self):
        dump = " ".join(["We need output pure body text."] * 40)
        assert _looks_like_non_article(dump)

    def test_clean_article_passes(self):
        assert not _looks_like_non_article(_CLEAN_SECTION)

    def test_strip_obvious_meta_lines_removes_labels(self):
        text = (
            "Paragraph 1 final:\n" + _FINAL_PARAS[0]
            + "\nPolish:\n- \"围绕这一关系\" maybe better."
        )
        cleaned = _strip_obvious_meta_lines(text)
        assert "Paragraph 1 final" not in cleaned
        assert "Polish" not in cleaned
        assert "消费者支付意愿首先受" in cleaned


class TestStreamAnomalyGate:
    """流式首稿异常门:网关缓冲单块与英文回声。"""

    def test_overlong_single_chunk_detected(self):
        # 事故实测形态:45766 字符单块(上限 8000)
        corrupted = "WeWe need craft need craft Chinese academic Chinese academic " * 500
        assert _section_stream_anomaly(corrupted)

    def test_english_echo_detected(self):
        assert _section_stream_anomaly(
            "We need craft Chinese academic section 1200-2000 Chinese chars, "
            "5-8 paragraphs, each >=250 chars, each paragraph compare/juxtapose "
            "at least 3 different studies."
        )

    def test_clean_stream_passes(self):
        assert not _section_stream_anomaly(_CLEAN_SECTION)

    def test_empty_detected(self):
        assert _section_stream_anomaly("")


class TestHumanizeDraftDumpRollback:
    """humanizer:草稿过程倾倒回滚(引用锚点保留也拦)。"""

    def test_draft_dump_rolled_back(self):
        calls: list[str] = []

        def fake_call(system, user, max_tokens=8000, **kw):
            calls.append(user)
            return _POLLUTED_REPAIR_OUTPUT  # 含草稿标签+英文推理,锚点"保留"

        result, diff = humanize_one_round(
            round_no=1, text=_CLEAN_SECTION, caller=fake_call,
        )
        assert result == _CLEAN_SECTION, "草稿倾倒输出必须回滚保留原文"
        assert not diff.accepted
        assert "草稿过程" in diff.detail

    def test_clean_polish_accepted(self):
        polished = _CLEAN_SECTION.replace("大体沿三条路径展开", "大体沿三条主线展开")

        def fake_call(system, user, max_tokens=8000, **kw):
            return polished

        result, diff = humanize_one_round(
            round_no=1, text=_CLEAN_SECTION, caller=fake_call,
        )
        assert diff.accepted
        assert result == polished

    def test_dump_detector_signal(self):
        assert _looks_like_draft_dump(_POLLUTED_REPAIR_OUTPUT)
        assert not _looks_like_draft_dump(_CLEAN_SECTION)
