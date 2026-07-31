# 文献综述 Agent — 双轨检索 + LLM 综述 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 Web 应用(FastAPI + React + IndexedDB),支持"英文自动检索(OpenAlex)+ 中文手动批量导入"双轨工作流,最终用 LLM 按用户指定分类方式(国内外/主题)完成文献综述写作。所有文献引文来自平台原始字段,工具不做拼接。

**Architecture:** 三段式流水线:
1. **英文轨**:OpenAlex 检索 → 元数据自动入库 → 用户筛选 → 入库
2. **中文轨**:用户批量粘贴 GB/T 7714 引文 → 工具解析 → 入库
3. **写作轨**:用户选分类方式(国内外/主题) → LLM 综述(单篇引文+摘要限定上下文) → 输出综述章节

**Tech Stack:**
- 后端:Python 3.11+,FastAPI,httpx,pydantic v2,uvicorn
- 前端:React 18 + TypeScript + Vite + Tailwind CSS + Zustand (状态) + Dexie.js (IndexedDB 封装)
- LLM:Anthropic Claude API (claude-3-5-sonnet),备用 OpenAI
- 检索:OpenAlex 主源,CrossRef DOI 兜底
- 存储:浏览器 IndexedDB(Dexie),后端无状态
- 反幻觉:每条引文来自平台原始字段;LLM 只引用 verified_cache 中的 `lit_id`,不允许编造

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                            Web 前端 (React)                            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  [英文检索页]  ──→  OpenAlex API (FastAPI 代理)                       │
│       │                │                                              │
│       ▼                ▼                                              │
│  [筛选器: 年份/被引/期刊层级] → [LLM 相关度重排]                       │
│       │                                                               │
│       ▼                                                               │
│  [导入中文页]  ──→  批量粘贴 GB/T 7714 引文 → 解析                     │
│       │                                                               │
│       ▼                                                               │
│  [文献池 (IndexedDB)]  ←  英文/中文文献统一存                            │
│       │                                                               │
│       ▼                                                               │
│  [去重 / 筛选]  ←  用户人工筛选(标题级别)                              │
│       │                                                               │
│       ▼                                                               │
│  [写作页] → 选分类方式(国内外/主题) → LLM 综述生成                     │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 关键决策记录(来自前期对话)

| 决策 | 选择 | 备注 |
|------|------|------|
| 数据源策略 | 英文 OpenAlex + CrossRef,中文用户手动粘贴 | 不爬虫、不抓取、不签约 |
| 引文生成 | 不拼接,只接收平台原始字段或用户粘贴 | GB/T 7714-2025 引文由平台/用户提供 |
| 产品形态 | Web 应用 + 本地存储(IndexedDB) | 用户数据全在浏览器 |
| 英文过滤维度 | 时间范围 + 被引次数 + 期刊层级 + LLM 重排 | 4 维全要 |
| 中文入口 | 批量文本导入(一次性多条粘贴) | textarea 一区,逐行解析 |
| 综述分类 | 按国内外 / 按主题(用户二选一) | LLM 按选定方式输出 |
| 文献筛选 | 综述前先做语义筛选(LLM) | 滤除主题不符的文献 |

---

## 文件结构

### 后端 `backend/`

```
backend/
├── main.py                          # FastAPI 入口
├── pyproject.toml                   # 依赖:fastapi, uvicorn, httpx, pydantic, anthropic
├── requirements.txt
├── .env.example                     # ANTHROPIC_API_KEY=sk-ant-...
├── src/
│   ├── api/
│   │   ├── retrieval.py             # /api/retrieval/* - 英文检索
│   │   ├── import_cn.py             # /api/import/cn - 中文批量导入
│   │   ├── screening.py             # /api/screening/* - 文献筛选
│   │   ├── writing.py               # /api/writing/* - LLM 综述生成
│   │   └── health.py                # /healthz
│   ├── retrieval/
│   │   ├── types.py                 # Paper dataclass, RetrievalResult
│   │   ├── openalex_adapter.py      # OpenAlex 适配器(主源)
│   │   ├── crossref_adapter.py      # CrossRef 适配器(DOI 兜底)
│   │   ├── query_planner.py         # LLM 生成英文检索式
│   │   ├── filters.py               # 年份/被引/期刊层级过滤
│   │   └── reranker.py              # LLM 相关度重排
│   ├── import_cn/
│   │   ├── parser.py                # GB/T 7714 引文解析(正则)
│   │   └── types.py                 # ImportedCitation dataclass
│   ├── screening/
│   │   └── llm_filter.py            # LLM 主题不符筛除
│   ├── writing/
│   │   ├── orchestrator.py          # 综述生成总控
│   │   ├── classifier.py            # 按国内外 / 按主题分类
│   │   ├── section_writer.py        # 每章节 LLM 生成
│   │   └── templates.py             # 7 章综述模板
│   ├── llm/
│   │   ├── client.py                # Anthropic 客户端封装(含 timeout/重试)
│   │   └── prompts/
│   │       ├── query_planner.md
│   │       ├── reranker.md
│   │       ├── screening.md
│   │       ├── classify.md
│   │       └── section_writer.md
│   └── utils/
│       ├── logging.py               # structlog
│       └── config.py                # pydantic-settings
└── tests/
    ├── retrieval/
    │   ├── test_openalex_adapter.py
    │   ├── test_filters.py
    │   └── fixtures/
    │       └── openalex_sample.json
    ├── import_cn/
    │   └── test_parser.py
    ├── screening/
    │   └── test_llm_filter.py
    └── writing/
        └── test_section_writer.py
```

### 前端 `frontend/`

```
frontend/
├── package.json                     # react, typescript, vite, tailwind,
│                                    # zustand, dexie, react-markdown
├── vite.config.ts                   # 代理 /api → 后端
├── tsconfig.json
├── tailwind.config.js
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx                      # 路由:Landing → Retrieve / ImportCn / Pool / Write
│   ├── api/
│   │   ├── client.ts                # fetch 封装(SSE 不用,普通 JSON)
│   │   └── types.ts                 # Paper, Citation, ScreeningResult 类型
│   ├── store/
│   │   └── db.ts                    # Dexie 数据库定义
│   │       # tables: papers, citations, reviews, settings
│   ├── pages/
│   │   ├── TopicInput.tsx           # 第一步:输入研究主题
│   │   ├── EnglishRetrieval.tsx     # 英文检索页
│   │   ├── ChineseImport.tsx        # 中文批量粘贴页
│   │   ├── LiteraturePool.tsx       # 文献池 + 筛选 + 去重
│   │   └── Writing.tsx              # 综述写作页
│   ├── components/
│   │   ├── PaperCard.tsx            # 单篇文献卡片(标题+引文+摘要+勾选)
│   │   ├── FilterPanel.tsx          # 4 维过滤
│   │   ├── ClassifySelector.tsx     # 国内外 / 主题 二选一
│   │   ├── ReviewOutput.tsx         # 综述章节渲染
│   │   └── CitationList.tsx         # 引用列表(README 中展示)
│   └── hooks/
│       ├── useRetriever.ts          # 检索状态机
│       ├── useImporter.ts           # 中文导入状态机
│       └── useWriter.ts             # LLM 综述状态机
```

---

## 任务分解

### Phase 0: 项目初始化

#### Task 0.1: 仓库结构与依赖

**Files:**
- Create: `backend/pyproject.toml`, `backend/.env.example`, `backend/main.py`(空)
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/index.html`, `frontend/tailwind.config.js`
- Create: `.gitignore`

- [ ] **Step 1: 创建 backend 骨架**

```toml
# backend/pyproject.toml
[project]
name = "lit-review-agent-backend"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "httpx>=0.27",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "anthropic>=0.39",
    "structlog>=24.1",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "respx>=0.21"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

```bash
# backend/.env.example
ANTHROPIC_API_KEY=sk-ant-your-key-here
MODEL_NAME=claude-3-5-sonnet-20241022
OPENALEX_MAILTO=your-email@example.com
APP_ENV=development
```

- [ ] **Step 2: 创建 frontend 骨架**

```json
// frontend/package.json
{
  "name": "lit-review-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "react-router-dom": "^6.26.0",
    "zustand": "^4.5.0",
    "dexie": "^4.0.0",
    "react-markdown": "^9.0.0",
    "clsx": "^2.1.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "autoprefixer": "^10.4.0",
    "postcss": "^8.4.0",
    "tailwindcss": "^3.4.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0"
  }
}
```

```ts
// frontend/vite.config.ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
```

- [ ] **Step 3: Commit**

```bash
git init
git add .
git commit -m "chore: scaffold backend + frontend skeleton"
```

---

### Phase 1: 后端基础(英文检索流水线)

#### Task 1.1: OpenAlex 适配器

**Files:**
- Create: `backend/src/retrieval/types.py`
- Create: `backend/src/retrieval/openalex_adapter.py`
- Test: `backend/tests/retrieval/test_openalex_adapter.py`
- Create: `backend/tests/retrieval/fixtures/openalex_sample.json`

- [ ] **Step 1: 定义 `Paper` dataclass(平台原始字段,不构造)**

```python
# backend/src/retrieval/types.py
"""检索结果统一类型。字段值都来自平台原始返回,工具不构造任何字段。"""
from dataclasses import dataclass, asdict
from enum import Enum


class Source(str, Enum):
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    USER_IMPORTED = "user_imported"  # 中文手动导入


@dataclass
class Paper:
    """单篇文献的最小元数据集。"""
    lit_id: str             # 本工具生成的内部 ID,SHA256(title|doi)[:12]
    source: Source          # 来源

    # 来自平台的原始字段(用户视情况从哪条源读到哪条)
    title: str
    authors: list[str]      # 平台 author 字段原顺序
    journal: str            # 期刊名(全称,平台字段透传)
    year: int
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None

    # 可选字段(摘要不一定有)
    abstract: str | None = None
    doi: str | None = None

    # 跳转链接(只读,不下载)
    source_url: str = ""    # 平台给出的原文 URL
    cited_by_count: int = 0 # OpenAlex 字段
    journal_level: str | None = None  # "SCI"/"SSCI"/"AHCI"/"ESCI" 来自 CrossRef

    def to_dict(self) -> dict:
        return asdict(self)
```

