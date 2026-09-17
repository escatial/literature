from qa import citation_format
from retrieval.types import Paper, Source


def _paper(**kwargs):
    values = dict(
        lit_id="lit_test",
        source=Source.OPENALEX,
        title="Test Article",
        authors=["John Smith"],
        journal="Test Journal",
        year=2024,
        volume="12",
        issue="3",
        pages="101-110",
        doi="10.1000/test",
    )
    values.update(kwargs)
    return Paper(**values)


def test_citation_format_flags_online_marker_for_formal_journal(monkeypatch):
    paper = _paper()
    monkeypatch.setattr(
        citation_format,
        "_render_papers",
        lambda papers, style_id: {paper.lit_id: "Smith J. Test Article[J/OL]. Test Journal, 2024."},
    )
    result = citation_format.check_citation_format([paper])
    assert result.status.value == "fail"
    assert any(issue.code == "CITATION_FORMAT_WRONG_MEDIUM" for issue in result.issues)


def test_citation_format_allows_online_marker_without_publication_details(monkeypatch):
    paper = _paper(volume=None, issue=None, pages=None)
    monkeypatch.setattr(
        citation_format,
        "_render_papers",
        lambda papers, style_id: {
            paper.lit_id: "Smith J. Test Article[J/OL]. Test Journal, 2024."
        },
    )
    result = citation_format.check_citation_format([paper])
    assert not any(issue.code == "CITATION_FORMAT_WRONG_MEDIUM" for issue in result.issues)
