# 爬虫运维管理 API 文档

> 路由前缀：`/api/crawler`（注册于 `backend/src/api/crawler_admin.py`）
> 交互式文档：后端运行时访问 `/docs`（Swagger UI）
> 数据来源：automation/cnki 各子系统进程内单例，与爬虫运行实例严格同源（无双单例分裂）

## 目录

- [GET /dashboard — 总览聚合](#get-dashboard)
- [GET /tasks — 任务列表](#get-tasks)
- [GET /tasks/{task_id} — 任务详情](#get-taskstask_id)
- [POST /tasks/{task_id}/stop — 停止单任务](#post-taskstask_idstop)
- [POST /tasks/stop-all — 停止全部](#post-tasksstop-all)
- [PUT /tasks/{task_id}/params — 参数热调整](#put-taskstask_idparams)
- [GET /alerts — 告警中心](#get-alerts)
- [GET /proxies — 代理池快照](#get-proxies)
- [GET /breakers — 断路器状态](#get-breakers)
- [POST /breakers/{name}/reset — 复位断路器](#post-breakersnamereset)
- [GET /pool — 动态并发池状态](#get-pool)

---

## GET /dashboard

面板首屏一次拉全：任务概览 + 动态池 + 断路器 + 代理池 + 最近告警。

**响应 200：**

```json
{
  "tasks": {
    "total": 3, "running": 1, "done": 1, "failed": 0, "stopped": 1,
    "saved_total": 47,
    "recent": [ { "…任务快照，同 /tasks 元素结构，最多 10 条…": "" } ]
  },
  "pool": { "initialized": true, "workers": 4, "…同 /pool…": 0 },
  "breakers": [ { "…同 /breakers 元素…": "" } ],
  "proxy": { "mode": "off", "proxies": [] },
  "alerts": [ { "…同 /alerts 元素…": "" } ]
}
```

> 未初始化的池返回 `{"initialized": false}` 壳，前端无需判空。

## GET /tasks

任务列表，新→旧排序，可按状态过滤。

| Query 参数 | 类型 | 说明 |
|-----------|------|------|
| `status` | str，可选 | `queued` / `running` / `done` / `failed` / `stopped`，精确匹配 |

**响应 200：** 任务快照数组

```json
[
  {
    "task_id": "t-20260902-001",
    "query": "深度学习 综述",
    "status": "running",
    "stage": "解析详情页",
    "saved": 12, "skipped": 1, "failed": 0,
    "started_at": 1788390000.0, "ended_at": 0.0,
    "params": { "delay_seconds": 2.0, "max_workers": 4 },
    "error": "",
    "counters": {
      "requests_total": 35, "requests_failed": 1,
      "parse_errors": 0, "saved_total": 12,
      "captcha_hits": 1, "breaker_trips": 0
    },
    "quality": { "score": 92.5, "flags": {}, "filled": 2, "normalized": 15, "ts": 1788390100.0 },
    "elapsed": 100.0
  }
]
```

| 字段 | 说明 |
|------|------|
| `status` 生命周期 | `queued → running → done/failed/stopped`；stop 端点仅置取消事件，状态由业务协程安全退出时翻转为 `stopped` |
| `counters` | 全链路计数：请求发起/失败、解析错误、落库数、验证码命中、断路器熔断次数 |
| `quality` | 数据质量评分（0~100）、异常标记分布、补全条数、标准化条数 |

## GET /tasks/{task_id}

单任务全量快照。

| Query 参数 | 类型 | 默认 | 说明 |
|-----------|------|------|------|
| `logs` | bool | `true` | 是否附环形日志（新→旧，含时间戳前缀） |

**响应 200：** 同 `/tasks` 元素 + `logs: ["[12:00:01] 发起检索 …", …]`
**404：** `{"detail": "任务不存在: <task_id>"}`

## POST /tasks/{task_id}/stop

停止单个任务：置取消事件（与业务侧 stop_event 桥接），幂等。

**响应 200：** `{"task_id": "t-001", "stopped": true}`
**404：** 任务不存在

> 语义：本端点不直接翻转 `status`——业务协程在下一个安全检查点感知取消事件后收尾并标记 `stopped`，避免脏中断导致半成品数据。

## POST /tasks/stop-all

停止全部运行中任务（queued/running），done 不受影响。运维兜底动作。

**响应 200：** `{"stopped": 2}`

## PUT /tasks/{task_id}/params

运行中参数热调整，白名单校验，钩子即时生效（如 `delay_seconds` 直达爬虫限速器），无需重启。

**请求体（全部可选，至少一项）：**

```json
{
  "delay_seconds": 3.0,
  "max_workers": 8,
  "page_size": 30,
  "max_per_keyword": 50
}
```

| 字段 | 约束 |
|------|------|
| `delay_seconds` | float，> 0 |
| `max_workers` | int，1 ~ 16 |
| `page_size` | int，10 ~ 50 |
| `max_per_keyword` | int，≥ 1 |

**响应 200：** `{"task_id": "t-001", "params": { …合并后的生效参数… }}`
**422：** 空补丁 / 约束越界 / 白名单外字段
**404：** 任务不存在

## GET /alerts

告警中心环形缓冲（新→旧，最多 200 条）。同类告警在 `alert_cooldown`（默认 300s）窗口内节流合并：只推送一次 webhook，`count` 持续累加。

**响应 200：**

```json
{
  "alerts": [
    { "title": "断路器熔断: http", "level": "critical", "detail": "连续失败 5 次",
      "ts": 1788390100.0, "count": 3 }
  ]
}
```

> 推送通道：配置环境变量 `CNKI_ALERT_WEBHOOK_URL` 后，告警以 POST JSON `{title, level, detail, count, ts}` 推送；webhook 失败静默（告警是旁路，不反噬爬取）。

## GET /proxies

代理池快照。URL 凭据打码（`http://***@host:port`），面板可安全展示。

**响应 200：**

```json
{
  "mode": "on",
  "proxies": [
    {
      "url": "http://***@1.2.3.4:8080",
      "score": 85.0, "alive": true, "available": true,
      "success_count": 7, "fail_count": 0,
      "last_latency": 0.21, "cooldown_left": 0.0, "last_check_ts": 1788390090.0
    }
  ]
}
```

> `mode` 为 `off` 时 `proxies` 为空数组（无池实例）。巡检评分：成功 +5（封顶 100）/ 失败 -10，连败 3 次冷却 600s，跌破 `score_threshold` 剔除轮换。

## GET /breakers

断路器状态表。

**响应 200：**

```json
{
  "breakers": [
    { "name": "http", "state": "open", "consecutive_failures": 5, "cooldown_left": 42.0 }
  ]
}
```

| state | 含义 |
|-------|------|
| `closed` | 正常放行 |
| `open` | 熔断中：拒绝发请求，防止无效流量堆积；冷却期满转半开 |
| `half_open` | 放行单个探测请求：成功→closed，失败→重新 open |

## POST /breakers/{name}/reset

手动复位断路器（open 状态卡死时的运维动作）。

**响应 200：** `{"name": "http", "reset": true}`

## GET /pool

动态并发池状态。并发度按实时负载自适应：响应延迟高/失败率高/风控信号/本机 CPU 高载 → 自动收缩；窗口健康 → 逐轮扩张 +1。

**响应 200：**

```json
{
  "initialized": true,
  "workers": 4, "min_workers": 1, "max_workers": 6,
  "avg_latency": 1.83, "fail_rate": 0.05,
  "risk_pending": 0, "cpu_load": 32.0, "psutil_available": true
}
```

---

## 通用约定

- 成功一律 200/201，无包裹层：响应体即数据。
- 错误格式：FastAPI 标准 `{"detail": "…"}`（404/422 等）。
- 所有端点与爬虫运行实例共享同一模块单例——面板数据即运行时真相，无轮询延迟与双源不一致问题。