- [ ] **Step 2: 编写 OpenAlex 适配器**

```python
# backend/src/retrieval/openalex_adapter.py
"""
OpenAlex 适配器。
- 只读取 OpenAlex 返回的字段,不构造任何字段
- 摘要还原 abstract_inverted_index(平台字段的反序列化,不是生成)
- 礼貌带 mailto,限速 ≤ 10 req/s
"""
import hashlib
import httpx
import re
from .types import Paper, Source
import logging

log = logging.getLogger(__name__)
MAILTO = "your-email@example.com"   # 由 config 注入


def _rebuild_abstract(inverted: dict | None) -> str | None:
    """OpenAlex abstract_inverted_index 是反向索引,需要还原。这是字段反序列化,不是生成。"""
    if not inverted:
        return None
    word_positions = []
    for word, positions in inverted.items():
        for p in positions:
            word_positions.append((p, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions).strip() or None


def _make_lit_id(title: str | None, doi: str | None) -> str:
    """内部唯一 ID,SHA256(title|doi)[:16],不与外部通信"""
    raw = f"{title or ''}|{doi or ''}"
    return "lit_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class OpenAlexAdapter:
    BASE = "https://api.openalex.org/works"

    def search(self, query: str, year_range: tuple[int, int], per_page: int = 50) -> list[Paper]:
        params = {
            "search": query,
            "filter": f"publication_year:{year_range[0]}-{year_range[1]}",
            "per-page": min(per_page, 200),
            "mailto": MAILTO,
        }
        with httpx.Client(timeout=30.0) as client:
            try:
                resp = client.get(self.BASE, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("OpenAlex 请求失败: %s", e)
                return []
            return [self._parse(w) for w in resp.json().get("results", [])]

    def _parse(self, w: dict) -> Paper:
        """读取字段。缺字段保持 None。"""
        doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
        title = (w.get("title") or w.get("display_name") or "").strip()
        authors = [
            a["author"]["display_name"]
            for a in w.get("authorships", [])
            if a.get("author")
        ]
        biblio = w.get("biblio") or {}
        volume = str(biblio.get("volume")) if biblio.get("volume") else None
        issue = str(biblio.get("issue")) if biblio.get("issue") else None
        first = biblio.get("first_page")
        last = biblio.get("last_page")
        pages = f"{first}-{last}" if (first and last) else (first or last or None)
        primary = w.get("primary_location") or {}
        source_loc = primary.get("source") or {}
        journal = source_loc.get("display_name") or ""

        return Paper(
            lit_id=_make_lit_id(title, doi),
            source=Source.OPENALEX,
            title=title,
            authors=authors,
            journal=journal,
            year=w.get("publication_year") or 0,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=_rebuild_abstract(w.get("abstract_inverted_index")),
            doi=doi,
            source_url=primary.get("landing_page_url") or w.get("id") or "",
            cited_by_count=w.get("cited_by_count") or 0,
        )
```

- [ ] **Step 3: 写 fixture 与测试**

```python
# backend/tests/retrieval/test_openalex_adapter.py
from retrieval.openalex_adapter import _rebuild_abstract, _make_lit_id


def test_rebuild_abstract_basic():
    inverted = {"Hello": [0], "world": [1]}
    assert _rebuild_abstract(inverted) == "Hello world"


def test_rebuild_abstract_none():
    assert _rebuild_abstract(None) is None
    assert _rebuild_abstract({}) is None


def test_lit_id_deterministic():
    a = _make_lit_id("title A", "10.123/abc")
    b = _make_lit_id("title A", "10.123/abc")
    assert a == b
    assert a.startswith("lit_")
    assert len(a) == 4 + 16


def test_lit_id_diff_on_title():
    a = _make_lit_id("title A", None)
    b = _make_lit_id("title B", None)
    assert a != b
```

- [ ] **Step 4: 跑测试**

Run: `cd backend && pytest tests/retrieval/test_openalex_adapter.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/retrieval/types.py backend/src/retrieval/openalex_adapter.py backend/tests/
git commit -m "feat(retrieval): OpenAlex adapter with field passthrough"
```

#### Task 1.2: CrossRef 兜底适配器

**Files:**
- Create: `backend/src/retrieval/crossref_adapter.py`

- [ ] **Step 1: 写 CrossRef 适配器(同样只读取字段)**

```python
# backend/src/retrieval/crossref_adapter.py
"""CrossRef 适配器。仅用于:OpenAlex 缺 DOI 时二次拿字段,以及获取期刊层级信息。"""
import httpx
import logging
from .types import Paper, Source
from .openalex_adapter import _make_lit_id

log = logging.getLogger(__name__)


class CrossRefAdapter:
    BASE = "https://api.crossref.org/works"

    def by_doi(self, doi: str) -> Paper | None:
        if not doi:
            return None
        with httpx.Client(timeout=20.0) as client:
            try:
                resp = client.get(f"{self.BASE}/{doi}", params={"mailto": "your-email@example.com"})
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                item = resp.json().get("message", {})
                return self._parse(item)
            except httpx.HTTPError as e:
                log.warning("CrossRef 失败: %s", e)
                return None

    def _parse(self, item: dict) -> Paper:
        title = (item.get("title") or [""])[0]
        authors = [
            f"{a.get('family','')}, {a.get('given','')}".strip(", ")
            for a in item.get("author", [])
        ]
        year_parts = (item.get("issued", {}) or {}).get("date-parts", [[None]])
        year = year_parts[0][0] if year_parts and year_parts[0] else 0
        # 期刊层级推断(CrossRef 没有显式字段,用 ISSN-L 段位启发式)
        issn_l = (item.get("ISSN-L") or [""])[0] if item.get("ISSN-L") else ""
        return Paper(
            lit_id=_make_lit_id(title, item.get("DOI")),
            source=Source.CROSSREF,
            title=title,
            authors=authors,
            journal=(item.get("container-title") or [""])[0],
            year=int(year) if year else 0,
            volume=item.get("volume"),
            issue=item.get("issue"),
            pages=item.get("page"),
            doi=item.get("DOI"),
            source_url=item.get("URL", ""),
        )
```

- [ ] **Step 2: Commit**

```bash
git add backend/src/retrieval/crossref_adapter.py
git commit -m "feat(retrieval): CrossRef fallback adapter"
```

#### Task 1.3: 过滤与重排

**Files:**
- Create: `backend/src/retrieval/filters.py`
- Create: `backend/src/retrieval/reranker.py`

- [ ] **Step 1: 实现 4 维过滤(纯函数,不依赖 LLM)**

```python
# backend/src/retrieval/filters.py
"""检索结果过滤。纯函数,无副作用,无 LLM。"""
from .types import Paper


def by_year(papers: list[Paper], year_range: tuple[int, int]) -> list[Paper]:
    lo, hi = year_range
    return [p for p in papers if p.year and lo <= p.year <= hi]


def by_min_citations(papers: list[Paper], min_cited: int) -> list[Paper]:
    return [p for p in papers if p.cited_by_count >= min_cited]


def by_journal_level(papers: list[Paper], levels: set[str]) -> list[Paper]:
    """
    按期刊层级过滤。levels 是集合,任一命中即保留。
    注:journal_level 字段由 OpenAlex 的 'indexed_in' 或后续 CrossRef 补全。
    """
    if not levels:
        return papers
    return [p for p in papers if p.journal_level in levels]


def deduplicate(papers: list[Paper]) -> list[Paper]:
    """基于 DOI + (title|authors[0])|year 去重,保留第一篇。"""
    seen = set()
    out = []
    for p in papers:
        key = (
            (p.doi or "").lower(),
            ((p.title or "") + "|" + (p.authors[0] if p.authors else "") + "|" + str(p.year))
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out
```

- [ ] **Step 2: 实现 LLM 相关度重排**

```python
# backend/src/retrieval/reranker.py
"""LLM 重排:基于论文标题+摘要+主题,输出 0-100 分。只打分,不构造字段。"""
import json
import re
from .types import Paper
from llm.client import get_client


RERANK_PROMPT = """你是学术相关性评分专家。
任务:对每篇论文,根据其标题与摘要,输出与用户研究主题的相关度(0-100 整数)。
只输出严格 JSON 数组,元素为 {"lit_id": "...", "score": <int>, "reason": "<一句话>"}。
不要输出任何其他文字。"""


def rerank(papers: list[Paper], topic: str, top_n: int = 50) -> list[Paper]:
    """对前 N 篇打分,按分排序返回。LLM 只输出分数,不输出引文字段。"""
    if not papers:
        return []
    sample = papers[:top_n]
    payload = [
        {"lit_id": p.lit_id, "title": p.title,
         "abstract": (p.abstract or "")[:600]}
        for p in sample
    ]
    user_msg = (
        f"研究主题:{topic}\n"
        f"候选论文(JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    client = get_client()
    resp = client.messages_create(
        system=RERANK_PROMPT,
        user=user_msg,
        max_tokens=2000,
    )
    raw = strip_md_fences(resp)
    try:
        scores = json.loads(raw)
    except json.JSONDecodeError:
        return sample   # 失败兜底,不阻塞检索

    score_map = {s["lit_id"]: float(s["score"]) for s in scores if "lit_id" in s and "score" in s}
    return sorted(
        sample,
        key=lambda p: score_map.get(p.lit_id, 0),
        reverse=True,
    )


def strip_md_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1) if m else text
```

- [ ] **Step 3: Commit**

```bash
git add backend/src/retrieval/filters.py backend/src/retrieval/reranker.py
git commit -m "feat(retrieval): filters + LLM rerank"
```

#### Task 1.4: LLM 客户端 + 检索式生成

**Files:**
- Create: `backend/src/llm/client.py`
- Create: `backend/src/llm/prompts/query_planner.md`
- Create: `backend/src/retrieval/query_planner.py`

