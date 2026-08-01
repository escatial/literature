/** 浏览器 SubtleCrypto SHA-256 → 内部 lit_id。 */
export async function hashId(input: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
  return (
    'lit_' +
    Array.from(new Uint8Array(buf))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
      .slice(0, 16)
  );
}
