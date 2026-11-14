# 文献综述 Agent

面向学术写作的一站式文献综述生成工具：中英文多源检索（CNKI / OpenAlex / PubMed）、
文献池管理、AI 主题划分、两阶段流式综述生成、全流程质量核查（QA）、GB/T 7714
参考文献渲染，以及可选的知网爬虫 7×24 监控面板。

- 后端：FastAPI + SQLAlchemy + SQLite（`backend/`，Python 3.11+）
- 前端：Vue 3 + TypeScript + Element Plus + Vite（`frontend/`）
- 测试：pytest（后端 846 例）/ vitest（前端）

---

## 1. 写作主链路（7 节点 + 交付保障层）

写作流程是一个 **7 节点** 的线性链路，节点间通过事件推进
（事件名契约单一事实来源：`frontend/src/api/sseTypes.ts`）。
自 J1 任务化改造起，事件**逐条落库**（`writing_job_events` 表），前端通过
**可续传事件流**（`GET /writing/jobs/{id}/stream`）重放+实时接入 —— 连接只是
「读取视图」，断线/刷新页面凭 job_id 全量重放恢复界面，不再有「一次连接扛
60 分钟」的脆弱性：

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

### 1.1.1 任务化改造（J1，2026-11）

长 SSE 连接的固有脆弱性（代理掐连接、网络抖动、机器休眠）由**任务化**根治：

- **任务模型**：`writing_jobs`（状态机 running → awaiting_confirm → running →
  done/failed/stopped）+ `writing_job_events`（append-only 事件日志，seq 连续）；
- **API**：`POST /writing/jobs`（启动，立即返回 job_id）、
  `POST /writing/jobs/{id}/confirm`（阶段2确认，同一 job 内 seq 连续续写）、
  `GET /writing/jobs/{id}`（快照）、
  `GET /writing/jobs/{id}/stream?since_seq=N`（重放+实时 tail，终态即收尾）、
  `POST /writing/jobs/{id}/stop`（进程内 Event 即停 + 落库）；
- **前端**：`consumeWritingJobStream` 退避重连（非终态断开按 since_seq 续传），
  WritingPage 挂载时 `resumeActive()` 经 sessionStorage 恢复活跃任务界面；
- **重启语义**：启动时把仍处 running 的任务标记 failed（规划快照保留，
  确认主题后可直接重试阶段2）；awaiting_confirm 是合法持久状态不受影响；
- **保留策略**：仅最近 100 个任务及其事件；
- LLM 正在生成中的章节不可跨进程续写（模型调用本身不可恢复）——这是任务化
  唯一无法覆盖的场景，服务重启后该任务按 failed 处理。

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

## 2. 速度与质量引擎（2026-11-14 提速+质量重构）

量化基线(job 8e25ffd9 取证,90 篇全流程 124 分钟):首过写作仅 2-4 分钟,
其余为质量失败连锁(整章重写 545s×2 全失败 / 固定 3 轮润色 9.5 分钟 / QA
修复 4.4 分钟)与固定开销(筛选 17.7 分钟)。四项根因工程:

- **LLM 分级路由**:`users` 表新增快模型四元组(fast_kind/base_url/key/model,
  监控页「LLM 设置」配置),`messages_create/stream_ex(tier="fast")` 让筛选/
  大纲/核查类高频短调用走快档;未配置或失败自动回落主档。筛选并发 6→10
  (上限 16),筛选摘要 2000→600 字符。
- **段落级大纲→成文**(写作重构核心):每章先产段落级论证大纲
  (论点句 + 支撑文献 + relation:共识/分歧/演进/缺口)→ 逐段并行成文(≤4
  线程)→ 段级验收(缺引用单段重写 ~30s)。废除"按池规模要求引用数"的
  整章重写(引用目标改为大纲分配的文献集);大纲不可用自动回退旧单稿路径。
- **条件润色**:article_lint 先行,通过则跳过 humanize(旧固定 3 轮是每章
  2-4 分钟固定开销);不达标补 1 轮。
- **主题均衡收紧**:大组拆分上限 50%→25(`WRITING_THEME_MAX_PAPERS`),
  治「33 篇巨型组→引用密度被推高→塞引用」;确认面板每组显示归组依据
  (研究问题 + 3 篇代表文献)。
- **综合并行化**:逐主题线程池(旧串行 = 240s 硬时钟 × N);模型自写的
  `[...]` 引用占位符在 finalize 咽喉点统一剥离,交付门禁计数复核。

预期(90 篇基准):总时长 124 → ≤30 分钟;QA 通过率 ≥0.9;首过章 ≥3/4。

## 2b. 并发与停止语义

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
keywords`（D1），并自动建 `writing_jobs / writing_job_events` 表（J1）。
存量 reviews 行这些字段为空属预期；导出时历史行的残留锚点
会被导出门禁自动剥离。旧 SSE 端点（`/writing/generate-stream`、
`/writing/plan-stream`、`/writing/stop`）已随 J1 下线，前端已全部切换到
`/writing/jobs` 体系。

### 5.3 关键环境变量（`.env` 或系统环境）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ALLOWED_ORIGINS` | `*` | CORS 允许来源，逗号分隔。生产建议收紧为前端 origin |
| `AUTH_TOKEN_TTL_SECONDS` | `604800` | 登录令牌 TTL（7 天），剩余寿命过半自动滑动续期 |
| `WRITING_ABSTRACT` | `1` | 摘要/关键词节点开关（D1）。`0` 关闭摘要生成 |
| `WRITING_THEME_MIN_PAPERS` | `15` | 单主题最低文献数（J1 用户规则）：定轴轴数上限 = 池子规模 ÷ 此值；归组后不足额主题确定性并入最相似主题（合并不跌破最少组数，小池交确认面板人工裁决） |
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
8. 前端 e2e（Playwright）尚未建立（写作流断线已由 J1 任务化 + 重放式重连解决）。

## 8. 相关文档

- 知网爬虫部署手册：`docs/crawler/DEPLOYMENT.md`
- 爬虫 API：`docs/crawler/API.md`
- 主题分类代码地图：`THEME_CLASSIFICATION_CODE_MAP.md`