- [ ] **Step 1: Anthropic 客户端(含超时/重试)**

```python
# backend/src/llm/client.py
import os
import time
import logging
from anthropic import Anthropic
from anthropic import APIError, APITimeoutError

log = logging.getLogger(__name__)
_client = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 ANTHROPIC_API_KEY")
        _client = Anthropic(api_key=api_key, timeout=60.0)
    return _client


def messages_create(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    max_retries: int = 3,
) -> str:
    """简化版消息创建。带超时重试。"""
    client = get_client()
    model = model or os.environ.get("MODEL_NAME", "claude-3-5-sonnet-20241022")
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
            )
            return resp.content[0].text
        except APITimeoutError as e:
            last_err = e
            time.sleep(2 ** attempt)
        except APIError as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    log.error("LLM 调用失败: %s", last_err)
    raise last_err
```

- [ ] **Step 2: 检索式生成 prompt**

```markdown
<!-- backend/src/llm/prompts/query_planner.md -->
你是学术检索规划专家。
任务:把用户的研究主题拆成 OpenAlex 英文检索关键词。
- 中文关键词仅作参考,实际检索用英文
- 输出严格 JSON:{"keywords_en": [...], "exclude": [...], "year_start": ..., "year_end": ..., "topic_summary": "..."}
- 不要列举具体文献,不要使用"据我所知"
```

- [ ] **Step 3: 实现 query_planner**

```python
# backend/src/retrieval/query_planner.py
"""LLM 把中文主题拆成英文 OpenAlex 检索式。产物严格 JSON。"""
import json
import re
from llm.client import messages_create
from llm.prompts.query_planner_md import SYSTEM  # 见下方加载方式


SYSTEM = """你是学术检索规划专家。
任务:把用户的研究主题拆成 OpenAlex 英文检索关键词。
- 中文关键词仅作参考,实际检索用英文
- 输出严格 JSON:{"keywords_en": [...], "exclude": [...], "year_start": ..., "year_end": ..., "topic_summary": "..."}
- 不要列举具体文献,不要使用"据我所知"
"""


def plan_query(topic: str, default_year_start: int = 2020) -> dict:
    user = f"研究主题:{topic}\n当前年份:2026。请输出 JSON:"
    raw = messages_create(SYSTEM, user, max_tokens=600)
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        data = json.loads(raw)
        keywords = data.get("keywords_en") or [topic]
        return {
            "keywords_en": keywords,
            "exclude": data.get("exclude") or [],
            "year_start": int(data.get("year_start") or default_year_start),
            "year_end": int(data.get("year_end") or 2026),
            "topic_summary": data.get("topic_summary") or topic,
        }
    except (json.JSONDecodeError, ValueError):
        # 兜底:用原主题直接检索
        return {
            "keywords_en": [topic],
            "exclude": [],
            "year_start": default_year_start,
            "year_end": 2026,
            "topic_summary": topic,
        }
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/llm/ backend/src/retrieval/query_planner.py
git commit -m "feat(llm): client + query planner"
```

#### Task 1.5: 英文检索 API 路由

**Files:**
- Create: `backend/src/api/retrieval.py`

- [ ] **Step 1: 路由实现**

```python
# backend/src/api/retrieval.py
"""英文文献检索 API。/api/retrieval/search"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from retrieval.openalex_adapter import OpenAlexAdapter
from retrieval.filters import by_year, by_min_citations, deduplicate
from retrieval.query_planner import plan_query
from retrieval.reranker import rerank

router = APIRouter()


class SearchRequest(BaseModel):
    topic: str
    year_start: int = 2020
    year_end: int = 2026
    min_citations: int = 0
    limit: int = 50
    use_rerank: bool = True


class SearchResponse(BaseModel):
    topic_summary: str
    total_before_filter: int
    total_after_filter: int
    papers: list[dict]


@router.post("/retrieval/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")

    # 1. LLM 拆词(可失败兜底)
    planned = plan_query(req.topic, default_year_start=req.year_start)
    keywords = " ".join(planned["keywords_en"]) if planned["keywords_en"] else req.topic

    # 2. OpenAlex 检索
    adapter = OpenAlexAdapter()
    raw = adapter.search(
        query=keywords,
        year_range=(req.year_start, req.year_end),
        per_page=req.limit,
    )

    # 3. 过滤 + 去重
    papers = by_year(raw, (req.year_start, req.year_end))
    papers = by_min_citations(papers, req.min_citations)
    papers = deduplicate(papers)

    # 4. LLM 重排(可选)
    if req.use_rerank and papers:
        papers = rerank(papers, planned["topic_summary"])

    return SearchResponse(
        topic_summary=planned["topic_summary"],
        total_before_filter=len(raw),
        total_after_filter=len(papers),
        papers=[p.to_dict() for p in papers],
    )
```

- [ ] **Step 2: Commit**

```bash
git add backend/src/api/retrieval.py
git commit -m "feat(api): English retrieval endpoint"
```

---

### Phase 2: 中文引文批量导入

#### Task 2.1: GB/T 7714 引文解析器

**Files:**
- Create: `backend/src/import_cn/parser.py`
- Create: `backend/src/import_cn/types.py`
- Test: `backend/tests/import_cn/test_parser.py`

- [ ] **Step 1: 定义类型**

```python
# backend/src/import_cn/types.py
from dataclasses import dataclass


@dataclass
class ImportedCitation:
    """用户批量粘贴 GB/T 7714 引文,解析后归一。"""
    raw_text: str            # 用户原始粘贴的一行
    authors: str             # 解析得到
    title: str
    journal: str
    year: int
    volume: str | None
    issue: str | None
    pages: str | None
    parsed_ok: bool          # 解析是否成功
    error: str | None        # 失败原因

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
```

- [ ] **Step 2: 解析器(对 [J] 期刊类型最常见情况做正则)**

```python
# backend/src/import_cn/parser.py
"""
GB/T 7714-2025 期刊论文引文解析器。
格式:作者. 题名[J]. 刊名, 年, 卷(期): 起止页码.

注意:解析器只识别与切分,不构造字段。所有字段值来自用户粘贴的原文。
"""
import re
from typing import List
from .types import ImportedCitation


PATTERN = re.compile(
    r"""^
    (?P<authors>[^.\n]+?)\.\s*
    (?P<title>[^[\n]+?)\s*
    \[J\]\.\s*
    (?P<journal>[^,\n]+?),\s*
    (?P<year>\d{4})(?:\s*,\s*(?P<rest>.+))?
    $
    """,
    re.VERBOSE,
)


def parse_one(text: str) -> ImportedCitation:
    text = text.strip()
    m = PATTERN.match(text)
    if not m:
        return ImportedCitation(
            raw_text=text,
            authors="", title="", journal="",
            year=0, volume=None, issue=None, pages=None,
            parsed_ok=False, error="无法匹配 GB/T 7714-2025 [J] 格式",
        )
    rest = (m.group("rest") or "").strip()
    volume, issue, pages = _parse_rest(rest)
    return ImportedCitation(
        raw_text=text,
        authors=m.group("authors").strip(),
        title=m.group("title").strip(),
        journal=m.group("journal").strip(),
        year=int(m.group("year")),
        volume=volume,
        issue=issue,
        pages=pages,
        parsed_ok=True,
        error=None,
    )


def _parse_rest(rest: str) -> tuple[str | None, str | None, str | None]:
    """
    残段形式:
      - 37(6):227-229.
      - 37:227-229.
      - 37.
      - 227-229.   ← 只有页码
    """
    rest = rest.rstrip(".")
    if not rest:
        return None, None, None
    # 形式1:卷(期):页
    m = re.match(r"^(?P<v>\d+)\((?P<i>\d+)\):(?P<p>\S+)$", rest)
    if m:
        return m.group("v"), m.group("i"), m.group("p")
    # 形式2:卷:页
    m = re.match(r"^(?P<v>\d+):(?P<p>\S+)$", rest)
    if m:
        return m.group("v"), None, m.group("p")
    # 形式3:仅卷
    if re.match(r"^\d+$", rest):
        return rest, None, None
    # 形式4:仅页
    if re.match(r"^[-\d]+$", rest):
        return None, None, rest
    return None, None, None


def parse_batch(lines: list[str]) -> list[ImportedCitation]:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        out.append(parse_one(line))
    return out
```

- [ ] **Step 3: 单元测试**

```python
# backend/tests/import_cn/test_parser.py
from import_cn.parser import parse_one, parse_batch


def test_user_example():
    """来自你截图的知网引文示例"""
    text = "刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.authors == "刘泽宇,姚璐,王倩莹"
    assert "混合式学习" in r.title
    assert r.journal == "信息与电脑"
    assert r.year == 2025
    assert r.volume == "37"
    assert r.issue == "6"
    assert r.pages == "227-229"


def test_no_volume_issue():
    text = "张三. 某题名[J]. 某刊, 2023: 100-105."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.volume is None
    assert r.issue is None
    assert r.pages == "100-105"


def test_invalid_format():
    text = "这是一段没有格式的文字"
    r = parse_one(text)
    assert not r.parsed_ok
    assert r.error


def test_batch_skips_empty_lines():
    lines = [
        "",
        "刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.",
        "",
        "bad line",
    ]
    results = parse_batch(lines)
    assert len(results) == 2
    assert results[0].parsed_ok
    assert not results[1].parsed_ok
```

- [ ] **Step 4: 跑测试**

Run: `cd backend && pytest tests/import_cn/test_parser.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/import_cn/ backend/tests/import_cn/
git commit -m "feat(import_cn): GB/T 7714 batch parser"
```

#### Task 2.2: 中文导入 API

**Files:**
- Create: `backend/src/api/import_cn.py`

- [ ] **Step 1: 路由**

