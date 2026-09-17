"""Independent audit of the latest writing replay output.

Reads outputs/latest_writing_result.json (produced by run_real_review.py) and
checks the defect classes that were reported in earlier rounds:

  1. every plan paper is assigned to exactly one theme group
  2. theme names are complete, distinct, not over-broad, not title fragments
  3. citation coverage / unique cited count
  4. body text residue: internal ids, bare numeric citations, glued author-year,
     stacked reference dumps, truncated sentences
  5. in-text citations carry author name + year
  6. reference list sanity: no [J/OL] on English entries, no placeholder text

Run:  python audit_latest_output.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OUT = Path(__file__).resolve().parent / "outputs"
RESULT = OUT / "latest_writing_result.json"

BARE_NUM = re.compile(r"(?<![\u4e00-\u9fffA-Za-z])[\[\(]\s*\d+(?:\s*[-,，]\s*\d+)*\s*[\]\)]")
INTERNAL_ID = re.compile(r"lit_[a-z]+_[0-9a-f]{6,}|lit_id|theme_\d+")
AUTHOR_YEAR = re.compile(
    r"([\u4e00-\u9fff][\u4e00-\u9fff,，、\s]{0,18}?(?:等)?[\(\（]\s*\d{4}[a-z]?\s*[\)\）])"
    r"|([A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and|&)\s+[A-Za-z\-']+)*\s*[\(\（]\s*\d{4}[a-z]?\s*[\)\）])"
)
CJK_GLUE = re.compile(r"[\u4e00-\u9fff]，[\u4e00-\u9fff]|[\u4e00-\u9fff]。[，、；]")
TRUNC_TAIL = re.compile(r"[的与和及在为对把从向以等是在]$")

problems: list[str] = []
warnings: list[str] = []
passed: list[str] = []


def report(level: str, msg: str) -> None:
    if level == "fail":
        problems.append(msg)
    elif level == "warn":
        warnings.append(msg)
    else:
        passed.append(msg)


def main() -> int:
    data = json.loads(RESULT.read_text(encoding="utf-8"))
    groups = data.get("groups") or []
    sections = data.get("sections") or []
    plan_n = int(data.get("plan_papers") or 0)

    # 1. group assignment integrity -----------------------------------------
    assigned: list[str] = []
    for g in groups:
        assigned.extend(g.get("lit_ids") or [])
    dup = [k for k, v in Counter(assigned).items() if v > 1]
    if dup:
        report("fail", f"同一文献被分到多个主题: {len(dup)} 篇 -> {dup[:5]}")
    if len(assigned) != plan_n:
        report(
            "fail",
            f"写作池 {plan_n} 篇，但只归入主题 {len(assigned)} 篇，"
            f"丢弃 {plan_n - len(assigned)} 篇",
        )
    else:
        report("ok", f"写作池 {plan_n} 篇全部归入主题，无重复归属")

    # 2. theme naming --------------------------------------------------------
    names = [str(g.get("name") or "").strip() for g in groups]
    if len(names) < 4:
        report("fail", f"主题数 {len(names)} < 4，文献池规模不足以支撑综述结构")
    for n in names:
        if not n:
            report("fail", "存在空主题名")
        elif len(n) < 4:
            report("fail", f"主题名过短/疑似截断: {n!r}")
        elif TRUNC_TAIL.search(n):
            report("fail", f"主题名以虚词结尾，疑似标题半截片段: {n!r}")
        elif len(n) > 24:
            report("warn", f"主题名过长，可读性差: {n!r} ({len(n)} 字)")
    if len(set(names)) != len(names):
        report("fail", f"主题名重复: {names}")
    near = [
        (a, b)
        for i, a in enumerate(names)
        for b in names[i + 1 :]
        if a and b and (a in b or b in a) and a != b
    ]
    if near:
        report("fail", f"主题名高度重叠，聚类边界不清: {near}")
    sizes = sorted(len(g.get("lit_ids") or []) for g in groups)
    if sizes and sizes[0] < 10:
        report("fail", f"主题规模不均衡，最小主题仅 {sizes[0]} 篇: {sizes}")
    report("ok", f"主题: {names} 规模={sizes}")

    # 3. citation coverage ---------------------------------------------------
    cited: list[str] = []
    for s in sections:
        cited.extend(s.get("citations") or [])
    uniq = set(cited)
    coverage = len(uniq) / plan_n if plan_n else 0.0
    if coverage < 0.8:
        report("fail", f"引用覆盖率仅 {coverage:.1%}（{len(uniq)}/{plan_n}），丢弃文献过多")
    elif coverage < 0.9:
        report("warn", f"引用覆盖率 {coverage:.1%}（{len(uniq)}/{plan_n}）")
    else:
        report("ok", f"引用覆盖率 {coverage:.1%}（{len(uniq)}/{plan_n}）")
    if not (data.get("screened_out_ids") or []):
        report("warn", "screened_out_ids 为空，无法核验 540->90 的淘汰链路")

    # 4/5. body text residue + in-text citation quality ----------------------
    for s in sections:
        title = s.get("title") or s.get("key")
        content = s.get("content") or ""
        if not content.strip():
            report("fail", f"章节 {title} 正文为空")
            continue
        for m in set(INTERNAL_ID.findall(content)):
            report("fail", f"章节 {title} 正文残留内部标识: {m}")
        # GB/T 7714 numeric style legitimately uses [n] right after author+year;
        # only flag numbers with no author-year marker in front of them.
        orphans = []
        for m in BARE_NUM.finditer(content):
            head = content[max(0, m.start() - 40) : m.start()]
            if not AUTHOR_YEAR.search(head):
                orphans.append(m.group(0))
        if orphans:
            report("fail", f"章节 {title} 存在无作者年份的裸引用 {len(orphans)} 处: {orphans[:6]}")
        glue = [m.group(0) for m in CJK_GLUE.finditer(content)]
        if glue:
            report("fail", f"章节 {title} 存在标点粘连/句界损坏 {len(glue)} 处: {glue[:6]}")
        if re.search(r"[（(]\s*[)）]|\[\s*\]", content):
            report("fail", f"章节 {title} 存在空括号占位")
        if "补充证据显示" in content:
            report("fail", f"章节 {title} 仍有“补充证据显示”式兜底堆砌")
        # sentence length sanity: catch truncation
        for sent in re.split(r"[。；;!?！？]\s*", content):
            if 0 < len(sent.strip()) < 6 and not re.match(r"^[#\-\|>]", sent.strip()):
                report("warn", f"章节 {title} 疑似残句: {sent.strip()!r}")
    if not any("裸引用" in p for p in problems):
        report("ok", "正文无裸数字引用")
    if not any("标点粘连" in p for p in problems):
        report("ok", "正文无标点粘连/句界损坏")
    if not any("内部标识" in p for p in problems):
        report("ok", "正文无 lit_/theme_ 等内部标识残留")

    # 6. reference list ------------------------------------------------------
    ref_list = data.get("reference_list") or ""
    if not isinstance(ref_list, str) or len(ref_list) < 200:
        report("fail", "参考文献列表过短或缺失")
    else:
        entries = re.findall(r"^\[(\d+)\]\s*(.+)$", ref_list, re.M)
        nums = [int(n) for n, _ in entries]
        if nums != sorted(nums) or (nums and nums[0] != 1):
            report("fail", "参考文献编号不连续或未从 1 开始")
        if len(entries) != len(uniq):
            report("fail", f"参考文献 {len(entries)} 条与唯一引用 {len(uniq)} 篇不匹配")
        cjk = [t for _, t in entries if re.search(r"[\u4e00-\u9fff]", t)]
        eng = [t for _, t in entries if not re.search(r"[\u4e00-\u9fff]", t)]
        eng_ol = [t for t in eng if "[J/OL]" in t]
        if eng and len(eng_ol) == len(eng):
            report("fail", f"全部 {len(eng)} 条英文文献均为 [J/OL]，期刊论文类型标错")
        elif eng_ol:
            report("warn", f"英文文献中 {len(eng_ol)}/{len(eng)} 条为 [J/OL]")
        if "TODO" in ref_list or "待补" in ref_list or "unknown" in ref_list.lower():
            report("fail", "参考文献含占位文本")
        report("ok", f"参考文献 {len(entries)} 条（中文 {len(cjk)} / 英文 {len(eng)}）")

    # 7. QA self-report cross-check -----------------------------------------
    qa = data.get("qa") or {}
    overall = (qa.get("summary") or {}).get("overall")
    if overall != "pass":
        report("fail", f"项目内置 QA 未通过: {overall}")
    else:
        report("ok", "项目内置 QA: pass")

    # ---- output ------------------------------------------------------------
    print("=" * 72)
    print("独立核查：", RESULT.name)
    print("=" * 72)
    for line in problems:
        print(f"[FAIL] {line}")
    for line in warnings:
        print(f"[warn] {line}")
    print("--- 通过项 ---")
    for line in passed:
        print(f"[ ok ] {line}")
    print()
    print(f"结论: {'不合格' if problems else '合格'}  (失败 {len(problems)} / 提示 {len(warnings)})")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
