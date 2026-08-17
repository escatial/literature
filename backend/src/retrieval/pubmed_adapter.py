from __future__ import annotations

import logging
import re

import httpx

from .types import Paper, Source


log = logging.getLogger(__name__)


class PubMedAdapter:
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def search(
        self,
        query: str,
        year_range: tuple[int, int],
        per_page: int = 50,
    ) -> list[Paper]:
        term = f"({query}) AND ({year_range[0]}:{year_range[1]}[pdat])"
        with httpx.Client(timeout=self.timeout) as client:
            search_response = client.get(
                f"{self.BASE}/esearch.fcgi",
                params={"db": "pubmed", "term": term, "retmode": "json", "retmax": min(per_page, 200)},
            )
            search_response.raise_for_status()
            ids = search_response.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []
            summary_response = client.get(
                f"{self.BASE}/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
            )
            summary_response.raise_for_status()
        result = summary_response.json().get("result", {})
        return [self._parse(uid, result.get(uid) or {}) for uid in ids if result.get(uid)]

    def _parse(self, uid: str, record: dict) -> Paper:
        date_text = str(record.get("pubdate") or record.get("sortpubdate") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
        article_ids = record.get("articleids") or []
        doi = next(
            (str(item.get("value")) for item in article_ids if item.get("idtype") == "doi"),
            None,
        )
        return Paper(
            lit_id=f"lit_pubmed_{uid}",
            source=Source.PUBMED,
            title=str(record.get("title") or "").strip(),
            authors=[str(author.get("name")) for author in record.get("authors") or [] if author.get("name")],
            journal=str(record.get("fulljournalname") or record.get("source") or ""),
            year=int(year_match.group(0)) if year_match else 0,
            volume=str(record.get("volume") or "") or None,
            issue=str(record.get("issue") or "") or None,
            pages=str(record.get("pages") or "") or None,
            doi=doi,
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
        )