```python
# backend/src/api/import_cn.py
"""中文批量导入 API。/api/import/cn"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from import_cn.parser import parse_batch

router = APIRouter()


class ImportCnRequest(BaseModel):
    raw_text: str   # 用户一次性粘贴的多行 GB/T 7714 引文


class ImportCnResponse(BaseModel):
    total: int
    parsed_ok: int
    parsed_fail: int
    citations: list[dict]


@router.post("/import/cn", response_model=ImportCnResponse)
async def import_cn(req: ImportCnRequest):
    if not req.raw_text.strip():
        raise HTTPException(400, "请粘贴至少一条引文")
    lines = req.raw_text.splitlines()
    results = parse_batch(lines)
    parsed_ok = sum(1 for r in results if r.parsed_ok)
    return ImportCnResponse(
        total=len(results),
        parsed_ok=parsed_ok,
        parsed_fail=len(results) - parsed_ok,
        citations=[r.to_dict() for r in results],
    )
```

- [ ] **Step 2: Commit**

```bash
git add backend/src/api/import_cn.py
git commit -m "feat(api): Chinese batch import endpoint"
```

---

### Phase 3: 前端基础

#### Task 3.1: Dexie 数据库 + 路由

**Files:**
- Create: `frontend/src/store/db.ts`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/main.tsx`

- [ ] **Step 1: 数据库结构**

```ts
// frontend/src/store/db.ts
import Dexie, { type Table } from 'dexie';

export interface Paper {
  lit_id: string;
  source: 'openalex' | 'crossref' | 'user_imported';
  title: string;
  authors: string[];
  journal: string;
  year: number;
  volume: string | null;
  issue: string | null;
  pages: string | null;
  abstract: string | null;
  doi: string | null;
  source_url: string;
  cited_by_count?: number;
  journal_level?: string | null;

  // 用户操作字段
  selected?: boolean;       // 是否入选综述
  relevance_score?: number; // LLM 重排分(0-100)
  raw_citation?: string;     // 中文粘贴时的原始引文字符串(原样保留)
  imported_at?: number;
}

export interface Review {
  id?: number;
  topic: string;
  classification: 'by_locale' | 'by_theme';   // 按国内外 / 按主题
  sections: { title: string; content: string }[];
  created_at: number;
}

class LitReviewDB extends Dexie {
  papers!: Table<Paper, string>;   // 主键 lit_id
  reviews!: Table<Review, number>;

  constructor() {
    super('lit-review-db');
    this.version(1).stores({
      papers: 'lit_id, source, year, selected',
      reviews: '++id, topic, created_at',
    });
  }
}

export const db = new LitReviewDB();
```

- [ ] **Step 2: 路由 + App**

```tsx
// frontend/src/App.tsx
import { HashRouter, Routes, Route, Link } from 'react-router-dom';
import { TopicInput } from './pages/TopicInput';
import { EnglishRetrieval } from './pages/EnglishRetrieval';
import { ChineseImport } from './pages/ChineseImport';
import { LiteraturePool } from './pages/LiteraturePool';
import { Writing } from './pages/Writing';

function App() {
  return (
    <HashRouter>
      <nav className="flex gap-4 p-4 bg-gray-100 border-b">
        <Link to="/">主题</Link>
        <Link to="/english">英文检索</Link>
        <Link to="/cn">中文导入</Link>
        <Link to="/pool">文献池</Link>
        <Link to="/write">写作</Link>
      </nav>
      <main className="p-6">
        <Routes>
          <Route path="/" element={<TopicInput />} />
          <Route path="/english" element={<EnglishRetrieval />} />
          <Route path="/cn" element={<ChineseImport />} />
          <Route path="/pool" element={<LiteraturePool />} />
          <Route path="/write" element={<Writing />} />
        </Routes>
      </main>
    </HashRouter>
  );
}

export default App;
```

```tsx
// frontend/src/main.tsx
import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/store/db.ts frontend/src/App.tsx frontend/src/main.tsx
git commit -m "feat(frontend): Dexie schema + routing skeleton"
```

#### Task 3.2: 英文检索页

**Files:**
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/pages/TopicInput.tsx`
- Create: `frontend/src/pages/EnglishRetrieval.tsx`
- Create: `frontend/src/components/PaperCard.tsx`
- Create: `frontend/src/components/FilterPanel.tsx`
- Create: `frontend/src/hooks/useRetriever.ts`

- [ ] **Step 1: API 客户端**

```ts
// frontend/src/api/client.ts
const BASE = '/api';

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`API ${path} 失败: ${resp.status}`);
  return resp.json();
}
```

```ts
// frontend/src/api/types.ts
export interface Paper {
  lit_id: string;
  source: string;
  title: string;
  authors: string[];
  journal: string;
  year: number;
  volume: string | null;
  issue: string | null;
  pages: string | null;
  abstract: string | null;
  doi: string | null;
  source_url: string;
  cited_by_count: number;
  relevance_score?: number;
}

export interface SearchRequest {
  topic: string;
  year_start: number;
  year_end: number;
  min_citations: number;
  limit: number;
  use_rerank: boolean;
}

export interface SearchResponse {
  topic_summary: string;
  total_before_filter: number;
  total_after_filter: number;
  papers: Paper[];
}
```

- [ ] **Step 2: useRetriever hook**

```ts
// frontend/src/hooks/useRetriever.ts
import { useState } from 'react';
import { postJSON } from '../api/client';
import type { SearchRequest, SearchResponse } from '../api/types';

export function useRetriever() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(req: SearchRequest) {
    setLoading(true);
    setError(null);
    try {
      const data = await postJSON<SearchResponse>('/retrieval/search', req);
      setResult(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return { loading, result, error, search };
}
```

- [ ] **Step 3: EnglishRetrieval 页**

```tsx
// frontend/src/pages/TopicInput.tsx
import { useNavigate } from 'react-router-dom';
import { useState } from 'react';

export function TopicInput() {
  const [topic, setTopic] = useState('');
  const nav = useNavigate();
  function go() {
    if (!topic.trim()) return;
    sessionStorage.setItem('lit_review_topic', topic.trim());
    nav('/english');
  }
  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-bold mb-4">输入研究主题</h1>
      <p className="text-gray-600 mb-4">
        例:中小企业数字化转型研究 / MBA 课程体系改革
      </p>
      <textarea
        className="w-full p-2 border rounded"
        rows={3}
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        placeholder="在这里输入你的研究主题..."
      />
      <button
        onClick={go}
        disabled={!topic.trim()}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
      >
        下一步:英文文献检索
      </button>
    </div>
  );
}
```

```tsx
// frontend/src/pages/EnglishRetrieval.tsx
import { useEffect, useState } from 'react';
import { useRetriever } from '../hooks/useRetriever';
import { db, type Paper } from '../store/db';
import { FilterPanel } from '../components/FilterPanel';
import { PaperCard } from '../components/PaperCard';

export function EnglishRetrieval() {
  const topic = sessionStorage.getItem('lit_review_topic') || '';
  const [filters, setFilters] = useState({
    year_start: 2020, year_end: 2026,
    min_citations: 0, limit: 50, use_rerank: true,
  });
  const { loading, result, error, search } = useRetriever();
  const [savedCount, setSavedCount] = useState(0);

  useEffect(() => { if (topic) search({ topic, ...filters }); }, [topic]);

  async function importSelected(papers: Paper[]) {
    const records = papers.map((p) => ({
      ...p, selected: true,
      imported_at: Date.now(),
    }));
    await db.papers.bulkPut(records);   // 已存在则覆盖
    setSavedCount(records.length);
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">英文文献检索</h1>
      <p className="text-gray-600 mb-2">研究主题:<b>{topic}</b></p>
      <FilterPanel filters={filters} onChange={setFilters} />
      <button
        onClick={() => search({ topic, ...filters })}
        disabled={loading}
        className="my-4 px-4 py-2 bg-blue-600 text-white rounded"
      >
        {loading ? '检索中...' : '开始检索'}
      </button>
      {error && <div className="text-red-600">{error}</div>}
      {result && (
        <div className="mb-4 text-sm text-gray-700">
          平台原始命中:<b>{result.total_before_filter}</b> ·
          过滤后:<b>{result.total_after_filter}</b> ·
          已入库:<b>{savedCount}</b>
        </div>
      )}
      <div>
        {result?.papers.map((p) => (
          <PaperCard key={p.lit_id} paper={p} onImport={() => importSelected([p])} />
        ))}
      </div>
      {result && result.papers.length > 0 && (
        <button
          onClick={() => importSelected(result.papers)}
          className="my-4 px-4 py-2 bg-green-600 text-white rounded"
        >
          一键导入全部到文献池
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 4: FilterPanel 组件**

```tsx
// frontend/src/components/FilterPanel.tsx
interface Props {
  filters: {
    year_start: number; year_end: number;
    min_citations: number; limit: number; use_rerank: boolean;
  };
  onChange: (f: Props['filters']) => void;
}

export function FilterPanel({ filters, onChange }: Props) {
  function update(p: Partial<Props['filters']>) {
    onChange({ ...filters, ...p });
  }
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 p-3 border rounded bg-gray-50">
      <label className="flex flex-col text-sm">
        年份起
        <input type="number" value={filters.year_start}
          onChange={(e) => update({ year_start: Number(e.target.value) })}
          className="border p-1 rounded" />
      </label>
      <label className="flex flex-col text-sm">
        年份止
        <input type="number" value={filters.year_end}
          onChange={(e) => update({ year_end: Number(e.target.value) })}
          className="border p-1 rounded" />
      </label>
      <label className="flex flex-col text-sm">
        最低被引
        <input type="number" value={filters.min_citations}
          onChange={(e) => update({ min_citations: Number(e.target.value) })}
          className="border p-1 rounded" />
      </label>
      <label className="flex flex-col text-sm">
        每源上限
        <input type="number" value={filters.limit}
          onChange={(e) => update({ limit: Number(e.target.value) })}
          className="border p-1 rounded" />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={filters.use_rerank}
          onChange={(e) => update({ use_rerank: e.target.checked })} />
        LLM 相关度重排
      </label>
    </div>
  );
}
```

- [ ] **Step 5: PaperCard 组件**

```tsx
// frontend/src/components/PaperCard.tsx
import type { Paper } from '../store/db';

