# 文献综述 Agent

面向学术写作的一站式文献综述生成工具：中英文多源检索（CNKI / OpenAlex / PubMed）、
文献池管理、AI 主题划分、两阶段流式综述生成、全流程质量核查（QA）、GB/T 7714
参考文献渲染，以及可选的知网爬虫 7×24 监控面板。

- 后端：FastAPI + SQLAlchemy + SQLite（`backend/`，Python 3.11+）
- 前端：Vue 3 + TypeScript + Element Plus + Vite（`frontend/`）
- 测试：pytest（后端 846 例）/ vitest（前端）

---

## 1. 写作主链路（7 节点 + 交付保障层）

写作流程是一个 **7 节点** 的线性链路，节点间通过 SSE 事件推进
（事件名契约单一事实来源：`frontend/src/api/sseTypes.ts`）：

```
planning(start)                      开始：接收文献、准备筛选
  → screening(screening_*)           相关性筛选（分批进度）
  → classify(classify_*)             主题划分：先通读全文做笔记
                                     （readthrough_*：逐块行协议笔记），
                                     再以笔记目录定轴（AI 质检 + 自动修复闭环）
  → await_confirm(plan_complete)     阶段1终点：等用户确认主题（两阶段模式）
  → writing(writing_started/section_*)  阶段2：逐章先读后写
                                     （section_notes_done：节级通读笔记）
                                     ，再流式生成正文
  → qa(qa_*)                         全流程质量核查（软门禁：FAIL 只告警不终止）
  → complete(complete)               交付：参考文献列表 + 摘要/关键词已生成
旁路：stopped(idle)                   用户主动停止
```

### 1.1 交付保障层（D1，2026-11 交付事故后建立）

> 背景：2026-11-01 交付的综述存在 41 处内部引用锚点泄漏、章节乱序（一、三、二、五）、
> 整章缺失、QA 报告从未送达四类缺陷，根因是「客户端拼装交付物」——定稿只短暂存在于
> 服务端内存，前端用收到的零散事件拼装并回传入库。交付保障层把信任模型倒转回服务端：

| 机制 | 实现 | 防住的缺陷 |
|------|------|-----------|
| SSE 事件序号 | `api/writing.py` 为每帧注入流内单调 `seq`，前端跳号告警 | 大帧静默丢失无感知 |
| 交付清单 manifest | `complete` 事件携带章节清单/缺章/锚点泄漏/参考文献计数 | 丢帧后残品被当成功 |
| 完成性门禁（前端） | `generateWritingStream` 校验 `complete` + manifest，未过一律抛错 | 流中断 → 残品静默入库 |
| 服务端权威落库 | 编号+QA 修复+摘要齐备的定稿由后端直接写 `reviews`（幂等 upsert） | 交付物依赖前端拼装 |
| 保存调和（后端） | `POST /reviews` 同 `(task_id, owner)` 调和：完整干净副本才可覆盖服务端定稿；正文残留 `[lit_xxx]` 直接 422 | 前端残品覆盖定稿 |
| 导出门禁 | `render_checker.check_sections_delivery` 五项硬校验 + 历史行锚点剥离 + 渲染后 `check_render` | 带病文件导出给用户 |
| QA 修复重发 | QA 自动修复后重新编号并重发 `sections_finalized`（`final:true`） | 修复结果从未送达用户 |
| 定稿序号重排 | `_renumber_section_titles` 按交付位置连续编号 | 用户删组/重排后乱序 |
| 摘要/关键词节点 | 写作完成后 LLM 蒸馏 200-300 字摘要 + 4-6 关键词（`abstract_done` 事件 + docx 渲染 + 落库） | 综述缺摘要的体裁缺失 |
| run 级事件日志 | 每次写作流落盘 `backend/.runs/writing_*.jsonl`（保留 30 份） | 事故无法复盘 |

### 1.2 断点恢复（两阶段模式）

阶段1 跑到 `await_confirm` 即停，规划快照（主题分组 / 章节标题 / 筛选后文献 /
研究问题）经 `writing/plan_store.py` UPSERT 落库（键：`task_id + user_id`）。
恢复用法：

1. 前端为每次写作会话持有一个 `X-Task-Id`（http 客户端全请求自动注入）；
2. 阶段2 请求 `POST /writing/generate-stream` 时**不带 papers**、只带
   `confirmed_groups` + 同一 `X-Task-Id`，后端 `_resolve_stage2_papers`
   按规划快照的 `screened_lit_ids` 从 papers 表回表取全字段；
3. 也可用 `GET /writing/plan`（带 `X-Task-Id`）读回快照；非 admin 只能取
   自己名下的规划，越权按 404。

---

## 2. 并发与停止语义

- **多写作流并发**：LLM 用户配置经 ContextVar 在 worker 线程内绑定/复位
  （`_sse_stream_response` 的 `bind`），同一用户可并行多条写作流。
- **写作停止**：`POST /writing/stop` 按注册表键 `(user_id, task_id)` 隔离 ——
  普通用户只停自己名下的流；admin 全量；返回 `{"stopped": n}`。
