/** 爬虫运维管理 API 客户端(对应后端 /api/crawler/*,见 backend/src/api/crawler_admin.py)。*/
import { http } from './http';

// ─── 类型(与后端各子系统 snapshot 字段一一对应) ────────────────

export type CrawlerTaskStatus = 'queued' | 'running' | 'done' | 'stopped' | 'failed';

/** 单任务摘要(monitor.TaskState.to_dict)。*/
export interface CrawlerTaskSummary {
  task_id: string;
  query: string;
  status: CrawlerTaskStatus;
  stage: string;
  saved: number;
  skipped: number;
  failed: number;
  started_at: number;
  ended_at: number;
  params: Record<string, number>;
  error: string;
  /** 全链路计数:请求发起→解析→落库→反爬应对(见 monitor.py TaskState.counters) */
  counters: Record<string, number>;
  quality: { count: number; avg_score: number; min_score: number; flags: Record<string, number> };
  elapsed: number;
}

export interface CrawlerTaskDetail extends CrawlerTaskSummary {
  logs?: string[];
}

/** 动态并发池快照(scheduler.DynamicWorkerPool.snapshot;未初始化时只有 initialized=false)。*/
export interface PoolSnapshot {
  initialized?: boolean;
  workers?: number;
  min_workers?: number;
  max_workers?: number;
  avg_latency?: number;
  fail_rate?: number;
  risk_pending?: number;
  cpu_load?: number | null;
  psutil_available?: boolean;
}

export interface BreakerSnapshot {
  name: string;
  state: 'closed' | 'open' | 'half_open' | string;
  consecutive_failures: number;
  cooldown_left: number;
}

export interface ProxyEntry {
  url: string;
  score: number;
  alive: boolean;
  available: boolean;
  success_count: number;
  fail_count: number;
  last_latency: number;
  cooldown_left: number;
  last_check_ts: number;
}

export interface ProxySnapshot {
  mode: string;
  proxies: ProxyEntry[];
}

export interface AlertItem {
  title: string;
  level: string;
  detail: string;
  ts: number;
  count: number;
}

export interface CrawlerDashboard {
  tasks: {
    total: number;
    running: number;
    done: number;
    failed: number;
    stopped: number;
    saved_total: number;
    recent: CrawlerTaskSummary[];
  };
  pool: PoolSnapshot;
  breakers: BreakerSnapshot[];
  proxy: ProxySnapshot;
  alerts: AlertItem[];
}

/** 运行中可热调整的参数(后端白名单校验,越权键 422)。*/
export interface CrawlerParamsPatch {
  delay_seconds?: number;
  max_workers?: number;
  page_size?: number;
  max_per_keyword?: number;
}

// ─── API ────────────────────────────────────────────────

/** 面板首屏聚合:任务概览 + 动态池 + 断路器 + 代理池 + 最近告警。*/
export const getCrawlerDashboard = () =>
  http.get<CrawlerDashboard>('/crawler/dashboard').then(r => r.data);

export const listCrawlerTasks = (status?: CrawlerTaskStatus) =>
  http.get<CrawlerTaskSummary[]>('/crawler/tasks', { params: status ? { status } : undefined })
    .then(r => r.data);

export const getCrawlerTask = (taskId: string, logs = true) =>
  http.get<CrawlerTaskDetail>(`/crawler/tasks/${taskId}`, { params: { logs } }).then(r => r.data);

export const stopCrawlerTask = (taskId: string) =>
  http.post<{ task_id: string; stopped: boolean }>(`/crawler/tasks/${taskId}/stop`).then(r => r.data);

export const stopAllCrawlerTasks = () =>
  http.post<{ stopped: number }>('/crawler/tasks/stop-all').then(r => r.data);

export const updateCrawlerTaskParams = (taskId: string, patch: CrawlerParamsPatch) =>
  http.put<{ task_id: string; params: Record<string, number> }>(`/crawler/tasks/${taskId}/params`, patch)
    .then(r => r.data);

export const listCrawlerAlerts = () =>
  http.get<{ alerts: AlertItem[] }>('/crawler/alerts').then(r => r.data.alerts);

export const listCrawlerProxies = () =>
  http.get<ProxySnapshot>('/crawler/proxies').then(r => r.data);

export const listCrawlerBreakers = () =>
  http.get<{ breakers: BreakerSnapshot[] }>('/crawler/breakers').then(r => r.data.breakers);

export const resetCrawlerBreaker = (name: string) =>
  http.post<{ name: string; reset: boolean }>(`/crawler/breakers/${name}/reset`).then(r => r.data);

export const getCrawlerPool = () =>
  http.get<PoolSnapshot>('/crawler/pool').then(r => r.data);