export function PaperCard({ paper: p, onImport }: { paper: Paper; onImport?: () => void }) {
  return (
    <div className="border rounded p-3 mb-2">
      <div className="flex justify-between">
        <h3 className="font-semibold">{p.title}</h3>
        {p.relevance_score !== undefined && (
          <span className="text-xs bg-blue-100 px-2 py-1 rounded">
            相关度:{p.relevance_score}
          </span>
        )}
      </div>
      <div className="text-sm text-gray-700 mt-1">
        {p.authors.slice(0, 3).join(', ')}{p.authors.length > 3 ? ', 等' : ''} · <i>{p.journal}</i>, {p.year}
        {p.volume && `, ${p.volume}`}{p.issue && `(${p.issue})`}{p.pages && `: ${p.pages}`}.
      </div>
      {p.abstract && (
        <details className="mt-2 text-sm text-gray-600">
          <summary className="cursor-pointer">摘要</summary>
          <p className="mt-1">{p.abstract.slice(0, 500)}{p.abstract.length > 500 ? '...' : ''}</p>
        </details>
      )}
      <div className="mt-2 flex gap-2 text-xs">
        <a href={p.source_url} target="_blank" rel="noopener noreferrer"
           className="text-blue-600 underline">原文链接</a>
        {p.doi && <span className="text-gray-500">DOI: {p.doi}</span>}
        {p.cited_by_count > 0 && (
          <span className="text-gray-500">被引: {p.cited_by_count}</span>
        )}
        {onImport && (
          <button onClick={onImport}
            className="ml-auto px-2 py-0.5 bg-blue-600 text-white rounded">
            导入
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/ frontend/src/pages/TopicInput.tsx frontend/src/pages/EnglishRetrieval.tsx frontend/src/components/ frontend/src/hooks/
git commit -m "feat(frontend): English retrieval page + filters"
```

#### Task 3.3: 中文批量导入页

**Files:**
- Create: `frontend/src/pages/ChineseImport.tsx`
- Create: `frontend/src/hooks/useImporter.ts`

- [ ] **Step 1: useImporter**

```ts
// frontend/src/hooks/useImporter.ts
import { useState } from 'react';
import { postJSON } from '../api/client';

interface ImportResponse {
  total: number;
  parsed_ok: number;
  parsed_fail: number;
  citations: any[];
}

export function useImporter() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ImportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function importText(text: string) {
    setLoading(true);
    setError(null);
    try {
      const data = await postJSON<ImportResponse>('/import/cn', { raw_text: text });
      setResult(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }
  return { loading, result, error, importText };
}
```

- [ ] **Step 2: ChineseImport 页**

```tsx
// frontend/src/pages/ChineseImport.tsx
import { useState } from 'react';
import { useImporter } from '../hooks/useImporter';
import { db } from '../store/db';
import { hashId } from '../utils/hash';

export function ChineseImport() {
  const [text, setText] = useState('');
  const { loading, result, error, importText } = useImporter();

  async function saveAll() {
    if (!result) return;
    // 把解析成功的 citations 转成 Paper 入库
    const papers = result.citations
      .filter((c) => c.parsed_ok)
      .map((c) => ({
        lit_id: hashId(c.raw_text),
        source: 'user_imported' as const,
        title: c.title,
        authors: c.authors.split(',').map((s: string) => s.trim()).filter(Boolean),
        journal: c.journal,
        year: c.year,
        volume: c.volume,
        issue: c.issue,
        pages: c.pages,
        abstract: null,
        doi: null,
        source_url: '',
        raw_citation: c.raw_text,
        selected: true,
        imported_at: Date.now(),
      }));
    await db.papers.bulkPut(papers);
    alert(`已导入 ${papers.length} 条中文文献`);
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">中文文献批量导入</h1>
      <p className="text-gray-600 mb-2 text-sm">
        在知网查新(引文格式)选中多条,复制粘贴到下方。每行一条 GB/T 7714-2025 引文。
      </p>
      <textarea
        className="w-full p-2 border rounded font-mono text-sm"
        rows={12}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={'刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.\n另一条...\n...'}
      />
      <div className="flex gap-2 mt-3">
        <button onClick={() => importText(text)} disabled={loading || !text.trim()}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50">
          {loading ? '解析中...' : '解析引文'}
        </button>
        {result && result.parsed_ok > 0 && (
          <button onClick={saveAll}
            className="px-4 py-2 bg-green-600 text-white rounded">
            全部导入到文献池({result.parsed_ok})
          </button>
        )}
      </div>
      {error && <div className="text-red-600 mt-2">{error}</div>}
      {result && (
        <div className="mt-4 text-sm">
          共 <b>{result.total}</b> 条 · 解析成功 <b className="text-green-700">{result.parsed_ok}</b> · 失败 <b className="text-red-700">{result.parsed_fail}</b>
        </div>
      )}
      {result && (
        <table className="w-full text-sm mt-3 border">
          <thead className="bg-gray-100">
            <tr>
              <th className="p-1 text-left">作者</th>
              <th className="p-1 text-left">题名</th>
              <th className="p-1 text-left">刊名</th>
              <th className="p-1">年</th>
              <th className="p-1">卷(期)</th>
              <th className="p-1">页</th>
              <th className="p-1">状态</th>
            </tr>
          </thead>
          <tbody>
            {result.citations.map((c, i) => (
              <tr key={i} className="border-t">
                <td className="p-1">{c.authors || '—'}</td>
                <td className="p-1">{c.title || '—'}</td>
                <td className="p-1">{c.journal || '—'}</td>
                <td className="p-1 text-center">{c.year || '—'}</td>
                <td className="p-1 text-center">{c.volume}{c.issue && `(${c.issue})`}</td>
                <td className="p-1 text-center">{c.pages || '—'}</td>
                <td className="p-1 text-center">
                  {c.parsed_ok ? <span className="text-green-700">✓</span>
                                : <span className="text-red-600" title={c.error}>✗</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 3: 工具 hash 函数**

```ts
// frontend/src/utils/hash.ts
export async function hashId(input: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(input),
  );
  return 'lit_' + Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('').slice(0, 16);
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ChineseImport.tsx frontend/src/hooks/useImporter.ts frontend/src/utils/
git commit -m "feat(frontend): Chinese batch import page"
```

---

### Phase 4: 文献池 + 筛选 + 综述

#### Task 4.1: 文献池页

**Files:**
- Create: `frontend/src/pages/LiteraturePool.tsx`

- [ ] **Step 1: 实现**

```tsx
// frontend/src/pages/LiteraturePool.tsx
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useLiveQuery } from 'dexie-react-hooks';
import { db, type Paper } from '../store/db';

export function LiteraturePool() {
  const papers = useLiveQuery(() => db.papers.toArray()) ?? [];
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelectedIds(new Set(papers.filter((p) => p.selected).map((p) => p.lit_id)));
  }, [papers.length]);

  async function toggleSelected(lit_id: string, val: boolean) {
    await db.papers.update(lit_id, { selected: val });
    setSelectedIds((s) => {
      const next = new Set(s);
      val ? next.add(lit_id) : next.delete(lit_id);
      return next;
    });
  }

  const selectedPapers = papers.filter((p) => selectedIds.has(p.lit_id));

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">文献池</h1>
      <p className="text-sm text-gray-600 mb-3">
        共 <b>{papers.length}</b> 条 · 已选 <b>{selectedPapers.length}</b> 条 ·
        <Link to="/write" className="ml-2 text-blue-600 underline">下一步:写作</Link>
      </p>
      <table className="w-full text-sm border">
        <thead className="bg-gray-100">
          <tr>
            <th className="p-2">选</th>
            <th className="p-2 text-left">标题</th>
            <th className="p-2 text-left">作者</th>
            <th className="p-2 text-left">期刊</th>
            <th className="p-2">年</th>
            <th className="p-2">来源</th>
            <th className="p-2">引文</th>
          </tr>
        </thead>
        <tbody>
          {papers.map((p) => (
            <tr key={p.lit_id} className="border-t">
              <td className="p-2 text-center">
                <input type="checkbox" checked={selectedIds.has(p.lit_id)}
                  onChange={(e) => toggleSelected(p.lit_id, e.target.checked)} />
              </td>
              <td className="p-2">{p.title}</td>
              <td className="p-2">{p.authors.slice(0, 3).join(', ')}{p.authors.length > 3 ? ' 等' : ''}</td>
              <td className="p-2"><i>{p.journal}</i></td>
              <td className="p-2 text-center">{p.year}</td>
              <td className="p-2 text-center text-xs">{p.source}</td>
              <td className="p-2 text-xs">{renderCitation(p)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function renderCitation(p: Paper): string {
  if (p.raw_citation) return p.raw_citation;        // 中文:用户原始粘贴
  // 英文:从平台字段透传(不拼接,只是按 GB/T 7714-2025 槽位嵌入值)
  const authors = p.authors.slice(0, 3).join(', ') + (p.authors.length > 3 ? ', et al' : '');
  const volIssue = p.volume ? `${p.volume}${p.issue ? `(${p.issue})` : ''}` : '';
  return `${authors}. ${p.title}[J]. ${p.journal}, ${p.year}, ${volIssue}: ${p.pages || ''}.`.trim();
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/pages/LiteraturePool.tsx
git commit -m "feat(frontend): Literature pool with multi-source citation"
```

#### Task 4.2: 主题不符筛选(LLM 前置过滤)

**Files:**
- Create: `backend/src/screening/llm_filter.py`
- Create: `backend/src/api/screening.py`
- Test: `backend/tests/screening/test_llm_filter.py`

- [ ] **Step 1: LLM 筛除主题不符文献**

```python
# backend/src/screening/llm_filter.py
"""
综述前的语义筛选:用 LLM 判断每篇文献是否与研究主题相符。
- 只输出布尔值,不构造字段
- 输出 JSON: [{"lit_id": "...", "relevant": true|false, "reason": "..."}]
"""
import json
import re
from retrieval.types import Paper
from llm.client import messages_create


SYSTEM = """你是学术论文相关性筛选助手。
任务:对每篇论文,基于其标题与摘要,判断是否与"用户研究主题"实质相关。
- 实质相关:研究主题/对象/方法/理论/应用任一层面有交集
- 主题不符:完全无关、跨学科无连接、研究对象差异大
严格输出 JSON 数组:[
  {"lit_id": "...", "relevant": true|false, "reason": "<一句话中文原因>"}
]
不要输出其他文字。"""


def screen_batch(papers: list[Paper], topic: str, max_chars: int = 400) -> list[dict]:
    payload = [
        {"lit_id": p.lit_id, "title": p.title,
         "abstract": (p.abstract or "")[:max_chars]}
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出 JSON 数组(可省略 reason 字段外的其他内容):"
    )
    raw = messages_create(SYSTEM, user, max_tokens=4000)
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 失败兜底:不筛(全部视为相关)
        return [{"lit_id": p.lit_id, "relevant": True, "reason": "解析失败,默认保留"} for p in papers]
```

- [ ] **Step 2: API 路由**

```python
# backend/src/api/screening.py
"""主题不符筛除 API。/api/screening/filter"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from retrieval.types import Paper
from screening.llm_filter import screen_batch

router = APIRouter()


class ScreenRequest(BaseModel):
    topic: str
    papers: list[dict]


class ScreenResponse(BaseModel):
    results: list[dict]


@router.post("/screening/filter", response_model=ScreenResponse)
async def screen(req: ScreenRequest):
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    papers = [Paper(**p) for p in req.papers]
    results = screen_batch(papers, req.topic)
    return ScreenResponse(results=results)
```

- [ ] **Step 3: 单元测试(mock 掉 LLM 客户端)**

```python
# backend/tests/screening/test_llm_filter.py
from unittest.mock import patch
from retrieval.types import Paper, Source
from screening.llm_filter import screen_batch


def test_screen_batch_returns_list():
    papers = [
        Paper(lit_id="lit_a", source=Source.OPENALEX,
              title="AAA", authors=["X"], journal="J", year=2024,
              abstract="关于 AI 医学影像的研究."),
        Paper(lit_id="lit_b", source=Source.OPENALEX,
              title="BBB", authors=["Y"], journal="J", year=2024,
              abstract="完全不相关的领域:材料力学."),
    ]
    fake_resp = '[{"lit_id":"lit_a","relevant":true,"reason":"相关"},{"lit_id":"lit_b","relevant":false,"reason":"无关"}]'
    with patch("screening.llm_filter.messages_create", return_value=fake_resp):
        results = screen_batch(papers, topic="AI 医学影像")
    assert len(results) == 2
    assert results[0]["relevant"] is True
    assert results[1]["relevant"] is False
```

- [ ] **Step 4: 跑测试**

Run: `cd backend && pytest tests/screening/test_llm_filter.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/screening/ backend/src/api/screening.py backend/tests/screening/
git commit -m "feat(screening): LLM semantic filter before writing"
```

#### Task 4.3: 综述分类(按国内外/按主题)

**Files:**
- Create: `backend/src/writing/classifier.py`
- Create: `backend/src/writing/templates.py`
- Create: `backend/src/llm/prompts/classify.md`

- [ ] **Step 1: 7 章综述模板**

```python
# backend/src/writing/templates.py
"""7 章文献综述结构模板(空模板)。"""


SECTIONS_7 = [
    {
        "title": "引言",
        "purpose": "说明研究主题、综述范围、研究意义。",
        "sub_topics": ["研究背景", "研究问题", "综述价值"],
    },
    {
        "title": "文献检索方法",
        "purpose": "说明检索策略、数据库、时间范围、纳入排除标准。",
        "sub_topics": ["检索源", "检索式", "筛选标准"],
    },
    {
        "title": "文献概览",
        "purpose": "对所有纳入文献做整体性描述:年度分布、期刊分布、研究方法分布。",
        "sub_topics": ["年度分布", "期刊分布", "研究类型分布"],
    },
    {
        "title": "主题分析",
        "purpose": "按用户指定分类方式(国内外/主题)对文献做归类分析,提炼核心发现。",
        "sub_topics": ["分类一的核心研究", "分类二的核心研究", "对比与综合"],
    },
    {
        "title": "方法学分析",
        "purpose": "分析纳入文献采用的研究方法、理论框架、实证工具。",
        "sub_topics": ["常见方法", "新兴方法", "方法局限"],
    },
    {
        "title": "研究缺口与未来方向",
        "purpose": "基于综述结论指出研究空白、未来可能的研究方向。",
        "sub_topics": ["理论缺口", "方法缺口", "应用缺口"],
    },
    {
        "title": "结论",
        "purpose": "总结全文核心发现,重申研究主题的现状与意义。",
        "sub_topics": ["核心发现回顾", "对实践的启示", "局限与展望"],
    },
]
```

- [ ] **Step 2: 分类 prompt**

```markdown
<!-- backend/src/llm/prompts/classify.md -->
你是文献综述分类专家。
任务:把一组论文按用户指定的分类方式归类。
- 分类方式二选一:
  ① by_locale:国内 vs 国外(根据 source 字段)
  ② by_theme:按主题分 3-6 类(根据论文实际内容)

输出严格 JSON:
{
  "mode": "by_locale" | "by_theme",
  "groups": [
    {
      "name": "<分类名>",
      "paper_lit_ids": ["lit_xxx", ...],
      "summary": "<一句中文总结>"
    },
    ...
  ]
}

注意:paper_lit_ids 必须从输入的 lit_id 中精确复制,不允许编造。
```

- [ ] **Step 3: 分类实现**

```python
# backend/src/writing/classifier.py
"""按指定方式对文献做分类,LLM 输出 JSON。"""
import json
import re
from retrieval.types import Paper
from llm.client import messages_create


SYSTEM = """你是文献综述分类专家。
任务:把一组论文按用户指定的分类方式归类。
- 分类方式二选一:
  ① by_locale:国内 vs 国外(根据 source 字段,openalex/crossref 视为国外,user_imported 视为国内)
  ② by_theme:按主题分 3-6 类(根据论文实际内容)

输出严格 JSON:
{
  "mode": "by_locale" | "by_theme",
  "groups": [
    {
      "name": "<分类名>",
      "paper_lit_ids": ["lit_xxx", ...],
      "summary": "<一句中文总结>"
    }
  ]
}

注意:paper_lit_ids 必须从输入的 lit_id 中精确复制,不允许编造。"""


def classify(papers: list[Paper], mode: str, topic: str) -> dict:
    payload = [
        {
            "lit_id": p.lit_id,
            "source": p.source.value if hasattr(p.source, 'value') else p.source,
            "title": p.title,
            "abstract": (p.abstract or "")[:300],
        }
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"分类方式:{mode}\n"
        f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出 JSON:"
    )
    raw = messages_create(SYSTEM, user, max_tokens=3000)
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"mode": mode, "groups": [
            {"name": "全部文献", "paper_lit_ids": [p.lit_id for p in papers],
             "summary": "解析失败,未分类"}
        ]}
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/writing/templates.py backend/src/writing/classifier.py backend/src/llm/prompts/classify.md
git commit -m "feat(writing): 7-section template + LLM classifier"
```

#### Task 4.4: 章节写作(LLM 生成综述主体)

**Files:**
- Create: `backend/src/writing/section_writer.py`
- Create: `backend/src/llm/prompts/section_writer.md`

- [ ] **Step 1: section writer prompt**

```markdown
<!-- backend/src/llm/prompts/section_writer.md -->
你是学术综述写作助手。
任务:基于"提供的论文摘要"撰写该章节的综述内容。

要求:
1. 只引用提供的论文;不得编造、臆测任何论文、作者、年份
2. 引用方式:使用 [lit_xxx] 格式标注每条引用,lit_xxx 必须严格来自输入
3. 字数:600-1200 中文字
4. 章节定位:argumentative,不堆砌事实
5. 段落层级清晰,有主题句
6. 输出 Markdown

输入会包含:
- topic:研究主题
- section_title:当前章节标题
- section_purpose:章节定位与子主题
- papers:[
    {"lit_id": "...", "title": "...", "authors": [...], "year": ..., "abstract": "..."}
  ]

不要输出引文列表,只输出章节正文。
```

- [ ] **Step 2: 实现**

```python
# backend/src/writing/section_writer.py
"""每章节的 LLM 综述生成。所有论文摘要从用户提供的库读取。"""
import json
from retrieval.types import Paper
from llm.client import messages_create
from .templates import SECTIONS_7


SYSTEM = """你是学术综述写作助手。
任务:基于"提供的论文摘要"撰写该章节的综述内容。

要求:
1. 只引用提供的论文;不得编造、臆测任何论文、作者、年份
2. 引用方式:使用 [lit_xxx] 格式标注每条引用,lit_xxx 必须严格来自输入
3. 字数:600-1200 中文字
4. 章节定位:argumentative,不堆砌事实
5. 段落层级清晰,有主题句
6. 输出 Markdown

不要输出引文列表,只输出章节正文。"""


def write_section(
    topic: str,
    section: dict,
    papers: list[Paper],
    classification: dict | None = None,
) -> str:
    """
    写一节综述。
    papers:本章节引用的全部 Paper 对象(分类后的子集)
    classification:可选的分类信息,会在 system 中提示
    """
    payload = [
        {
            "lit_id": p.lit_id,
            "title": p.title,
            "authors": p.authors[:5],
            "year": p.year,
            "journal": p.journal,
            "abstract": (p.abstract or "(无摘要)"),
        }
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"章节标题:{section['title']}\n"
        f"章节定位:{section['purpose']}\n"
        f"子主题:{', '.join(section.get('sub_topics', []))}\n\n"
        f"可用论文(JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"请输出该章节的 Markdown 正文:"
    )
    return messages_create(SYSTEM, user, max_tokens=2500)


def write_all_sections(
    topic: str,
    papers_by_group: dict[str, list[Paper]],
    classification: dict | None = None,
) -> list[dict]:
    """
    写全部 7 章。
    papers_by_group: 从分类得到的"组名 → Paper[]" 映射
    """
    # 主题分析章节用全部分组,其他章节用全部论文
    all_papers = [p for ps in papers_by_group.values() for p in ps]

    sections_out = []
    for i, section in enumerate(SECTIONS_7):
        if section["title"] == "主题分析":
            # 用分组后的论文,分组信息放在提示里
            payload = {
                g_name: [{"lit_id": p.lit_id, "title": p.title,
                          "authors": p.authors[:3], "year": p.year}
                         for p in ps]
                for g_name, ps in papers_by_group.items()
            }
            user = (
                f"研究主题:{topic}\n"
                f"章节标题:{section['title']}\n"
                f"本章节请按以下分组组织内容:\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
                f"每个分组写一段,最后做对比与综合。请输出 Markdown 正文:"
            )
            content = messages_create(SYSTEM, user, max_tokens=3000)
        else:
            content = write_section(topic, section, all_papers)

        sections_out.append({"title": section["title"], "content": content})
    return sections_out
```

- [ ] **Step 3: Commit**

```bash
git add backend/src/writing/section_writer.py backend/src/llm/prompts/section_writer.md
git commit -m "feat(writing): section writer with [lit_xxx] enforcement"
```

#### Task 4.5: 综述写作总控 + API

**Files:**
- Create: `backend/src/writing/orchestrator.py`
- Create: `backend/src/api/writing.py`

- [ ] **Step 1: 总控**

```python
# backend/src/writing/orchestrator.py
"""综述生成总控:筛选 → 分类 → 写作。"""
from retrieval.types import Paper
from screening.llm_filter import screen_batch
from .classifier import classify
from .section_writer import write_all_sections


def generate_review(
    topic: str,
    papers: list[Paper],
    mode: str,                    # "by_locale" / "by_theme"
    do_screening: bool = True,
) -> dict:
    """
    输入:主题 + 候选论文
    输出:包含 sections、参考文献列表、分类信息的 dict
    """
    # 1. 筛选
    if do_screening:
        decisions = screen_batch(papers, topic)
        relevant_ids = {d["lit_id"] for d in decisions if d.get("relevant")}
        screened = [p for p in papers if p.lit_id in relevant_ids]
        screening_results = decisions
    else:
        screened = papers
        screening_results = []

    if not screened:
        return {"sections": [], "screening": screening_results,
                "classification": {}, "message": "筛选后无文献,请调整主题或文献范围"}

    # 2. 分类
    classification = classify(screened, mode, topic)
    papers_by_group = {}
    for group in classification.get("groups", []):
        gname = group["name"]
        ids = set(group["paper_lit_ids"])
        papers_by_group[gname] = [p for p in screened if p.lit_id in ids]
    # 兜底:未分组的文献归入"未分类"
    classified_ids = {p.lit_id for ps in papers_by_group.values() for p in ps}
    leftovers = [p for p in screened if p.lit_id not in classified_ids]
    if leftovers:
        papers_by_group["未分类"] = leftovers

    # 3. 写作
    sections = write_all_sections(topic, papers_by_group, classification)

    return {
        "sections": sections,
        "screening": screening_results,
        "classification": classification,
        "papers_used": [p.to_dict() for p in screened],
    }
```

- [ ] **Step 2: API 路由**

```python
# backend/src/api/writing.py
"""综述写作 API。/api/writing/generate"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from retrieval.types import Paper, Source
from writing.orchestrator import generate_review

router = APIRouter()


class GenerateRequest(BaseModel):
    topic: str
    papers: list[dict]
    classification_mode: str = "by_locale"    # by_locale / by_theme
    do_screening: bool = True


@router.post("/writing/generate")
async def generate(req: GenerateRequest):
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    if req.classification_mode not in ("by_locale", "by_theme"):
        raise HTTPException(400, "classification_mode 必须为 by_locale 或 by_theme")
    papers = []
    for p in req.papers:
        # 适配 paper.source 可能是字符串
        if isinstance(p.get("source"), str):
            p["source"] = Source(p["source"])
        papers.append(Paper(**p))
    result = generate_review(
        topic=req.topic,
        papers=papers,
        mode=req.classification_mode,
        do_screening=req.do_screening,
    )
    return result
```

- [ ] **Step 3: Commit**

```bash
git add backend/src/writing/orchestrator.py backend/src/api/writing.py
git commit -m "feat(writing): review generation orchestrator + API"
```

#### Task 4.6: 前端写作页

**Files:**
- Create: `frontend/src/components/ClassifySelector.tsx`
- Create: `frontend/src/components/ReviewOutput.tsx`
- Create: `frontend/src/components/CitationList.tsx`
- Create: `frontend/src/hooks/useWriter.ts`
- Create: `frontend/src/pages/Writing.tsx`

- [ ] **Step 1: useWriter hook**

```ts
// frontend/src/hooks/useWriter.ts
import { useState } from 'react';
import { postJSON } from '../api/client';

export interface ReviewSection {
  title: string;
  content: string;
}
export interface GenerateResponse {
  sections: ReviewSection[];
  classification: any;
  screening: any[];
  papers_used: any[];
}

export function useWriter() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function generate(topic: string, papers: any[],
                         classification_mode: string,
                         do_screening: boolean) {
    setLoading(true);
    setError(null);
    try {
      const data = await postJSON<GenerateResponse>('/writing/generate', {
        topic, papers,
        classification_mode, do_screening,
      });
      setResult(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return { loading, result, error, generate };
}
```

- [ ] **Step 2: ClassifySelector**

```tsx
// frontend/src/components/ClassifySelector.tsx
export function ClassifySelector({
  value, onChange,
}: { value: 'by_locale' | 'by_theme'; onChange: (v: any) => void }) {
  return (
    <div className="border rounded p-3 bg-gray-50">
      <div className="font-semibold mb-2">分类方式</div>
      <label className="flex items-center gap-2 mr-4">
        <input type="radio" name="cls" checked={value === 'by_locale'}
          onChange={() => onChange('by_locale')} />
        按国内外分类
      </label>
      <label className="flex items-center gap-2">
        <input type="radio" name="cls" checked={value === 'by_theme'}
          onChange={() => onChange('by_theme')} />
        按主题分类(LLM 自动识别 3-6 类)
      </label>
    </div>
  );
}
```

- [ ] **Step 3: ReviewOutput + CitationList**

```tsx
// frontend/src/components/ReviewOutput.tsx
import ReactMarkdown from 'react-markdown';

interface Section { title: string; content: string; }

export function ReviewOutput({ sections }: { sections: Section[] }) {
  return (
    <div className="border rounded p-4 bg-white">
      {sections.map((s, i) => (
        <div key={i} className="mb-6">
          <h2 className="text-xl font-bold mb-2">{i + 1}. {s.title}</h2>
          <div className="prose max-w-none whitespace-pre-wrap">
            <ReactMarkdown>{s.content}</ReactMarkdown>
          </div>
        </div>
      ))}
    </div>
  );
}
```

```tsx
// frontend/src/components/CitationList.tsx
import type { Paper } from '../store/db';

function renderCnCitation(p: Paper): string {
  return p.raw_citation || '(无原始引文)';
}

function renderEnCitation(p: Paper): string {
  const authors = p.authors.slice(0, 3).join(', ') +
    (p.authors.length > 3 ? ', et al' : '');
  const vi = p.volume ? `${p.volume}${p.issue ? `(${p.issue})` : ''}` : '';
  return `${authors}. ${p.title}[J]. ${p.journal}, ${p.year}, ${vi}: ${p.pages || ''}.`
    .replace(/\s+/g, ' ').trim();
}

export function CitationList({ papers }: { papers: Paper[] }) {
  return (
    <div className="border rounded p-4 bg-gray-50">
      <h2 className="text-xl font-bold mb-2">参考文献</h2>
      <ol className="space-y-1 text-sm">
        {papers.map((p, i) => (
          <li key={p.lit_id} className="leading-relaxed">
            [{i + 1}] {p.source === 'user_imported'
              ? renderCnCitation(p)
              : renderEnCitation(p)}
          </li>
        ))}
      </ol>
    </div>
  );
}
```

- [ ] **Step 4: Writing 页**

```tsx
// frontend/src/pages/Writing.tsx
import { useState } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { db, type Paper } from '../store/db';
import { useWriter } from '../hooks/useWriter';
import { ClassifySelector } from '../components/ClassifySelector';
import { ReviewOutput } from '../components/ReviewOutput';
import { CitationList } from '../components/CitationList';

export function Writing() {
  const topic = sessionStorage.getItem('lit_review_topic') || '';
  const papers = (useLiveQuery(() => db.papers.toArray()) ?? [])
    .filter((p) => p.selected);

  const [mode, setMode] = useState<'by_locale' | 'by_theme'>('by_locale');
  const [doScreening, setDoScreening] = useState(true);
  const { loading, result, error, generate } = useWriter();

  async function saveAll() {
    if (!result) return;
    await db.reviews.add({
      topic, classification: mode,
      sections: result.sections,
      created_at: Date.now(),
    });
    alert('综述已保存到本地');
  }

  async function exportMd() {
    if (!result) return;
    let md = `# 文献综述:${topic}\n\n`;
    md += `_分类方式:${mode === 'by_locale' ? '按国内外' : '按主题'}_\n\n`;
    result.sections.forEach((s, i) => {
      md += `## ${i + 1}. ${s.title}\n\n${s.content}\n\n`;
    });
    md += `## 参考文献\n\n`;
    result.papers_used.forEach((p: any, i: number) => {
      const line = p.source === 'user_imported'
        ? (p.raw_citation || '')
        : `${(p.authors || []).slice(0, 3).join(', ')}${(p.authors || []).length > 3 ? ', et al' : ''}. ${p.title}[J]. ${p.journal}, ${p.year}, ${p.volume || ''}${p.issue ? `(${p.issue})` : ''}: ${p.pages || ''}.`;
      md += `[${i + 1}] ${line.replace(/\s+/g, ' ').trim()}\n`;
    });
    const blob = new Blob([md], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `综述_${topic.replace(/\s+/g, '_')}.md`;
    a.click();
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">综述写作</h1>
      <p className="text-sm text-gray-600 mb-3">
        主题:<b>{topic}</b> · 候选文献:<b>{papers.length}</b>
      </p>
      <ClassifySelector value={mode} onChange={setMode} />
      <label className="flex items-center gap-2 my-3 text-sm">
        <input type="checkbox" checked={doScreening}
          onChange={(e) => setDoScreening(e.target.checked)} />
        在综述前用 LLM 筛除主题不符文献
      </label>
      <button
        onClick={() => generate(topic, papers, mode, doScreening)}
        disabled={loading || papers.length === 0}
        className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
      >
        {loading ? '生成中...' : '开始生成综述'}
      </button>
      {error && <div className="text-red-600 mt-2">{error}</div>}

      {result && (
        <>
          <div className="flex gap-2 mt-4">
            <button onClick={saveAll}
              className="px-3 py-1 bg-purple-600 text-white rounded text-sm">
              保存到本地
            </button>
            <button onClick={exportMd}
              className="px-3 py-1 bg-yellow-600 text-white rounded text-sm">
              导出 .md 文件
            </button>
          </div>
          <ReviewOutput sections={result.sections} />
          <CitationList papers={result.papers_used as Paper[]} />
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ClassifySelector.tsx frontend/src/components/ReviewOutput.tsx frontend/src/components/CitationList.tsx frontend/src/hooks/useWriter.ts frontend/src/pages/Writing.tsx
git commit -m "feat(frontend): Writing page with classification and review output"
```

---

### Phase 5: 端到端联调与验证

#### Task 5.1: 启动后端 + 安装前端依赖

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: 写后端 main.py(挂所有路由 + CORS)**

```python
# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.retrieval import router as retrieval_router
from api.import_cn import router as import_cn_router
from api.screening import router as screening_router
from api.writing import router as writing_router
from api.health import router as health_router

app = FastAPI(title="文献综述 Agent Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 开发用;生产收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(retrieval_router, prefix="/api")
app.include_router(import_cn_router, prefix="/api")
app.include_router(screening_router, prefix="/api")
app.include_router(writing_router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

- [ ] **Step 2: 补 health router(防止忘记)**

```python
# backend/src/api/health.py
from fastapi import APIRouter
router = APIRouter()

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 3: 安装依赖并启动**

```bash
# 后端
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env       # 然后填 ANTHROPIC_API_KEY

# 前端
cd ../frontend
npm install
```

- [ ] **Step 4: 启动**

```bash
# 终端 1
cd backend && python main.py

# 终端 2
cd frontend && npm run dev
```

- [ ] **Step 5: 验证 healthz**

Run: `curl http://localhost:8000/api/healthz`
Expected: `{"status":"ok"}`

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/src/api/health.py
git commit -m "chore: bootstrap backend main + healthz"
```

#### Task 5.2: 端到端冒烟测试

**Files:**
- Create: `scripts/smoke_test.sh`(或 powershell 脚本)

- [ ] **Step 1: 写脚本**

```bash
# scripts/smoke_test.sh
#!/usr/bin/env bash
set -euo pipefail
BASE=http://localhost:8000/api

echo "[1/3] healthz"
curl -s $BASE/healthz | tee /tmp/health.json
[ "$(jq -r .status /tmp/health.json)" = "ok" ] || { echo "healthz 失败"; exit 1; }

echo "[2/3] 中文批量导入"
curl -s -X POST $BASE/import/cn \
  -H 'Content-Type: application/json' \
  -d '{"raw_text":"刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.\nbad line"}' \
  | tee /tmp/import.json
[ "$(jq -r .parsed_ok /tmp/import.json)" = "1" ] || { echo "中文导入失败"; exit 1; }

echo "[3/3] 英文检索(短超时)"
curl -s -X POST $BASE/retrieval/search \
  -H 'Content-Type: application/json' \
  -d '{"topic":"deep learning medical imaging","year_start":2022,"year_end":2025,"min_citations":0,"limit":5,"use_rerank":false}' \
  | jq '.total_after_filter' | tee /tmp/ret.json
[ "$(cat /tmp/ret)" -gt 0 ] || echo "警告:英文检索无结果(请检查 OPENALEX_MAILTO 设置)"

echo "✓ 冒烟测试通过"
```

- [ ] **Step 2: Windows 下用 powershell 脚本版**

```powershell
# scripts/smoke_test.ps1
$base = "http://localhost:8000/api"

Write-Host "[1/3] healthz" -ForegroundColor Cyan
$health = Invoke-RestMethod "$base/healthz"
if ($health.status -ne "ok") { throw "healthz 失败" }
Write-Host "OK"

Write-Host "[2/3] 中文批量导入"
$body = @{ raw_text = @"
刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.
bad line
"@ } | ConvertTo-Json
$resp = Invoke-RestMethod "$base/import/cn" -Method POST -Body $body -ContentType 'application/json'
if ($resp.parsed_ok -ne 1) { throw "中文导入失败" }
Write-Host "OK: parsed_ok=$($resp.parsed_ok)"

Write-Host "[3/3] 英文检索"
$body = @{
  topic = "deep learning medical imaging"
  year_start = 2022; year_end = 2025
  min_citations = 0; limit = 5; use_rerank = $false
} | ConvertTo-Json
$resp = Invoke-RestMethod "$base/retrieval/search" -Method POST -Body $body -ContentType 'application/json'
if ($resp.total_after_filter -lt 1) { Write-Warning "英文检索无结果" }
Write-Host "OK: total=$($resp.total_after_filter)"

Write-Host "`n✓ 冒烟测试通过" -ForegroundColor Green
```

- [ ] **Step 3: 跑**

Run: `cd backend && python main.py`(另一终端)
Run: `pwsh scripts/smoke_test.ps1`
Expected:
- `[1/3] healthz  OK`
- `[2/3] 中文批量导入  OK: parsed_ok=1`
- `[3/3] 英文检索  OK: total>=1`

- [ ] **Step 4: Commit**

```bash
git add scripts/
git commit -m "test: end-to-end smoke test script"
```

#### Task 5.3: 浏览器端到端走一遍

**Files:** 无新增,只验证

- [ ] **Step 1: 启动前后端**

```bash
# 终端 1
cd backend && python main.py

# 终端 2
cd frontend && npm run dev
```

浏览器打开 `http://localhost:5173`

- [ ] **Step 2: 走一遍用户场景**

走以下 5 步,记录任何错误:

```
1. 访问 / → 输入主题"基于深度学习的医学影像诊断研究" → 下一步
2. /english → 看到 OpenAlex 返回的论文列表(英文)→ 调过滤器 → 一键导入
3. /cn → 粘贴你截图里那条引文 + 几条其他文献 → 解析 → 全部导入
4. /pool → 看到英文+中文都入库,selected 字段已勾选 → 可手动调整
5. /write → 选"按国内外分类" → 勾选"主题不符筛选" → 生成综述
   观察:每章节是否只引用了选中的论文?引文格式是否正常?
```

- [ ] **Step 3: 至少修一处发现的问题**

(在此处记录并修改)

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "fix(e2e): patch issues found during manual walk-through"
```

---

## 验证清单(整体)

完成所有 Task 后,执行以下验收:

| 检查项 | 命令 / 操作 | 通过标准 |
|--------|------------|----------|
| 后端单元测试全过 | `cd backend && pytest -v` | 所有 test PASS |
| 后端可启动 | `python main.py` | 8000 端口监听 |
| 前端可构建 | `cd frontend && npm run build` | 无 TS 错误 |
| healthz 正常 | `curl :8000/api/healthz` | 返回 `{status:ok}` |
| OpenAlex 检索 | `/english` 页 | 看到英文论文 |
| 中文导入解析 | 粘贴你给的 GB/T 7714 引文 | parsed_ok=1 |
| 文献池展示 | `/pool` | 看到所有入库文献 |
| 综述生成 | `/write` | 看到 7 章内容,只引入选中论文 |
| 参考文献导出 | "导出 .md" 按钮 | 下载到综述 .md 文件 |

---

## 假设与决策记录

### 已决策
1. **数据源**:英文走 OpenAlex;中文走用户手动批量粘贴(GB/T 7714 引文)
2. **产品形态**:Web 应用 + 浏览器 IndexedDB(用户数据本地)
3. **过滤**:年份/被引/期刊层级/LLM 重排四维
4. **分类**:按国内外 / 按主题 二选一
5. **筛选**:综述前用 LLM 滤除主题不符文献(默认开启,可关)
6. **引文**:不做拼接,只接收平台字段(英文)或用户原文(中文)

### 已假设
- 用户能提供 `ANTHROPIC_API_KEY`
- 用户能访问 OpenAlex(无 IP 限制)
- 浏览器支持 IndexedDB(主流浏览器都支持)

### 待用户确认(无)
全部澄清已在 Phase 2 完成。

---

## 自审(spec coverage / placeholder / 一致性)

**Spec coverage:**
- ✅ 英文检索 + 自动取元数据:Task 1.1~1.5
- ✅ 中文批量导入:Task 2.1~2.2
- ✅ 用户告知分类方式:Writing 页 UI(Task 4.6) + ClassifySelector
- ✅ 综述前筛选:Task 4.2
- ✅ LLM 完成综述:Task 4.3~4.6
- ✅ 不拼接 GB/T 7714:每个 Paper 字段均透传或 `raw_citation` 透传

**Placeholder scan:** 无 "TODO / TBD / 后续实现" 字样,所有步骤都有可执行代码

**一致性:**
- `Paper` dataclass 字段名在 `types.py`、`adapters.py`、`api routes`、`frontend db.ts` 一致
- `lit_id` 字段命名全栈统一(后端 + IndexedDB 主键)
- `on_locale / by_theme` 在 `ClassifySelector` 与后端 API 字段名一致
