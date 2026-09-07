# 知网爬虫产品级部署手册

> 适用版本：文献综述 Agent v0.2.0（产品级爬虫改造后）
> 本手册覆盖：环境要求 → 安装 → 配置 → 启动验证 → 7×24 守护 → 日常运维

---

## 1. 架构总览

```
┌─ frontend/ (Vue3 + TS + element-plus + Vite)
│    └─ CrawlerMonitorPage.vue  可视化监控面板（启停/状态/参数热调整）
│
┌─ backend/
│  ├─ main.py                   FastAPI 入口（挂载 /api/crawler/*）
│  ├─ src/api/crawler_admin.py  爬虫运维管理 API（11 个端点）
│  └─ src/automation/cnki/
│       ├─ crawler.py           爬取主流程（safe_request 统一入口）
│       ├─ cnki_adapter.py      业务适配层（质量评分+动态池+任务注册）
│       ├─ resilience.py        分层重试 + 断路器 + 告警中心
│       ├─ fingerprint.py       浏览器指纹随机化（UA↔sec-ch-ua 严格配对）
│       ├─ proxy_pool.py        代理 IP 池（巡检/评分/冷却/加权轮换）
│       ├─ quality.py           数据质量（完整性校验/缺失补全/评分）
│       ├─ scheduler.py         动态并发池（按负载自适应 1~N 线程）
│       ├─ monitor.py           任务注册表（状态/进度/环形日志/全链路计数）
│       └─ config.yaml          爬虫配置（优先级：参数 > 环境变量 > 文件 > 默认）
└─ backend/tests/crawler/       单元测试 + 集成测试 + 压力测试
```

## 2. 环境要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 使用 `str \| None`、内建泛型语法 |
| Node.js | 18+ | 前端构建（仅部署面板时需要） |
| 操作系统 | Windows 10+ / Linux | 7×24 需配合守护方式（见第 6 节） |
| 网络 | 可直连 `kns.cnki.net` | 知网为国内站，建议直连不走代理 |

## 3. 安装步骤

### 3.1 后端

```powershell
cd backend
pip install -r requirements.txt
```

- 若部署机 site-packages 无写权限（如 Anaconda 共享环境），citeproc-py 需装到项目内 `backend/lib/`（仓库已内置该目录，可跳过）。
- 复制 `.env.example` 为 `.env`，按第 4 节填写环境变量。

### 3.2 前端（可视化监控面板）

```powershell
cd frontend
npm install
npm run build        # 产物在 frontend/dist/，任意静态服务器可托管
```

开发调试用 `npm run dev`（Vite 开发服务器，默认 5173 端口）。

### 3.3 数据库

后端启动时 `db.session.init_db()` 自动建表，无需手动迁移。核心期刊清单首次启动自动从 xlsx 冷导入。

## 4. 配置说明

### 4.1 环境变量（.env 或系统环境）

| 变量 | 必填 | 说明 |
|------|------|------|
| `CJY_USER` / `CJY_PASS` / `CJY_SOFT_ID` | 是* | 超级鹰验证码平台账号。*不填则验证码识别不可用，极端风控场景爬通率下降 |
| `CNKI_SIGN_APP_ID` / `CNKI_SIGN_SECRET` | 否 | 知网签名参数覆盖（config.yaml 的 sign 段） |
| `CNKI_PROXY_MODE` | 否 | 代理模式，**优先级高于 config.yaml**：`off`（默认直连）/ `on`（全走代理池）/ `failover`（直连失败自动切代理） |
| `CNKI_PROXY_LIST` | 否 | 代理列表，逗号分隔，与 config.yaml 的 proxy.pool 自动去重合并。支持 `http://user:pass@host:port` |
| `CNKI_ALERT_WEBHOOK_URL` | 否 | 告警推送 webhook。配置后告警以 POST JSON 推送（title/level/detail/count/ts），未配置仅落内存缓冲（面板可查） |

### 4.2 config.yaml 关键段（backend/src/automation/cnki/config.yaml）

