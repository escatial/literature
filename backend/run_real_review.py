import json
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR / "src"))

from retrieval.history_service import get_history
from retrieval.types import Paper, normalize_source
from retrieval.provenance import derive_paper_provenance
from writing.orchestrator import generate_review

history = get_history(7)
if not history:
    raise SystemExit("history 7 not found")
papers = []
for raw in history.get("papers_snapshot", []):
    p = dict(raw)
    papers.append(Paper(
        lit_id=str(p["lit_id"]), source=normalize_source(p.get("source")),
        title=p.get("title") or "", authors=list(p.get("authors") or []),
        journal=p.get("journal") or "", year=int(p.get("year") or 0),
        volume=p.get("volume"), issue=p.get("issue"), pages=p.get("pages"),
        abstract=p.get("abstract") or p.get("abstract_text"), doi=p.get("doi"),
        source_url=p.get("source_url") or "", cited_by_count=int(p.get("cited_by_count") or 0),
        journal_level=p.get("journal_level"), relevance_score=p.get("relevance_score"),
        provenance=p.get("provenance") or derive_paper_provenance(
            normalize_source(p.get("source")).value,
            str(p["lit_id"]),
            p.get("source_url") or "",
        ), raw_citation=p.get("raw_citation"),
    ))
result = generate_review(topic=history["topic"], papers=papers, classify_mode="theme", do_screening=True)
out = Path(__file__).parent / "outputs"
out.mkdir(exist_ok=True)
payload = {
    "topic": result.topic, "history_id": history["id"], "input_count": len(papers),
    "plan_papers": sum(len(g.lit_ids) for g in result.groups),
    "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in result.groups],
    "sections": [{"key": s.key, "title": s.title, "content": s.content, "citations": s.citations, "dropped_citations": s.dropped_citations} for s in result.sections],
    "reference_list": result.reference_list, "screened_out_ids": result.screened_out_ids,
    "dropped_citations": result.dropped_citations, "qa": result.qa_report,
}
(out / "latest_writing_result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
(out / "latest_writing.md").write_text("\n\n".join(f"# {s['title']}\n\n{s['content']}" for s in payload["sections"]) + "\n\n## References\n\n" + payload["reference_list"], encoding="utf-8")
print(json.dumps({"groups": [g["name"] for g in payload["groups"]], "sections": len(payload["sections"]), "citations": [len(s["citations"]) for s in payload["sections"]], "qa": (payload["qa"] or {}).get("summary", {}).get("overall")}, ensure_ascii=False))
