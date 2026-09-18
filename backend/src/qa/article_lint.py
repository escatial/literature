"""文章正文可读性与引用排版体检。"""
from __future__ import annotations
import re
from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from writing.orchestrator import SectionResult

# 姓名被中文标点拆开的几种形态:逗号、句号、间隔号、顿号等,后跟 (YYYY)。
# 真正字符类,直接嵌入 Unicode 字符,避免转义歧义。
_NAME_SEP = r"[,，。、》」．·；：！？!?\s]+"
_SPLIT_NAME = re.compile(
    rf"(?:^|(?<=[，。；：、\s]))[一-鿿]{_NAME_SEP}[一-鿿](?:{_NAME_SEP}[一-鿿])?"
    rf"(?=[一-鿿等（(\s]*\d{{4}})"
)
# 作者年份引用"粘连"判定:必须满足
#   1. 上一字符是中文或文本首(没有标句边界)
#   2. 紧接着出现 2~8 个汉字 + 等? + (YYYY) + [N]
# 修复阶段会在满足 (1) 的位置插入句号,让 (1) 失效,避免重复报错。
_STUCK_AUTHOR = re.compile(
    # 只识别正文边界词后直接跟作者夹注的确定性粘连。不能从每个汉字
    # 位置做零宽扫描，否则正常作者姓名会被从第二个汉字内部误报。
    r"(?:算法|研究|模型|方法|结果|表明|指出|发现|证实|说明)"
    r"[一-鿿]{2,4}(?:等)?[（(]\s*\d{4}\s*[）)][ ]*\[\d+\]"
)
# 数字锚点不能用“前后一个字符”判断是否合法：合法形式通常是
# ``作者（年份）[N]``，而 [N] 后面可能紧接中文右括号、分号或句号。
# 下面只负责枚举锚点，是否裸链由 _raw_numeric_anchor_issues 按句判断。
_RAW_NUMERIC = re.compile(r"\[(\d{1,3})\]")
_DENSE_CITES = re.compile(
    # 3 个以上 [N] 在不超过 60 字符内连续出现,且中间仅夹"作者(年份);",即视为堆叠。
    "(?:\\[[0-9]{1,3}\\][^\\n]{0,18}){3,}"
)
_META_REASONING = re.compile(
    r"(?i)(let\s+me\s+(?:check|make\s+sure|finalize|revise)|"
    r"the\s+(?:previous|original)\s+draft|auto-check|"
    r"i\s+should\s+not\s+write|maybe\s+the\s+issue\s+is|"
    r"内部推理|思考过程|自动检查|上一版草稿)"
)
_AUTHOR_YEAR = re.compile(
    r"(?:"
    r"[一-鿿]{2,4}(?:\s*(?:和|与|、|,|，)\s*[一-鿿]{2,4})*(?:等)?"
    r"|[A-ZÀ-Þ][A-Za-zÀ-Þ .'-]{0,48}(?:et\s+al\.|and\s+[A-ZÀ-Þ][A-Za-zÀ-Þ .'-]+)?"
    r")\s*[（(]\s*(?:19|20)\d{2}\s*[）)]",
    re.IGNORECASE,
)
_FALLBACK_EVIDENCE_PREFIXES = (
    "补充证据显示",
    "与本节问题相邻的研究中",
)


def _journal_patterns(papers) -> list[tuple[str, re.Pattern]]:
    """构造“把期刊当作正文叙述主体”的上下文模式。"""
    if not papers:
        return []
    names = sorted({str(getattr(p, "journal", "") or "").strip() for p in papers}, key=len, reverse=True)
    names = [n for n in names if len(n) >= 3]
    patterns = []
    for name in names:
        escaped = re.escape(name)
        patterns.append((name, re.compile(
            rf"(?:《\s*{escaped}\s*》|(?:在|发表于|刊载于|载于)\s*《?\s*{escaped}\s*》?\s*(?:中|上)?)",
            re.IGNORECASE,
        )))
    return patterns


def _journal_leak_issues(text: str, papers) -> list[re.Match]:
    """检测正文中泄漏的期刊名；普通领域短语不因与刊名同名而误报。"""
    matches = []
    for _, pattern in _journal_patterns(papers):
        matches.extend(pattern.finditer(text or ""))
    return sorted(matches, key=lambda match: match.start())

