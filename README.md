# 文献综述 Agent

一个自动化的硕士论文文献综述写作助手：输入研究主题，自动从 OpenAlex/PubMed 等英文库 + 知网/万方/维普等中文库（通过远程浏览器）中检索文献、构建专业级布尔检索式、抽取候选文献元数据、生成符合学术规范的文献综述初稿（GB/T 7714-2015 格式）。

## 当前状态

**核心流程已通：**
- 主题 → 概念拆解 + 中英文同义词 + 布尔检索式（LLM 驱动，带启发式兜底）
- 英文库（OpenAlex）→ 异步任务 → 文献入池
- 中文库（知网/万方/维普）→ 远程无头浏览器 → 多行 AND 高级检索 → 抽取候选 → 入库
- 文献池 → 选题分组 → 综述生成（SSE 流式 + 人性化改写）

**仍在完善（见 TODO.md）：**
- 知网多行填表在反爬/版本变更下会偶尔失败，需要用户在画布里手动验证
- 自动翻页抽取的"下一页"选择器对新版知网 DOM 还在适配

## 架构

```
┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
│  Frontend (Vue) │────│  FastAPI (uvicorn)   │────│  SQLite/Postgres│
│  Element Plus   │    │  + Playwright headless│    │  + MiniMax LLM  │
└─────────────────┘    └──────────────────────┘    └─────────────────┘
        │                       │                          │
   Pinia + localStorage    HTTP REST + WS              SQLAlchemy
                          (browser frame stream)
```

- **后端**：`backend/src/`
  - `api/` — FastAPI 路由（pydantic 模型 + HTTP 端点）
  - `retrieval/` — 文献检索（OpenAlex 适配器、检索式规划、任务服务）
  - `automation/` — 远程浏览器（Playwright 包装 + 会话管理 + 填表/抽取）
  - `writing/` — 综述生成编排 + 章节写作
  - `llm/` — MiniMax 客户端
  - `prompts/` — 提示词模板（接 humanizer-zh / literature-review skills）
  - `db/` — SQLAlchemy models
- **前端**：`frontend-vue/`
  - `pages/` — Vue 页面：主题 / 统一检索 / 文献池 / 写作
  - `stores/` — Pinia（topic / papers / unifiedRetrieval 等）
  - `api/` — axios 客户端 + 端点常量 + 类型
  - `components/` — 复用组件

## 运行

### 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # 然后填 MINIMAX_API_KEY 等
python -m uvicorn main:app --host 127.0.0.1 --port 8080
```

### 前端

```bash
cd frontend-vue
npm install
npm run dev
# 浏览器打开 http://localhost:5173
```

## 主要工作流

1. **主题** → 输入研究主题
2. **统一检索** → 生成中英检索式 → 启动远程浏览器（默认进知网高级检索）→ 自动填 3-4 行"主题"行 → 自动点"检索" → 抽取候选 → 入库
3. **文献池** → 浏览 / 选题 / 删除 / 跨库
4. **写作** → 选章节类型 → 调 LLM → SSE 流式生成 → 自动应用 GB/T 7714 引用 + 人性化改写

## 已实现的关键能力

| 能力 | 位置 |
|------|------|
| 中英双语布尔式生成（带 3-4 概念 + 同义词词典兜底）| `retrieval/query_planner.py` |
| 远程浏览器多 URL 轮询（同一会话跨多个库）| `automation/remote_browser.py` `BrowserSession` |
| 知网高级检索多行 AND 拆填（按 `*` 拆 + JS 克隆行 + 赋值）| `automation/remote_browser.py` `fill_query_into_search_box` |
| 自动循环翻页抽取 + 跨库 fallback（验证/无下一页自动跳下一个库）| `automation/remote_browser.py` `auto_extract` / `multi_extract` + `api/automation.py` `/multi_extract` |
| OpenAlex 异步检索任务 + 状态轮询 | `retrieval/task_service.py` + `api/retrieval_tasks.py` |
| 综述 SSE 流式生成 + 章节拆分 | `writing/orchestrator.py` + `api/writing.py` |
| Pinia + localStorage 跨 tab 状态保留 | `stores/unifiedRetrieval.ts` |

## 未完成

详见 [TODO.md](TODO.md)。主要包括：
- 知网 DOM 经常变，多行填表需持续适配
- 远程浏览器连接复用/失效的边界 case
- OpenAlex 检索结果按相关性/引用量排序
- 综述的引文格式校验（GB/T 7714）

## 测试

```bash
cd backend
python -m pytest tests/ -q --ignore=tests/automation
```

51 个测试通过 + 1 个 skip（远程浏览器测试默认 skip，需要真浏览器环境）。

## 依赖

- Python 3.10+
- Node 18+
- MiniMax API key（在 `.env` 里 `MINIMAX_API_KEY=...`）
- Playwright Chromium（`playwright install chromium`）

## 许可

个人项目，未公开许可。
