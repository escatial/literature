# 提示词模板管理模块使用说明

本模块把两个外部 Claude Skill(`humanizer-zh`、`literature-review`)的规则抽象为标准化提示词模板,在项目内通过 MiniMax API 调用,无需 Claude Agent 工具链。

## 1. 模块结构

```
backend/src/prompts/
├── __init__.py              # PromptTemplate 类 + 加载器
├── service.py               # render / call_template 业务封装
└── humanizer-zh.md          # 文本润色模板
    literature-review-section.md      # 章节写作模板
    literature-review-classify.md     # 主题/国内外分类模板
```

每个模板文件分两部分:
- 顶部 ` ```meta ` YAML 头(描述、版本、必填/可选参数、输出 schema)
- 下方 Markdown body(系统提示词正文,支持 `{{var}}` 占位符)

## 2. API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/prompts` | 列出所有可用模板(id + 描述) |
| GET | `/api/prompts/{id}` | 查看单个模板的参数和首屏预览 |
| POST | `/api/prompts/render` | 仅渲染模板(返回拼好参数的 system prompt 字符串,不调 LLM) |
| POST | `/api/prompts/call` | 渲染 + 调 LLM,返回助手消息文本 |
| POST | `/api/prompts/humanize` | 便捷入口,直接调 humanizer-zh 模板 |

## 3. 三个内置模板

### 3.1 `humanizer-zh`

来自 humanizer-zh skill 的核心 24 条 AI 写作规则与改写策略。

**调用示例**:
```bash
curl -X POST http://localhost:8080/api/prompts/humanize \
  -H "Content-Type: application/json" \
  -d '{"text": "此外,人工智能作为革命性的技术,标志着...", "score_mode": true}'
```

**入参**:
| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | ✓ | 待润色文本 |
| `score_mode` | | 是否给质量评分,默认 true |

**返回**:
```json
{
  "rewritten": "改写后的文本",
  "changes": ["删除『此外』", "删除『标志着』", "..."],
  "score": {"direct": 8, "rhythm": 7, "trust": 9, "authenticity": 8, "conciseness": 7, "total": 39},
  "raw": "LLM 原始输出"
}
```

### 3.2 `literature-review-section`

单篇综述章节写作。把 literature-review skill 的全部硬性约束写入 system prompt:
- 强制"作者(年份)"夹注 + 内联 `[lit_xxx]` 引用锚点
- 批判性写作(禁止简单罗列、必须比较与对比)
- 每篇文献在同一章只能出现一次
- "comment" 章节不引用任何文献(文献述评强制规则)
- 嵌入 humanizer-zh 的去 AI 痕迹要点(高优先级)

**入参**:
| 字段 | 必填 | 说明 |
|---|---|---|
| `topic` | ✓ | 研究主题 |
| `section_key` | ✓ | 章节 key,如 `introduction`/`themes`/`comment` |
| `section_title` | ✓ | 章节标题,如 "二、国外研究现状" |
| `section_role` | ✓ | 章节定位说明,自动按 `comment` 切换为不引用模式 |
| `papers_catalog` | ✓ | 文献清单(多行,每行 `- lit_xxx | 标题 | 作者 | 期刊 | 年份`) |
| `available_lit_ids` | ✓ | 可用 lit_id 列表(每行一个) |
| `humanize` | | 是否在 prompt 中嵌入去 AI 痕迹规则,默认 true |

**调用示例**:
```python
import requests
resp = requests.post("http://localhost:8080/api/prompts/call", json={
    "template_id": "literature-review-section",
    "vars": {
        "topic": "水产品营销策略",
        "section_key": "foreign_a",
        "section_title": "二、国外研究现状—营销策略",
        "section_role": "国外子主题 1",
        "papers_catalog": "- lit_aaa | Aquatic Marketing | Smith, Lee | J. Marketing | 2024\n...",
        "available_lit_ids": "lit_aaa\nlit_bbb\n...",
        "humanize": True,
    },
    "max_tokens": 4000,
})
print(resp.json()["output"])
```

### 3.3 `literature-review-classify`

主题分类(或 locale 分类辅助)模板。把 literature-review Step 3 的硬性规则写入 prompt:
- locale 模式:直接根据 `source` 字段(openalex/user_imported)分国内外
- theme 模式:3~5 个并列主题、每主题 ≥2 篇、无兜底"其他"

**入参**:
| 字段 | 必填 | 说明 |
|---|---|---|
| `topic` | ✓ | 研究主题 |
| `classify_mode` | ✓ | `locale` 或 `theme` |
| `papers_catalog` | ✓ | 文献条目,每行 `- lit_xxx | 标题 | 期刊 | 年份 | source=...` |

**返回**(LLM 应输出 JSON):
```json
{
  "groups": [
    {"name": "主题 A", "lit_ids": ["lit_aaa", "lit_bbb"]},
    {"name": "主题 B", "lit_ids": ["lit_ccc"]}
  ]
}
```

## 4. 程序内调用

```python
from prompts.service import render, call_template

# 仅渲染
sys_prompt = render("humanizer-zh", text="...", score_mode=True)

# 渲染 + 调 LLM
out = call_template(
    "literature-review-section",
    vars={"topic": "...", "section_key": "themes", "section_title": "...",
          "section_role": "...", "papers_catalog": "...", "available_lit_ids": "...",
          "humanize": True},
    max_tokens=4000,
)
```

## 5. 添加新模板

1. 在 `backend/src/prompts/` 下新建 `my-template.md`
2. 顶部加 ` ```meta ` 块(描述、版本、必填参数、输出 schema)
3. Body 中用 `{{param}}` 占位符引用必填参数
4. 模板自动被 `list_templates()` 发现,无需注册

## 6. 集成点

- `writing/section_writer.py`:写章节时使用 `literature-review-section` 模板
- `writing/classifier.py`:主题分类时使用 `literature-review-classify` 模板
- 前端可通过 `GET /api/prompts` 列出并在前端展示

## 7. 与原 Skill 的差异

| 维度 | 原 Claude Skill | 本项目模板 |
|---|---|---|
| 调用方式 | Claude Agent 工具(Read/Edit/Grep) | 直接作为 MiniMax system prompt |
| 触发条件 | 用户消息触发 | API 端点显式调用 |
| 文件产出 | 编辑用户提供的 .md/.docx | 在内存里渲染,不落盘原始文件 |
| Word 脚注/OpenXML | 原 Skill 强约束 | 本项目输出 Markdown,Word 脚注由前端/后处理做 |
| 检索源 | Google Scholar + 知网(强约束) | OpenAlex + 用户粘贴的知网引文(实际可达性不同) |

核心规则 100% 保留:**每篇只引一次**、**文献述评不引用**、**作者(年份)夹注 + 内联引用**、**批判性写作**、**去 AI 痕迹**。

## 8. 测试

```bash
$env:PYTHONPATH='backend'; pytest backend/tests/prompts -v
backend/tests/prompts/test_templates.py::test_list_templates              PASSED
backend/tests/prompts/test_templates.py::test_humanizer_loads            PASSED
backend/tests/prompts/test_templates.py::test_humanizer_render_basic      PASSED
backend/tests/prompts/test_templates.py::test_humanizer_render_missing_required  PASSED
backend/tests/prompts/test_templates.py::test_litrev_section_renders     PASSED
backend/tests/prompts/test_templates.py::test_litrev_classify_renders    PASSED
backend/tests/prompts/test_templates.py::test_cache_loads_once           PASSED
8 passed
```