def repair_article_text(sections: list[SectionResult], papers=None) -> list[dict]:
    """自动修复确定性的排版问题,并返回修复记录。"""
    repairs: list[dict] = []
    for section in sections:
        text = section.content or ""
        original = text
        # 0) 按当前文献池中的规范作者名归一化“欧光军和。梁钦”这类
        # 模型插入标点的姓名。只对真实作者做定向替换，不改动普通正文。
        if papers:
            for paper in papers:
                for raw_author in (getattr(paper, "authors", None) or []):
                    author = str(raw_author or "").strip()
                    compact = re.sub(r"[^一-鿿]", "", author)
                    if len(compact) < 2:
                        continue
                    split = r"[，,。、；;：:·．.\s]*".join(map(re.escape, compact))
                    text = re.sub(split, compact, text)
            # 连接词后的句号/顿号属于姓名分隔噪声，恢复为自然连接。
            text = re.sub(r"([一-鿿]{2,4})(?:和|与)[。．、,，;；\s]+(?=[一-鿿])", r"\1和", text)
            # 期刊是参考文献元数据，不是正文中的论证主体。确定性改写常见
            # “某某在《Applied Sciences》中……”句式，保留作者与论断。
            for _, pattern in _journal_patterns(papers):
                text = pattern.sub("在其研究中", text)
        # 1) 把被标点拆开的姓名压回完整姓名(逗号/句号/顿号等)
        text = re.sub(
            rf"(?P<boundary>^|[，。；：、\s])(?P<name>[一-鿿]{_NAME_SEP}[一-鿿](?:{_NAME_SEP}[一-鿿])?)"
            rf"(?=[一-鿿等（(\s]*\d{{4}})",
            lambda m: m.group("boundary") + re.sub(r"[,，。、》」．·；：！？!?\s]+", "", m.group("name")),
            text,
        )
        # 2) 修复作者年份引用与前一句粘连:
        #    形如"前一句中文文本 + (引用名)(年份)[N]"且前文无句末标点的情形。
        #    把整段切出,在引用名前插入一个句号。
        def _stick_split(match):
            head = match.group(1)
            citation = match.group(2)
            # head 内若已经含有句末标点,就不再切
            if re.search(r"[。！？!?]", head):
                return match.group(0)
            return head + "。" + citation
        # 贪婪匹配:把尽可能长的"前文"留给 group 1,只把 2~8 个汉字 + 等 + (YYYY)[N] 留给 group 2。
        text = re.sub(
            "([一-鿿]{1,})([一-鿿]{2,8}(?:等)?[(一-鿿（]\\s*\\d{4}\\s*[)一-鿿）]\\[\\d+\\])",
            _stick_split,
            text,
        )
        # 上面的句边界修复不能把三字中文姓名拆成“赵林。林等”。
        # 该形态只在年份夹注前出现时成立，确定性合并不会误伤普通句号。
        text = re.sub(
            r"([一-鿿])。([一-鿿])(?=等?[（(]\s*\d{4})",
            r"\1\2",
            text,
        )
        # “算法赵林林等（2026）[3]”这类粘连中，作者前通常是“算法/研究/
        # 模型/方法”等名词。按语义边界补句号，避免把作者姓名内部再切开。
        text = re.sub(
            r"(?P<head>(?:算法|研究|模型|方法|结果|表明|指出|发现|证实))"
            r"(?P<author>[一-鿿]{2,4}(?:等)?[（(]\s*\d{4}\s*[）)]\[\d+\])",
            lambda m: m.group("head") + "。" + m.group("author"),
            text,
        )

        # 3) 在 ] 之后若紧跟中文/英文,补一个空格,让句子边界可读
        text = re.sub(
            "(\\]\\s*(?:；|;)?)\\s*(?=[一-鿿A-Z])",
            "\\1 ",
            text,
        )
        # 4) 将同一句中连续堆叠的作者(年份)[N]拆成短句，避免引用链
        # 黏在段末。换行同时作为句边界，后续体检不会把它再次识别为堆叠。
        text = re.sub(
            r"(\]\s*)[、,，；;]\s*(?=[一-鿿A-Z][^。！？!?\n]{0,24}[（(]\s*(?:19|20)\d{2})",
            r"\1。\n",
            text,
        )
        if text != original:
            section.content = text
            repairs.append({
                "section_key": section.key,
                "changes": ["姓名标点归一化", "作者年份句边界修复", "链接间距修复"],
            })
    return repairs


