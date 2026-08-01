/** fetch 客户端封装。*/
const BASE = '/api';

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`API ${path} 失败(${resp.status}): ${text || resp.statusText}`);
  }
  return resp.json();
}
