from retrieval.types import Paper, Source
from writing.citeproc_renderer import format_citation_via_citeproc
from writing.orchestrator import render_reference_list


def _paper(**kwargs):
    values = dict(
        lit_id="lit_test",
        source=Source.OPENALEX,
        title="Test Article",
        authors=["John Smith", "Jane Doe"],
        journal="Test Journal",
        year=2024,
        doi="10.1000/test",
        source_url="https://openalex.org/W123",
    )
    values.update(kwargs)
    return Paper(**values)


def test_openalex_doi_provenance_is_not_online_only():
    rendered = format_citation_via_citeproc(_paper(volume="12", issue="3", pages="101-110"))
    assert "[J]." in rendered
    assert "[J/OL]" not in rendered


def test_pubmed_with_formal_issue_metadata_is_not_online_only():
    rendered = format_citation_via_citeproc(
        _paper(
            source=Source.PUBMED,
            source_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            volume="8",
            issue="2",
            pages="20-29",
        )
    )
    assert "[J]." in rendered
    assert "[J/OL]" not in rendered


def test_partial_formal_journal_metadata_does_not_duplicate_type_marker():
    rendered = format_citation_via_citeproc(_paper(volume="10", issue="1"))
    assert "[J]." in rendered
    assert "[J/J]" not in rendered


def test_author_date_style_does_not_duplicate_type_marker():
    rendered = format_citation_via_citeproc(
        _paper(volume="10", issue="1"),
        style_id="china-national-standard-gb-t-7714-2025-author-date",
    )
    assert "[J]" in rendered
    assert "[J/J]" not in rendered


def test_reference_list_route_renders_formal_english_journal_as_j():
    rendered = render_reference_list([_paper(volume="10", issue="1")])
    assert rendered.startswith("[1] ")
    assert "[J]." in rendered
    assert "[J/OL]" not in rendered


def test_online_first_without_publication_details_uses_journal_marker():
    rendered = format_citation_via_citeproc(_paper())
    assert "[J]" in rendered
    assert "[J/OL]" not in rendered


def test_reference_list_omits_doi_for_structured_english_record():
    rendered = render_reference_list([_paper(volume="10", issue="1", pages="1-9")])
    assert "DOI" not in rendered.upper()
    assert "10.1000/test" not in rendered


def test_reference_list_omits_doi_from_cnki_raw_citation():
    rendered = render_reference_list([
        _paper(
            source=Source.CNKI,
            raw_citation="张三. 中文题名[J]. 测试期刊, 2024, 10(1): 1-9. DOI:10.1234/example.2024.1",
        )
    ])
    assert "DOI" not in rendered.upper()
    assert "10.1234/example" not in rendered
