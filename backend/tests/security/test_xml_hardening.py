# -*- coding: utf-8 -*-
"""PubMed XML 解析加固(defusedxml)专项。

覆盖 Mimosa 门禁"XML 实体扩展"高危项的回归保护:
- billion laughs 实体扩展攻击:不展开、不挂起、不崩溃
- DTD 声明被拒绝,按非法 XML 走既有回退路径
- 正常 efetch XML 解析行为不回退
"""
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from retrieval.sources.pubmed import PubMedSource  # noqa: E402

# billion laughs:嵌套实体指数展开(真实展开需数 GB 内存/数分钟)
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<PubmedArticleSet><PubmedArticle><AbstractText>&lol9;</AbstractText></PubmedArticle></PubmedArticleSet>"""

# 仅 DTD、无实体引用(同样必须被拒)
DTD_ONLY = """<?xml version="1.0"?>
<!DOCTYPE PubmedArticleSet [<!ELEMENT foo ANY>]>
<PubmedArticleSet><PubmedArticle><AbstractText>hi</AbstractText></PubmedArticle></PubmedArticleSet>"""

# 正常 efetch 批量响应(回归基准)
NORMAL_BATCH = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation><PMID>12345</PMID></MedlineCitation>
    <Abstract><AbstractText Label="BACKGROUND">Cell fate study.</AbstractText></Abstract>
  </PubmedArticle>
</PubmedArticleSet>"""


def _source() -> PubMedSource:
    return PubMedSource(timeout=1.0)


class TestEntityExpansionBlocked:
    """实体扩展攻击:不扩不炸,走安全回退。"""

    def test_single_abstract_entity_attack_no_expansion(self):
        result = _source()._extract_abstract_from_xml(BILLION_LAUGHS)
        # defusedxml 拒绝实体 → 正则回退:引用原样保留在文本里,不展开
        assert result is not None
        assert "&lol" in result

    def test_batch_xml_entity_attack_returns_empty(self):
        assert _source()._extract_abstracts_from_batch_xml(BILLION_LAUGHS) == {}

    def test_batch_xml_dtd_only_returns_empty(self):
        assert _source()._extract_abstracts_from_batch_xml(DTD_ONLY) == {}

    def test_single_abstract_garbage_still_none(self):
        assert _source()._extract_abstract_from_xml("not xml at all <<<") is None


class TestNormalParsingUnchanged:
    """正常 XML 解析行为不回退。"""

    def test_batch_xml_normal(self):
        out = _source()._extract_abstracts_from_batch_xml(NORMAL_BATCH)
        assert out == {"12345": "BACKGROUND: Cell fate study."}

    def test_single_abstract_normal(self):
        xml = '<eSummaryResult><AbstractText>Hello world</AbstractText></eSummaryResult>'
        assert _source()._extract_abstract_from_xml(xml) == "Hello world"
