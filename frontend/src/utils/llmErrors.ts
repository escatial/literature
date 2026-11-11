/** LLM 服务商错误的友好化映射(402 额度不足 / 401 鉴权 / 超时)。
 *
 * 服务商原始错误(英文错误码 + 长文案)直接抛给用户没有可操作性;
 * 这里按模式识别给出"该做什么",原始错误截断附带,便于排查。
 * 匹配不到已知模式时原样返回,不装作认识。
 */
export function friendlyLlmError(raw: unknown, fallback = 'LLM 调用失败'): string {
  const s = String(raw ?? '');
  if (/insufficient_balance|Token Plan 用量上限|用量上限|额度不足|quota exceeded|\b402\b/i.test(s)) {
    return `LLM 服务商额度不足:请前往服务商控制台升级套餐或充值后重试。(${s.slice(0, 160)})`;
  }
  if (/\b401\b|invalid.?api.?key|unauthorized|authentication|鉴权失败|api key not valid/i.test(s)) {
    return `LLM 鉴权失败:请检查 API Key 是否正确、是否有该模型的访问权限。(${s.slice(0, 160)})`;
  }
  if (/timed? ?out|timeout|连接超时/i.test(s)) {
    return `LLM 调用超时:服务商无响应,请稍后重试或更换模型。(${s.slice(0, 160)})`;
  }
  return s || fallback;
}
