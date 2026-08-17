from __future__ import annotations

from urllib.parse import urlparse


ALLOWED_SOURCES = frozenset({"cnki", "wanfang", "cqvip", "pubmed", "openalex"})

SOURCE_HOSTS = {
    "cnki": ("cnki.net", "42.192.101.93"),
    "wanfang": ("wanfangdata.com.cn",),
    "cqvip": ("cqvip.com",),
    "pubmed": ("pubmed.ncbi.nlm.nih.gov",),
    "openalex": ("openalex.org",),
}


def validate_paper_provenance(source: str, lit_id: str, source_url: str) -> None:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"不允许的文献数据库: {source}")
    if not lit_id.startswith(f"lit_{source}_"):
        raise ValueError(f"文献 ID 与数据库来源不一致: {source}")
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError(f"缺少可验证的数据库记录链接: {source}")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in SOURCE_HOSTS[source]):
        raise ValueError(f"记录链接不属于数据库 {source}: {host}")


def has_valid_paper_provenance(paper: dict) -> bool:
    try:
        validate_paper_provenance(
            str(paper.get("source") or ""),
            str(paper.get("lit_id") or ""),
            str(paper.get("source_url") or ""),
        )
    except ValueError:
        return False
    return True
