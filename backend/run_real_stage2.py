import json
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE / "src"))

from retrieval.history_service import get_history
from retrieval.types import Paper, normalize_source
from retrieval.provenance import derive_paper_provenance
from writing.classifier import Group
from writing.orchestrator import generate_review, build_review_blueprint

h = get_history(7)
old = json.loads((BASE / "outputs/latest_writing_result.json").read_text(encoding="utf-8"))
groups = []
for g in old.get("groups", []):
    name = "应急配送协同优化" if g["name"] == "应急物资配送车辆与多一" else g["name"]
    name = "协同配送路径优化" if "/" in name else name
    groups.append(Group(name=name, lit_ids=list(g["lit_ids"])))
papers = []
for x in h["papers_snapshot"]:
    source = normalize_source(x.get("source"))
    source_url = x.get("source_url") or ""
    papers.append(Paper(
        lit_id=x["lit_id"], source=source,
        title=x.get("title") or "", authors=x.get("authors") or [],
        journal=x.get("journal") or "", year=int(x.get("year") or 0),
        abstract=x.get("abstract") or x.get("abstract_text"),
        doi=x.get("doi"), source_url=source_url,
        provenance=x.get("provenance") or derive_paper_provenance(source.value, x["lit_id"], source_url),
        raw_citation=x.get("raw_citation"),
    ))
by_id = {p.lit_id: p for p in papers}
selected = [by_id[lid] for g in groups for lid in g.lit_ids if lid in by_id]
result = generate_review(topic=h["topic"], papers=selected, classify_mode="theme", do_screening=False, confirmed_groups=groups)
blueprint = build_review_blueprint(h["topic"], selected, "theme", groups)
payload = {
    "topic": result.topic, "history_id": 7, "input_count": len(selected),
    "writing_pool_count": sum(len(g.lit_ids) for g in result.groups),
    "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in result.groups],
    "sections": [{"key": s.key, "title": s.title, "content": s.content, "citations": s.citations, "dropped_citations": s.dropped_citations} for s in result.sections],
    "reference_list": result.reference_list, "qa": result.qa_report, "blueprint": blueprint,
}
out = BASE / "outputs"
(out / "stage2_real_result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
(out / "stage2_real_writing.md").write_text("\n\n".join(f"# {s['title']}\n\n{s['content']}" for s in payload["sections"]) + "\n\n## References\n\n" + payload["reference_list"], encoding="utf-8")
print(json.dumps({"groups": [g["name"] for g in payload["groups"]], "sections": len(payload["sections"]), "citations": [len(s["citations"]) for s in payload["sections"]], "qa": (payload["qa"] or {}).get("summary", {}).get("overall")}, ensure_ascii=False))