- **检索停止**：`POST /retrieval/stop` 以 DB 行 `owner_user_id` 为权威，语义对齐。
- **数据归属**：papers / retrieval_tasks / reviews 等业务表带 `owner_user_id`
  （安全域），`X-Task-Id`（业务域）与之双层正交。

## 3. 快速启动

```bash
# 后端(Python 3.11+)
cd backend
pip install -r requirements.txt
copy .env.example .env      # 填 LLM key
python -m uvicorn main:app --reload

# 前端
cd frontend
npm install
npm run dev                 # Vite 开发服务器(127.0.0.1:5174,自动托管后端进程)
```

## 4. 测试

```bash
# 后端全量(约 140s,846 例;crawler/stress 含在内)
cd backend && python -m pytest tests -q

# 只跑写作与交付门禁(快速反馈)
cd backend && python -m pytest tests/writing tests/delivery tests/auth -q

# 前端类型 + 单测
cd frontend && npx vue-tsc --noEmit && npx vitest run
```

交付链回归测试锚点：`tests/delivery/test_delivery_gate.py`（锚点剥除/序号重排/
manifest/seq）与 `tests/auth/test_review_delivery_gate.py`（POST 门禁/副本调和）。

## 5. 部署要点

### 5.1 升级顺序：后端先上

新版后端对旧版前端向后兼容（旧前端保存的干净副本仍可入库）；反过来
（新前端 + 旧后端）会在阶段2因缺 manifest 校验失败——新前端的完成性门禁
会把「无 manifest 的完成事件」判为异常并拒绝保存。因此升级时：先重启后端，
再构建/发布前端。

### 5.2 存量库迁移

后端启动 `init_db()` 幂等补列：reviews 表新增 `task_id / qa_report / abstract /
keywords`（D1）。存量 reviews 行这些字段为空属预期；导出时历史行的残留锚点
会被导出门禁自动剥离。

### 5.3 关键环境变量（`.env` 或系统环境）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ALLOWED_ORIGINS` | `*` | CORS 允许来源，逗号分隔。生产建议收紧为前端 origin |
| `AUTH_TOKEN_TTL_SECONDS` | `604800` | 登录令牌 TTL（7 天），剩余寿命过半自动滑动续期 |
| `WRITING_ABSTRACT` | `1` | 摘要/关键词节点开关（D1）。`0` 关闭摘要生成 |
| `WRITING_READING_NOTES` | `1` | 通读笔记总开关（先通读→笔记→定轴；写作先读后写）。`0` 回到旧链路 |
| `READTHROUGH_CHUNK_PAPERS` | `12` | 通读分块每块最多篇数 |
| `READTHROUGH_CHUNK_CHARS` | `30000` | 通读分块每块最多字符数 |
| `PLAN_NOTES_CHARS` | `120000` | 定轴目录（笔记+题录）总字符预算 |
| `INIT_DB_PURGE` | 不启用 | 启动期脏数据清理显式开关（默认跳过；设 `1` 启用，先备份 SQLite） |

完整变量见 `backend/.env.example`。

## 6. 内容质量约束（写作 prompt 层）

- 每段 ≥250 字，完成「共识——分歧——证据局限」三元推进，相邻段落禁同构句式开头；
- 文献清单按**核心期刊优先 + 相关性分级**排序，并附预格式化夹注
  （英文只取第一作者姓氏 + et al.，无作者文献用短题名，修复「通威鱼(2013)」类误标）；
- 每段至少并置 3 篇不同文献，密度不足自动进入 LLM 修复闭环。

## 7. 已知遗留项（技术债归档）

以下项经评估暂缓，按需排期：

1. `writing/orchestrator.py` 体量过大，待按 筛选/分类/写作/QA 拆分模块；
2. 迁移仍用 `init_db` 内联 DDL 列表（`_MIGRATIONS`），未引入 Alembic；
3. 检索 `events` 仍整段 JSON 存 `retrieval_tasks.events`，量大后待拆表；
4. **双导入树**（`backend` 与 `backend.src` 并存）：`llm.client` 与 `src.llm.client`
   是两个模块实例、ContextVar 互不可见，目前靠导入纪律防回归，待统一单根 + 守护测试；
5. 知网爬虫仍在 API 进程内跑，待拆独立服务 + 队列；
6. 前后端类型仍手工镜像（D1 已同步 qa_report/abstract/keywords/pubmed source，
   治本待 OpenAPI codegen）；
7. LLM SSRF 防护未在 transport 层 pin 解析后 IP（DNS rebinding 窗口仍在）；
8. 前端 e2e（Playwright）尚未建立；写作 SSE 断线后尚无自动续传（当前为显式失败
   + 重试，不静默保存残品）。

## 8. 相关文档

- 知网爬虫部署手册：`docs/crawler/DEPLOYMENT.md`
- 爬虫 API：`docs/crawler/API.md`
- 主题分类代码地图：`THEME_CLASSIFICATION_CODE_MAP.md`
