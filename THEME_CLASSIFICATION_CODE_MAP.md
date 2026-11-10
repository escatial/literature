# 主题划分相关代码总览

本文档列出当前项目中所有参与“主题划分”的代码入口、调用顺序和关键逻辑。完整实现仍以源文件为准。

## 1. 总调用链

```text
WritingPage.vue
  -> writingStore.plan()
  -> POST /api/writing/plan-stream
  -> api/writing.py: writing_plan_stream
  -> orchestrator.plan_review_stream
  -> _screen_papers
  -> classify(..., mode=theme)
  -> classifier.classify_by_theme
  -> readthrough.plan_readthrough_notes      # 2026-10-27 通读笔记(R1):
                                             #   确定性乱序(seed=hash(topic))
                                             #   → 分块(≤12篇/≤30k字符)
                                             #   → 逐块行协议笔记;
                                             #   失败降级原大文档路径
  -> readthrough.build_axis_catalog_with_notes  # R2 定轴输入 = 笔记目录+题录
  -> LLM JSON / 文本抢救 / 确定性兜底          # 产出经 _normalize_axes 单收口
  -> _finalize_theme_groups
  -> plan_complete(groups, readthrough_notes)  # 紧凑笔记随 payload 落库
  -> 用户编辑并确认主题
  -> writingStore.confirmGroups()
  -> POST /api/writing/generate-stream
  -> orchestrator.generate_review_stream(confirmed_groups=...)
  -> _sanitize_confirmed_groups
  -> 每节写作前 readthrough.section_notes_block  # R1' 先读后写(笔记进
                                                   # section prompt;失败
                                                   # 降级既有证据层)
  -> 按已确认主题写作
```

## 2. 后端主题分类核心

主文件：

- [backend/src/writing/classifier.py](backend/src/writing/classifier.py)

关键函数：

- `_group_name_acceptable`：主题名称合法性检查（第 111 行附近）
- `_clean_group_name`：主题名称清洗（第 166 行附近）
- `_derive_group_name`：从文献标题提取主题名（第 212 行附近）
- `_unique_evidence_group_name`：从簇内标题生成不重复名称（第 264 行附近）
- `_finalize_theme_groups`：覆盖率、重复归属、主题数量和语言校正（第 310 行附近）
- `_rebalance_language_groups`：中英文混合校正（第 420 行附近）
- `_salvage_text_groups`：抢救模型输出的自然语言主题（第 583 行附近）
- `_build_groups`：将模型编号映射为文献分组（第 651 行附近）
- `_deterministic_fallback_groups`：模型失败后的确定性兜底（第 742 行附近）
- `_semantic_cluster_groups`：500 篇以上文献的本地聚类（第 838 行附近）
- `_merge_chunk_groups`：大池子分块结果合并（第 1059 行附近）
- `classify_by_theme`：主题分类总入口（第 1148 行附近）
- `classify`：按主题/国内外模式分派（第 1356 行附近）

## 3. 当前截图对应的直接问题

截图中的四个名称不是在写作阶段产生的，而是在阶段一 `plan_review_stream` 的 `classify_by_theme` 结果中产生，随后被前端原样展示。

当前逻辑存在三个结构性问题：

1. `_deterministic_fallback_groups` 仍然以标题 bigram 相似度作为主要分簇依据，容易把“协同配送”“无人机协同配送”“联合配送路径”拆成高度重叠的簇。
2. `_unique_evidence_group_name` 主要从代表标题抽取短语，只保证“名称可读”，没有检查不同主题之间的研究问题边界是否互斥。
3. `_merge_chunk_groups` 的相似主题合并主要比较组名 `_title_similarity(extra.name, g.name)`，比较的是名称字符串，不是组内文献的研究问题证据，因此可能把名称相近的不同问题合并，或保留名称不同但实质重叠的主题。

## 4. 阶段一 API 与编排

- [backend/src/writing/orchestrator.py](backend/src/writing/orchestrator.py)
  - `plan_review_stream`：第 1025 行附近
  - `_select_theme_writing_pool`：第 383 行附近
  - `build_review_blueprint`：第 240 行附近
  - `_sanitize_confirmed_groups`：第 773 行附近
  - `generate_review_stream`：第 1131 行附近
  - `generate_review`：第 1366 行附近

- [backend/src/api/writing.py](backend/src/api/writing.py)
  - `writing_plan_stream`：主题规划 SSE 接口
  - `writing_generate_stream`：确认主题后的正文 SSE 接口
  - `GroupOut` / `confirmed_groups`：前后端主题分组协议

## 5. 前端主题确认与回传

- [frontend/src/pages/WritingPage.vue](frontend/src/pages/WritingPage.vue)
  - `startPlan`：触发阶段一主题划分
  - `editableGroups`：接收并编辑主题
  - `confirmThemes`：用户确认主题
  - 主题确认面板：约第 503 行附近

- [frontend/src/stores/writing.ts](frontend/src/stores/writing.ts)
  - `plan()`：调用 `/api/writing/plan-stream`
  - `confirmGroups()`：调用 `/api/writing/generate-stream` 并传 `confirmed_groups`

## 6. 相关提示词

- [backend/src/prompts/literature-review.md](backend/src/prompts/literature-review.md)
  - `classify`：主题分类提示词
  - `write_section`：按已确认主题写正文的提示词

## 7. 目前不应参与主题主轴的字段

以下字段只能作为文献属性或质量约束，不能形成一级主题：

- 中文/英文
- CNKI/OpenAlex/PubMed
- 期刊名称
- 作者名称
- 年份
- 单独的算法名称

主题应由同一研究问题、研究对象、关键约束或理论视角形成，并且主题之间必须经过“边界互斥性”检查。当前代码还缺少这一层真正的跨组边界检查，这正是截图中三个中文主题高度重叠的根本原因。
