import { useState } from 'react';
import { postJSON } from '../api/client';
import type { ImportCnResponse } from '../api/types';

export function useImporter() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ImportCnResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function importText(raw_text: string) {
    setLoading(true);
    setError(null);
    try {
      const data = await postJSON<ImportCnResponse>('/import/cn', { raw_text });
      setResult(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return { loading, result, error, importText };
}
