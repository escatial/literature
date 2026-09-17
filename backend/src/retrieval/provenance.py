from __future__ import annotations

from urllib.parse import urlparse
import re


ALLOWED_SOURCES = frozenset({"cnki", "pubmed", "openalex"})

SOURCE_HOSTS = {
    "cnki": ("cnki.net", "42.192.101.93"),
    "pubmed": ("pubmed.ncbi.nlm.nih.gov",),
    "openalex": ("openalex.org",),
}


def derive_paper_provenance(source: str, lit_id: str, source_url: str) -> dict | None:
    """从已有官方记录 URL 派生最小可追溯信息。

    只对 PubMed/OpenAlex 官方域名生效，且先复用
    ``validate_paper_provenance``；不会凭空构造不存在的记录地址。用于
    历史快照/前端回传丢失 provenance 字段时恢复溯源链。
    """
    source = str(source or "").strip().lower()
    try:
        validate_paper_provenance(source, str(lit_id or ""), str(source_url or ""))
    except ValueError:
        return None
    parsed = urlparse(source_url)
    record_id = ""
    if source == "pubmed":
        match = re.search(r"/(\d+)(?:/)?$", parsed.path or "")
        record_id = match.group(1) if match else ""
        api_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    elif source == "openalex":
        record_id = (parsed.path or "").rstrip("/").split("/")[-1]
        api_url = "https://api.openalex.org/works"
    else:
        return None
    if not record_id:
        return None
    return {
        "source": source,
        "record_id": record_id,
        "source_url": source_url,
        "api_url": api_url,
        "derived": True,
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
