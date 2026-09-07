from __future__ import annotations

import hashlib
import re
from typing import Mapping


def normalize_doi(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    return value.rstrip(" .;，。")


def normalize_title(value: str | None) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", (value or "").lower())


def normalize_author(value: str | None) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", (value or "").lower())


def _identity_key(
    source: str,
    title: str,
    authors: list[str] | None,
    year: int | None,
    doi: str | None,
) -> str:
    source = str(source or "").lower()
    doi_key = normalize_doi(doi)
    if doi_key:
        return f"{source}|doi|{doi_key}"
    first_author = normalize_author((authors or [""])[0])
    return f"{source}|title|{normalize_title(title)}|author|{first_author}|year|{int(year or 0)}"


def build_lit_id(
    *,
    source: str,
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
) -> str:
    key = _identity_key(source, title, authors, year, doi)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"lit_{str(source).lower()}_{digest}"


def build_identity_key(
    *,
    source: str,
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
) -> str:
    return _identity_key(source, title, authors, year, doi)


# ============================================================================
# 跨源归一(方案 §1 "英文文献以 Crossref/OpenAlex/出版社页面和
# Google Scholar 可检索性为交叉来源")
# ============================================================================

def cross_source_key(
    *,
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
) -> str:
    """跨源归一身份键(不含 source 字段),用于 canonical_work 跨源合并。

    优先级:
      1. DOI(优先,跨源稳定)
      2. (title | first_author | year)归一组合

    两篇不同来源但 DOI 相同的文献,在 canonical 层视为同一篇。
    """
    doi_key = normalize_doi(doi)
    if doi_key:
        return f"cross|doi|{doi_key}"
    first_author = normalize_author((authors or [""])[0])
    return f"cross|title|{normalize_title(title)}|author|{first_author}|year|{int(year or 0)}"


def merge_papers(*, papers: list) -> dict | None:
    """把多源 papers 合并成一个 canonical 视图。

    输入: list[Paper]
    输出: dict 包含 canonical 字段 + sources(原始 paper_id 列表)。

    合并规则:
      - DOI 优先级最高,若任一 paper 有 DOI,以该 DOI 为准;
      - 期刊/卷期/页码采用「多源一致优先,不一致保留 None」;
      - 来源去重:同 source+同 source_record_id 只保留一条。
    """
    if not papers:
        return None

    # DOI 优先级
    doi = None
    for p in papers:
        d = getattr(p, "doi", None)
        if d:
            doi = d
            break

    title = ""
    for p in papers:
        t = (getattr(p, "title", "") or "").strip()
        if t:
            title = t
            break

    # 取第一个非空值
    def first_non_empty(attr: str):
        for p in papers:
            v = getattr(p, attr, None)
            if v not in (None, "", 0):
                return v
        return None

    journal = first_non_empty("journal") or ""
    year = first_non_empty("year") or 0
    volume = first_non_empty("volume")
    issue = first_non_empty("issue")
    pages = first_non_empty("pages")
    abstract = first_non_empty("abstract")

    # authors:取最最完整的那一份(按长度)
    best_authors: list[str] = []
    for p in papers:
        au = getattr(p, "authors", None) or []
        if len(au) > len(best_authors):
            best_authors = au

    # 来源集合
    sources: list[dict] = []
    seen = set()
    for p in papers:
        key = (str(getattr(p, "source", "")), getattr(p, "lit_id", ""))
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            "source": str(getattr(p, "source", "")),
            "lit_id": getattr(p, "lit_id", ""),
            "source_url": getattr(p, "source_url", "") or "",
        })

    return {
        "title": title,
        "authors": best_authors,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "abstract": abstract,
        "sources": sources,
        "canonical_key": cross_source_key(
            title=title, authors=best_authors, year=year, doi=doi,
        ),
    }


def repair_paper_fields(record: Mapping[str, object]) -> dict:
    result = dict(record)
    if not result.get("abstract") and result.get("abstract_text"):
        result["abstract"] = result["abstract_text"]
    year = result.get("year")
    if not isinstance(year, int) or year <= 0:
        citation = str(result.get("raw_citation") or "")
        years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", citation)
        if years:
            result["year"] = int(years[-1])
    if not result.get("raw_citation"):
        authors = ", ".join(str(x) for x in result.get("authors") or [])
        title = str(result.get("title") or "").strip()
        journal = str(result.get("journal") or "").strip()
        year_value = int(result.get("year") or 0)
        doi = normalize_doi(str(result.get("doi") or ""))
        parts = [x for x in (authors, title, journal, str(year_value or "")) if x]
        if doi:
            parts.append(f"DOI:{doi}")
        elif result.get("source_url"):
            parts.append(str(result["source_url"]))
        if parts:
            result["raw_citation"] = ". ".join(parts) + "."
    return result


