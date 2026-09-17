"""Sentence-scoped audit: every in-text author-year mention must be accompanied
by a numeric citation marker within the same sentence, and fallback filler
sentences must not reach the delivered draft.

GB/T 7714 numeric style legitimately places [n] at the end of a clause, so the
window is the enclosing sentence rather than a fixed character count.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RESULT = Path(__file__).resolve().parent / "outputs" / "latest_writing_result.json"

MENTION = re.compile(
    r"([\u4e00-\u9fff]{2,6}(?:等)?)[\（\(]\s*(\d{4})[a-z]?\s*[\）\)]"
    r"|([A-Z][A-Za-z\-']+(?:\s+et\s+al\.?)?)\s*[\（\(]\s*(\d{4})[a-z]?\s*[\）\)]"
)
NUM_CITE = re.compile(r"\[\d+(?:\s*[-,，]\s*\d+)*\]")
FALLBACK = ("补充证据显示", "补充证据表明", "此外还有研究指出")


def main() -> int:
    data = json.loads(RESULT.read_text(encoding="utf-8"))
    bad = 0
    for i, s in enumerate(data.get("sections") or [], 1):
        content = s.get("content") or ""
        sentences = re.split(r"(?<=[。；;!?！？])", content)
        unbound = []
        total = 0
        seen_names: set[str] = set()
        for sent in sentences:
            names = [m.group(0) for m in MENTION.finditer(sent)]
            if not names:
                continue
            total += len(names)
            has_marker = bool(NUM_CITE.search(sent))
            for name in names:
                if not has_marker:
                    unbound.append((name, sent.strip()))
                seen_names.add(name)
        fb = [f for f in FALLBACK if f in content]
        flag = "OK " if not unbound and not fb else "BAD"
        print(f"[{flag}] 章节{i} {s.get('title')}: 作者年份 {total} 处, 所在句无编号 {len(unbound)} 处, 兜底句 {fb}")
        for name, sent in unbound:
            print(f"        {name} || {sent[:110]}")
        if unbound or fb:
            bad += 1
    print()
    print("结论:", "存在未绑定/兜底问题" if bad else "全部作者年份均已绑定编号")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
