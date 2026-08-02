/** SSE 流式写作客户端:逐章实时回填。*/
import { http } from './http';
import type { WritingRequest, WritingResponse } from './types';

export type StreamPhase =
  | 'idle'
  | 'start'
  | 'screening'
  | 'classify'
  | 'writing'
  | 'reference'
  | 'complete'
  | 'error';

export interface StreamState {
  phase: StreamPhase;
  /** 已完成的章节 */
  sections: { key: string; title: string; content: string; citations: string[] }[];
  groups: { name: string; lit_ids: string[] }[];
  referenceList: string;
  screenedOutIds: string[];
  droppedCitations: string[];
  progress: { index: number; total: number } | null;
  error: string | null;
}

const initialState: StreamState = {
  phase: 'idle',
  sections: [],
  groups: [],
  referenceList: '',
  screenedOutIds: [],
  droppedCitations: [],
  progress: null,
  error: null,
};

/** 走 fetch + getReader 解析 SSE(不走 EventSource,因 EventSource 不支持 POST body)。*/
export async function generateWritingStream(
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
): Promise<WritingResponse> {
  onUpdate(initialState);

  const resp = await fetch(`${http.defaults.baseURL}/writing/generate-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });

  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let state: StreamState = { ...initialState };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE event 边界是 \n\n
    const parts = buffer.split('\n\n');
    buffer = parts.pop() ?? '';

    for (const part of parts) {
      const lines = part.split('\n').filter(Boolean);
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        let evt: { event: string; data: any };
        try {
          evt = JSON.parse(payload);
        } catch {
          continue;
        }

        switch (evt.event) {
          case 'start':
            state.phase = 'start';
            break;
          case 'screening_started':
            state.phase = 'screening';
            break;
          case 'screening_done':
            state.screenedOutIds = evt.data.screened_out ?? [];
            break;
          case 'classify_done':
            state.phase = 'classify';
            state.groups = evt.data.groups ?? [];
            break;
          case 'section_started':
            state.phase = 'writing';
            state.progress = { index: evt.data.index, total: evt.data.total };
            break;
          case 'section_done':
            state.sections.push({
              key: evt.data.key,
              title: evt.data.title,
              content: evt.data.content,
              citations: evt.data.citations ?? [],
            });
            state.droppedCitations.push(...(evt.data.dropped_citations ?? []));
            state.progress = { index: evt.data.index + 1, total: evt.data.total };
            break;
          case 'reference_list':
            state.phase = 'reference';
            state.referenceList = evt.data.reference_list ?? '';
            break;
          case 'complete':
            state.phase = 'complete';
            break;
          case 'error':
            state.phase = 'error';
            state.error = evt.data.message;
            break;
        }
        onUpdate({ ...state });
      }
    }
  }

  if (state.phase === 'error') {
    throw new Error(state.error ?? '流式生成失败');
  }
  return {
    topic: req.topic,
    classify_mode: req.classify_mode,
    groups: state.groups,
    sections: state.sections,
    reference_list: state.referenceList,
    screened_out_ids: state.screenedOutIds,
    dropped_citations: state.droppedCitations,
  } satisfies WritingResponse;
}