def validate_paper_identity(record: Mapping[str, object]) -> None:
    """入库前校验:title / authors / year / abstract 缺一不可。

    任何字段缺失或不合规,直接 raise ValueError(由 api 层转为 422)。
    标题为空格的 paper 没有资格入库——screening 与 QA 阶段会用到的
    所有字段都必须存在。
    """
    repaired = repair_paper_fields(record)
    title = str(repaired.get("title") or "").strip()
    if not title:
        raise ValueError("文献缺少标题")
    authors = list(repaired.get("authors") or [])
    if not authors:
        raise ValueError("文献缺少作者")
    if int(repaired.get("year") or 0) <= 0:
        raise ValueError("文献缺少有效年份")
    if not repaired.get("abstract") and not repaired.get("abstract_text"):
        raise ValueError("文献缺少摘要")
    source = str(repaired.get("source") or "")
    expected = build_lit_id(
        source=source,
        title=title,
        authors=authors,
        year=int(repaired.get("year") or 0),
        doi=str(repaired.get("doi") or ""),
    )
    if str(repaired.get("lit_id") or "") != expected:
        raise ValueError("文献 ID 与作者、年份、标题、DOI 身份指纹不一致")


def repair_database() -> dict[str, int]:
    from db.models import PaperModel, RetrievalHistoryModel, RetrievalTaskModel, ReviewModel
    from db.session import SessionLocal

    def replace_ids(value: object, mapping: dict[str, str]) -> object:
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [replace_ids(item, mapping) for item in value]
        if isinstance(value, dict):
            return {key: replace_ids(item, mapping) for key, item in value.items()}
        return value

    merged = 0
    repaired = 0
    id_mapping: dict[str, str] = {}
    with SessionLocal() as db:
        rows = list(db.query(PaperModel).order_by(PaperModel.created_at.asc()).all())
        # v8.1:同一文献可在不同任务各存一行,去重 key 从全局 identity
        # 改为 (task_key, identity) —— 只在「同一任务内」合并重复行,
        # 跨任务同 identity 属于合法共存,不得互删。
        canonical: dict[tuple[str, str], PaperModel] = {}
        for row in rows:
            payload = repair_paper_fields({
                key: getattr(row, key)
                for key in (
                    "source", "title", "authors", "journal", "year", "abstract",
                    "abstract_text", "doi", "source_url", "raw_citation",
                )
            })
            identity = build_identity_key(
                source=str(payload.get("source") or ""),
                title=str(payload.get("title") or ""),
                authors=list(payload.get("authors") or []),
                year=int(payload.get("year") or 0),
                doi=str(payload.get("doi") or ""),
            )
            new_id = build_lit_id(
                source=str(payload.get("source") or ""),
                title=str(payload.get("title") or ""),
                authors=list(payload.get("authors") or []),
                year=int(payload.get("year") or 0),
                doi=str(payload.get("doi") or ""),
            )
            task_key = row.task_id or "__legacy__"
            existing = canonical.get((task_key, identity))
            if existing is None:
                canonical[(task_key, identity)] = row
                id_mapping[row.lit_id] = new_id
                row.identity_key = identity
                row.lit_id = new_id
                for key, value in payload.items():
                    setattr(row, key, value)
                repaired += 1
                continue
            id_mapping[row.lit_id] = existing.lit_id
            for key, value in payload.items():
                current = getattr(existing, key)
                if not current and value:
                    setattr(existing, key, value)
            db.delete(row)
            merged += 1
        db.flush()
        for task in db.query(RetrievalTaskModel).all():
            task.papers = replace_ids(task.papers or [], id_mapping)
        for history in db.query(RetrievalHistoryModel).all():
            history.papers_snapshot = replace_ids(history.papers_snapshot or [], id_mapping)
        for review in db.query(ReviewModel).all():
            review.sections = replace_ids(review.sections or [], id_mapping)
            review.screened_out_ids = replace_ids(review.screened_out_ids or [], id_mapping)
        db.commit()
    return {"repaired": repaired, "merged": merged, "mapped": len(id_mapping)}
