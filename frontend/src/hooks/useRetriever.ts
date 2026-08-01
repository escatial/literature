import { useState } from 'react';
import { postJSON } from '../api/client';
import type { SearchRequest, SearchResponse } from '../api/types';

export function useRetriever() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(req: SearchRequest) {
    setLoading(true);
    setError(null);
    try {
      const data = await postJSON<SearchResponse>('/retrieval/search', req);
      setResult(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return { loading, result, error, search };
}