| 段 | 关键项 | 默认 | 说明 |
|----|--------|------|------|
| `http.fingerprint` | `enabled` | `true` | 会话级指纹随机化。每次进程启动从内置池挑一份（Chrome/Edge/Firefox 桌面端，UA↔sec-ch-ua 严格配对），会话中途不换 UA |
| | `pool` | `[]` | 追加自定义指纹，诊断某指纹被拉黑时下架/上架 |
| `resilience.breaker` | `failure_threshold` / `recovery_timeout` | 5 / 60.0 | 连续失败 5 次熔断拒绝发请求；60s 后放行单个探测请求 |
| `resilience.retry` | `transient_retries` / `rate_limited_retries` | 3 / 2 | 分层重试：TRANSIENT（网络波动/5xx）短退避 1/2/4s；RATE_LIMITED（429/503）长退避 15/45s 跨限流窗口；BLOCKED/FATAL 不重试。退避含 ±20% jitter |
| `resilience` | `alert_cooldown` | 300.0 | 同类告警节流窗口（秒），窗口内只推送一次、count 累加 |
| `proxy` | `mode` / `pool` / `check_interval` / `cooldown` / `score_threshold` | off/[]/300/600/0 | mode=on 时后台按 check_interval 巡检；成功+5分/失败-10分，连败3次冷却 cooldown 秒；跌破阈值剔除轮换 |
| `runtime` | `delay_seconds` | 2.0 | 基础请求间隔。**过低触发风控，生产不建议 <1.5** |
| | `min_workers` / `max_workers` | 1 / 6 | 动态池边界：站点顺畅提速、迎风（延迟高/失败率高/风控信号/CPU 高载）自动减速到 1 |

修改 config.yaml 后重启后端生效（或调用 `automation.cnki.crawler.init()` 热重载）。

## 5. 启动与验证

### 5.1 启动后端

```powershell
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
# 或
python main.py
```

生产模式去掉 `--reload`。交互式 API 文档：`http://<host>:8000/docs`。

### 5.2 验证清单

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | `GET http://<host>:8000/api/health` | 200 |
| 2 | `GET http://<host>:8000/api/crawler/dashboard` | 返回 tasks/pool/breakers/proxy/alerts 五段 JSON，pool 为 `{"initialized": false}`（任务未启动时正常） |
| 3 | 打开前端面板 → 爬虫监控页 | 空态渲染正常、无控制台报错 |
| 4 | 发起一次小规模检索任务 | 面板出现任务卡片、环形日志滚动、saved 计数增长 |

## 6. 7×24 守护

### 6.1 Windows（NSSM 推荐）

```powershell
nssm install LitReviewAPI "C:\Python310\python.exe" "-m uvicorn main:app --host 0.0.0.0 --port 8000"
nssm set LitReviewAPI AppDirectory D:\code\文献综述agent\backend
nssm set LitReviewAPI AppStdout D:\logs\api-out.log
nssm set LitReviewAPI AppStderr D:\logs\api-err.log
nssm set LitReviewAPI AppExit Default Restart
nssm start LitReviewAPI
```

### 6.2 Linux（systemd）

```ini
# /etc/systemd/system/litreview-api.service
[Unit]
Description=LitReview Agent API
After=network.target

[Service]
WorkingDirectory=/opt/litreview/backend
ExecStart=/opt/litreview/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/opt/litreview/backend/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now litreview-api
```

## 7. 日常运维

### 7.1 监控面板（推荐入口）

前端「爬虫监控」页提供：任务总览/启停/参数热调整、动态池实时并发、断路器状态、代理池健康分、告警流水。全部能力亦有对应 REST API（见 [API.md](API.md)）。

### 7.2 常见操作

| 场景 | 操作 |
|------|------|
| 某任务卡住 | 面板点「停止」（先置取消事件，业务协程在下一个检查点安全退出并标记 stopped） |
| 全站限流告警刷屏 | 面板查断路器：open=熔断中，爬虫自动长退避；确认恢复后 `POST /api/crawler/breakers/{name}/reset` 手动复位 |
| 代理被拉黑 | 池自动降分/冷却/剔除；长期不可用的从 `CNKI_PROXY_LIST` 或 config.yaml 移除后重启 |
| 峰值期临时提速 | `PUT /api/crawler/tasks/{id}/params` 热调 `max_workers`（≤16）/`delay_seconds`，无需重启 |
| 页面结构变更 | 解析层按字段定位多选择器兜底，优先只改 config.yaml 与解析适配层，勿动主流程 |

### 7.3 日志与排障

- 后端日志：uvicorn 控制台/stdout（已统一本地时间前缀）；NSSM/systemd 落盘路径见第 6 节。
- 全链路任务日志：面板任务详情或 `GET /api/crawler/tasks/{id}`（环形缓冲，覆盖请求发起→响应解析→数据落库）。
- 告警流水：`GET /api/crawler/alerts`；若配置 webhook 可直接对接钉钉/企业微信/Slack 网关。
