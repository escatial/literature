"""section_writer 单元测试。"""

from unittest.mock import patch

from src.retrieval.types import Paper, Source
from src.writing.classifier import Group
from src.writing.section_writer import write_section
from src.writing.templates import SECTIONS


def _paper(lit_id: str) -> Paper:
    return Paper(
        lit_id=lit_id,
        source=Source.OPENALEX,
        title=f"T-{lit_id}",
        authors=["Alice", "Bob"],
        journal="J",
        year=2024,
        volume=None,
        issue=None,
        pages=None,
        abstract=None,
        doi=None,
        source_url=None,
        cited_by_count=None,
    )


_LIT_A = "lit_" + "a" * 16
_LIT_B = "lit_" + "b" * 16
_LIT_GHOST = "lit_" + "0" * 16


class TestWriteSection:
    def setup_method(self):
        self.section = next(s for s in SECTIONS if s.key == "themes")
        self.papers = [_paper(_LIT_A), _paper(_LIT_B)]
        self.groups = [Group(name="国内研究", lit_ids=[_LIT_A, _LIT_B])]

    def test_valid_citations_kept(self):
        content = f"研究A [{_LIT_A}] 提出X，研究B [{_LIT_B}] 进一步扩展。"
        with patch(
            "src.writing.section_writer.messages_create", return_value=content
        ) as m:
            res = write_section(self.section, "topic", self.groups, self.papers)
        assert res.citations == [_LIT_A, _LIT_B]
        assert res.dropped_citations == []
        assert f"[{_LIT_A}]" in res.content
        assert f"[{_LIT_B}]" in res.content
        m.assert_called_once()

    def test_hallucinated_citation_stripped(self):
        content = f"真实 [{_LIT_A}]，幻觉 [{_LIT_GHOST}]，又 [{_LIT_B}]。"
        with patch(
            "src.writing.section_writer.messages_create", return_value=content
        ):
            res = write_section(self.section, "topic", self.groups, self.papers)
        assert res.citations == [_LIT_A, _LIT_B]
        assert res.dropped_citations == [_LIT_GHOST]
        assert _LIT_GHOST not in res.content
        assert f"[{_LIT_A}]" in res.content
        assert f"[{_LIT_B}]" in res.content

    def test_duplicate_citations_deduped_in_list_but_kept_in_text(self):
        content = f"一 [{_LIT_A}] 二 [{_LIT_A}] 三 [{_LIT_A}]."
        with patch(
            "src.writing.section_writer.messages_create", return_value=content
        ):
            res = write_section(self.section, "topic", self.groups, self.papers)
        assert res.citations == [_LIT_A]  # 列表去重
        assert res.content.count(f"[{_LIT_A}]") == 3  # 正文保留

    def test_no_citations(self):
        content = "本章不引用文献。"
        with patch(
            "src.writing.section_writer.messages_create", return_value=content
        ):
            res = write_section(self.section, "topic", self.groups, self.papers)
        assert res.citations == []
        assert res.dropped_citations == []

    def test_section_key_and_title(self):
        with patch(
            "src.writing.section_writer.messages_create", return_value="x"
        ):
            res = write_section(self.section, "topic", self.groups, self.papers)
        assert res.key == "themes"
        assert res.title == self.section.title
