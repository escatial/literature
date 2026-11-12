# 事故回归语料库(Incident Corpus)

商业化质量体系的一部分:**每一次生产故障都必须留下三样东西**——
量化证据、根因、守护测试。本目录索引全部已固化的事故及其守护测试,
任何改动后全量跑一遍,任何一次旧事故复发都会在这里被拦住。

新事故处理流程:取证(事件表/trace/日志)→ 根因 → 修复 → **把故障数据
固化为测试** → 在本表登记一行。

| # | 日期 | 事故 | 根因 | 守护测试 |
|---|------|------|------|---------|
| 1 | 11-01 | 交付综述 41 处 `[lit_xxx]` 锚点泄漏 + 缺章 + 乱序 + QA 报告丢失 | 客户端拼装交付物;`replace_anchors_with_numbers` 未知锚点原样保留 | `tests/delivery/test_delivery_gate.py` |
| 2 | 11-10 | 归组三连败(覆盖 2/10/4)→ 自主分组降级产出「智能超短链」 | 90 篇摘要目录下模型正文通道元推理;无蒸馏目录 | `tests/writing/test_custom_theme_axes.py` + assign trace |
| 3 | 11-10 | 用户 LLM 配置从未生效(烧系统 key) | 双导入树:`llm.client` 与 `src.llm.client` 双实例 ContextVar 分裂 | `tests/conftest.py` 单实例别名(结构性守护) |
| 4 | 11-10 | 恢复重放 23 分钟循环,确认面板永远不到 | 重放端点每批 200 条后即判终态,510 事件任务永远截断 | 修复验证脚本(510 事件 seq 连续断言) |
| 5 | 11-11 | 阶段2「任务文献池为空」 | 论文池挂会话任务 ID,确认端点用 job uuid 读池 | `tests/pool/`(同属主回退) |
| 6 | 11-11 | 主题综合 15 分钟挂死 | httpx 读超时被网关滴漏字节挫败 | 综合硬时钟 240s(守护线程)+ 超时降级 |
| 7 | 11-11 | 定轴产出垃圾轴清单(近重复/截断名/城市名轴)→ 归组再崩 | 散文抢救链无质量闸门 | `tests/writing/test_axes_quality_gate.py` |
| 8 | 11-11 | 阶段2失败恢复后确认面板 0 主题 | failed 分支只恢复状态没恢复分组 | `tests/auth/test_writing_jobs_api.py` |
| 9 | 11-11 | 旧 awaiting_confirm 滞留一天被反复端给用户 | 确认窗口无 TTL | `tests/writing/test_confirm_ttl.py` |
| 10 | 11-12 | 评审42第3章 41 处英文自我分析混入正文 + 网关缓冲单块 45766 字符重复流 + 22 处 `[...]` 悬空占位 | ① 非流式重写/润色返回英文元推理与正文交错,normalize 词表漏检新词形(We need/Let's/Polish:) ② 流式首稿无异常门,废料直接进夹注回填与重写链 ③ 润色轮输出草稿过程但引用锚点全保留,旧校验放行 | `tests/writing/test_meta_reasoning_incident.py`(四层:normalize 交错剥离 / 章节非正文判据扩容 / 流式异常门+整段重试 / humanizer 草稿倾倒回滚) |

## 发布前门禁

```bash
# 1. 全量单测(含全部事故守护测试)
cd backend && python -m pytest tests -q

# 2. E2E 全流程冒烟(真实 LLM,运行中后端)
set E2E_USERNAME=... & set E2E_PASSWORD=...
python backend/scripts/e2e_smoke.py --pool-size 12
```

两者全绿才可发布。

## 质量单(运行可观测性)

任意一次客户运行的结构化质量数据(阶段耗时/筛选漏斗/通读产出/归组
尝试与覆盖率/闸门触发/章节清单/QA 结论/交付清单/错误)可离线合成:

```bash
python backend/scripts/quality_report.py --latest
```

新事故的三问:证据在哪(事件表+trace)?根因是什么(架构层)?守护测试
在哪(本表登记)?