def repair_unbound_author_year(sections: list[SectionResult], papers) -> list[dict]:
    """为正文中未绑定编号的作者年份夹注补上对应的数字锚点。

    仅处理能由当前文献池唯一确定的作者+年份；无法唯一匹配时不猜测，
    交给章节重写流程，避免把错误编号绑定到错误文献。
    """
    if not papers:
        return []
    candidates: dict[tuple[str, str], list] = {}
    for paper in papers:
        year = str(getattr(paper, "year", 0) or "")
        if not year:
            continue
        for raw in (getattr(paper, "authors", None) or []):
            author = str(raw or "").strip()
            if not author:
                continue
            compact = re.sub(r"[^一-鿿A-Za-z-]", "", author)
            if not compact:
                continue
            surname = compact if re.search(r"[一-鿿]", compact) else compact.split("-")[-1]
            for key in {compact.lower(), surname.lower()}:
                candidates.setdefault((key, year), []).append(paper)
    repairs = []
    author_year_re = re.compile(
        r"(?P<author>[一-鿿]{2,8}|[A-Z][A-Za-z-]{1,32}(?:\s+et\s+al\.)?)"
        r"\s*[（(]\s*(?P<year>(?:19|20)\d{2})\s*[）)]"
    )
    for section in sections:
        if getattr(section, "key", "") == "comment":
            continue
        text = section.content or ""
        changed = False
        def replace(match):
            nonlocal changed
            author = match.group("author").strip()
            year = match.group("year")
            key = re.sub(r"[^一-鿿A-Za-z-]", "", author).lower()
            options = candidates.get((key, year), [])
            if not options and key.endswith("et al"):
                key = key[:-5].strip()
                options = candidates.get((key, year), [])
            unique = {p.lit_id: p for p in options}
            if len(unique) != 1:
                return match.group(0)
            paper = next(iter(unique.values()))
            # 已有同句数字编号则无需处理。
            sentence_end = re.search(r"[。！？!?；;\n]", text[match.end():])
            end = match.end() + (sentence_end.start() if sentence_end else len(text))
            sentence = text[:end]
            if re.search(r"\[\d{1,3}\]", sentence[match.start():]):
                return match.group(0)
            changed = True
            return match.group(0) + f"[lit_{paper.lit_id.removeprefix('lit_')}]"
        new_text = author_year_re.sub(replace, text)
        if changed and new_text != text:
            section.content = new_text
            repairs.append({"section_key": section.key, "changes": ["作者年份夹注自动绑定文献编号"]})
    return repairs


def _raw_numeric_anchor_issues(text: str):
    """找出所在句没有作者(年份)夹注的数字锚点。"""
    for match in _RAW_NUMERIC.finditer(text or ""):
        before = text[:match.start()]
        sentence_start = 0
        for boundary in re.finditer(r"[。！？!?；;\n]", before):
            sentence_start = boundary.end()
        local = before[sentence_start:]
        # 中文作者、英文姓氏均支持；只要求年份与作者出现在同一分句中。
        has_author_year = bool(re.search(
            r"(?:[一-鿿]{2,8}(?:等)?|[A-Za-z][A-Za-z .'-]{1,48}(?:等|et\s+al\.)?)"
            r"\s*[（(]\s*(?:19|20)\d{2}\s*[）)]",
            local,
            flags=re.IGNORECASE,
        ))
        if not has_author_year:
            yield match


def _unbound_author_year_issues(text: str):
    """Yield author/year citations that have no numeric link in their sentence."""
    for match in _AUTHOR_YEAR.finditer(text or ""):
        before = text[:match.start()]
        sentence_start = 0
        for boundary in re.finditer(r"[。！？!?；;\n]", before):
            sentence_start = boundary.end()
        after = text[match.end():]
        end_match = re.search(r"[。！？!?；;\n]", after)
        sentence_end = match.end() + (end_match.end() if end_match else len(after))
        sentence = text[sentence_start:sentence_end]
        if not _RAW_NUMERIC.search(sentence):
            yield match

