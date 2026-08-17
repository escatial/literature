"""回填数据库文献摘要: OpenAlex 按 work id 拉单篇, PubMed 走 NCBI eutils。

用法: 在 backend 目录下 `python _backfill_abs.py`。
只更新 papers 表中 abstract 为空的记录, 已提交的摘要会保留。
"""
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from sqlalchemy import select

from db.models import PaperModel
from db.session import SessionLocal
from src.retrieval.openalex_adapter import _rebuild_abstract

LOG_PATH = Path(__file__).resolve().parent / "_backfill_log.txt"
_lines: list[str] = []


def log(*args):
    line = " ".join(str(a) for a in args)
    _lines.append(line)
    print(line, flush=True)
    LOG_PATH.write_text("\n".join(_lines), encoding="utf-8")


OPENALEX_BASE = "https://api.openalex.org/works"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def fetch_openalex(wid: str) -> str | None:
    """按 OpenAlex work id 拉单篇(带指数退避), 返回重建后的摘要。"""
    url = f"{OPENALEX_BASE}/{wid}"
    for attempt in range(3):
        try:
            r = httpx.get(url, timeout=60.0)
            r.raise_for_status()
            w = r.json()
            return _rebuild_abstract(w.get("abstract_inverted_index"))
        except Exception as e:
            log(f"  openalex {wid} attempt{attempt + 1} 失败: {e}")
            time.sleep(2 ** attempt)
    return None


def fetch_pubmed(pmid: str) -> str | None:
    """按 PMID 拉 XML, 拼接 AbstractText 节点为纯文本摘要。"""
    try:
        r = httpx.get(
            EUTILS,
            params={"db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "xml"},
            timeout=60.0,
        )
        r.raise_for_status()
        texts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", r.text, re.S)
        if not texts:
            return None
        parts = []
        for t in texts:
            t = re.sub(r"<[^>]+>", "", t)
            t = html.unescape(t).strip()
            if t:
                parts.append(t)
        return " ".join(parts) or None
    except Exception as e:
        log(f"  pubmed {pmid} 失败: {e}")
        return None


def backfill_one(row: PaperModel):
    if row.source == "openalex" and row.lit_id.startswith("lit_openalex_"):
        wid = row.lit_id[len("lit_openalex_"):].upper()  # w4385732350 -> W4385732350
        abstract = fetch_openalex(wid)
    elif row.source == "pubmed" and row.lit_id.startswith("lit_pubmed_"):
        pmid = row.lit_id[len("lit_pubmed_"):]
        abstract = fetch_pubmed(pmid)
    else:
        log(f"  跳过不支持来源: {row.lit_id} ({row.source})")
        return False
    if abstract and abstract.strip():
        row.abstract = abstract.strip()
        log(f"  回填成功: {row.lit_id} 摘要{len(abstract.strip())}字")
        return True
    log(f"  回填失败(无摘要): {row.lit_id}")
    return False


def main() -> None:
    with SessionLocal() as db:
        rows = db.execute(select(PaperModel)).scalars().all()
        log(f"总文献: {len(rows)}")
        missing = [r for r in rows if not (r.abstract or "").strip()]
        log(f"缺摘要: {len(missing)}")

        if not missing:
            log("无需回填")
            return

        ok = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(backfill_one, missing))
            ok = sum(1 for r in results if r)
            time.sleep(0.4)

        db.commit()
        log(f"\n本次回填成功: {ok}/{len(missing)}")
        filled = sum(1 for r in rows if (r.abstract or "").strip())
        log(f"回填后: 有摘要 {filled}/{len(rows)}")


if __name__ == "__main__":
    main()