def check_article_text(sections: list[SectionResult], papers=None) -> QACheckResult:
    result = QACheckResult(
        check_id="article_lint_001",
        name="文章正文可读性体检",
        status=QACheckStatus.PASS,
        metrics={"sections": len(sections), "issues_count": 0},
    )
    issues: list[QAIssue] = []
    for section in sections:
        text = section.content or ""
        if not text.strip():
            issues.append(QAIssue(
                "ARTICLE_EMPTY_SECTION", QACheckStatus.FAIL,
                "content", f"section:{section.key}",
                "章节正文为空", section_key=section.key,
            ))
            continue
        for match in _journal_leak_issues(text, papers):
            start = max(0, match.start() - 80)
            issues.append(QAIssue(
                "ARTICLE_JOURNAL_LEAK", QACheckStatus.FAIL,
                "content", f"section:{section.key}",
                "正文出现期刊或刊物名称，期刊名只能出现在文末参考文献列表",
                section_key=section.key,
                snippet=text[start:match.end() + 80],
            ))
        if getattr(section, "density_warning", False):
            issues.append(QAIssue(
                "ARTICLE_CITATION_DENSITY", QACheckStatus.FAIL,
                "citations", f"section:{section.key}",
                "本章不同文献引用数量低于按文献池规模计算的最低覆盖要求",
                section_key=section.key,
            ))
        if section.key != "comment":
            nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
            fallback_count = sum(
                line.startswith(_FALLBACK_EVIDENCE_PREFIXES) for line in nonempty_lines
            )
            # Any template fallback sentence is a delivery defect.  It is
            # acceptable as an internal recovery signal, but it must not leak
            # into the final article because it reads like a citation dump.
            if fallback_count >= 1:
                issues.append(QAIssue(
                    "ARTICLE_FALLBACK_ONLY", QACheckStatus.FAIL,
                    "content", f"section:{section.key}",
                    "正文包含模板化题录兜底句，应改写为连贯的证据综合句",
                    section_key=section.key,
                ))
        meta_match = _META_REASONING.search(text)
        if meta_match:
            start = max(0, meta_match.start() - 60)
            issues.append(QAIssue(
                "ARTICLE_META_REASONING", QACheckStatus.FAIL,
                "content", f"section:{section.key}",
                "正文包含模型内部推理、段落计划或文献清单泄漏",
                section_key=section.key,
                snippet=text[start:meta_match.end() + 120],
            ))
        for pattern, code, message in [
            (_SPLIT_NAME, "ARTICLE_SPLIT_AUTHOR", "检测到被中文标点拆开的中文姓名"),
            (_STUCK_AUTHOR, "ARTICLE_STUCK_AUTHOR_YEAR", "作者年份引用与前一句粘连,句子边界不完整"),
            (_DENSE_CITES, "ARTICLE_DENSE_CITATIONS", "同一位置连续堆叠多个引用,无法判断各文献对应论断"),
        ]:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 60)
                issues.append(QAIssue(
                    code,
                    QACheckStatus.FAIL if code in {"ARTICLE_SPLIT_AUTHOR", "ARTICLE_STUCK_AUTHOR_YEAR"} else QACheckStatus.WARN,
                    "content", f"section:{section.key}", message,
                    section_key=section.key,
                    snippet=text[start:match.end() + 60],
                ))
        for match in _raw_numeric_anchor_issues(text):
            start = max(0, match.start() - 60)
            issues.append(QAIssue(
                "ARTICLE_RAW_NUMBER_ANCHOR", QACheckStatus.FAIL,
                "content", f"section:{section.key}",
                "检测到缺少作者与年份的裸数字链接",
                section_key=section.key,
                snippet=text[start:match.end() + 60],
            ))
        if section.key != "comment":
            for match in _unbound_author_year_issues(text):
                start = max(0, match.start() - 60)
                issues.append(QAIssue(
                    "ARTICLE_UNBOUND_AUTHOR_YEAR", QACheckStatus.FAIL,
                    "content", f"section:{section.key}",
                    "正文作者年份夹注未绑定参考文献编号",
                    section_key=section.key,
                    snippet=text[start:match.end() + 60],
                ))
    result.issues = issues
    result.metrics["issues_count"] = len(issues)
    result.metrics["fail_count"] = sum(i.severity == QACheckStatus.FAIL for i in issues)
    result.metrics["warn_count"] = sum(i.severity == QACheckStatus.WARN for i in issues)
    result.status = (
        QACheckStatus.FAIL if result.metrics["fail_count"]
        else QACheckStatus.WARN if result.metrics["warn_count"]
        else QACheckStatus.PASS
    )
    return result
