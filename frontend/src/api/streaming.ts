/** SSE 流式写作客户端:逐章实时回填。*/
import { getWritingStreamURL } from '@/config/api';
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
  sections: { key: string; title: string; content: string; citations: string[] }[];
  groups: { name: string; lit_ids: string[] }[];
  referenceList: string;
  screenedOutIds: string[];
  droppedCitations: string[];
  progress: { index: number; total: number } | null;
  currentSection: { key: string; title: string; content: string } | null;
  detail: string | null;
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
  currentSection: null,
  detail: null,
  error: null,
};

export async function generateWritingStream(
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
): Promise<WritingResponse> {
  onUpdate(initialState);

  const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;
  const resp = await fetch(getWritingStreamURL(backendOrigin), {
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
            state.detail = `已接收 ${evt.data.total_papers ?? 0} 篇文献，准备启动综述写作...`;
            break;
          case 'screening_started':
            state.phase = 'screening';
            state.detail = `正在进行 LLM 主题筛选，共 ${evt.data.total ?? 0} 篇文献...`;
            break;
          case 'screening_done':
            state.screenedOutIds = evt.data.screened_out ?? [];
            state.detail = `主题筛选完成，保留 ${evt.data.kept ?? 0} 篇，剔除 ${state.screenedOutIds.length} 篇。`;
            break;
          case 'classify_started':
            state.phase = 'classify';
            state.detail = `正在按${evt.data.classify_mode === 'theme' ? '主题' : '国内外'}方式进行文献分组...`;
            break;
          case 'classify_done':
            state.phase = 'classify';
            state.groups = evt.data.groups ?? [];
            state.detail = `文献分组完成，共得到 ${state.groups.length} 个分组。`;
            break;
          case 'section_preparing':
            state.phase = 'writing';
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: '',
            };
            state.detail = evt.data.message ?? `正在准备《${evt.data.title}》...`;
            break;
          case 'section_started':
            state.phase = 'writing';
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: state.currentSection?.key === evt.data.key ? state.currentSection.content : '',
            };
            state.detail = `正在流式生成《${evt.data.title}》...`;
            break;
          case 'section_token': {
            state.phase = 'writing';
            const currentSection = (!state.currentSection || state.currentSection.key !== evt.data.key)
              ? {
                  key: evt.data.key,
                  title: evt.data.title,
                  content: '',
                }
              : state.currentSection;
            state.currentSection = {
              ...currentSection,
              content: `${currentSection.content}${evt.data.delta ?? ''}`,
            };
            const charCount = state.currentSection.content.length;
            state.detail = `正在流式生成《${state.currentSection.title}》，已输出 ${charCount} 字...`;
            break;
          }
          case 'section_done':
            state.sections.push({
              key: evt.data.key,
              title: evt.data.title,
              content: evt.data.content,
              citations: evt.data.citations ?? [],
            });
            state.droppedCitations.push(...(evt.data.dropped_citations ?? []));
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: evt.data.content,
            };
            state.detail = `《${evt.data.title}》已完成，包含 ${(evt.data.citations ?? []).length} 处引用。`;
            break;
          case 'reference_started':
            state.phase = 'reference';
            state.detail = `正在整理参考文献，共 ${evt.data.count ?? 0} 篇...`;
            break;
          case 'reference_list':
            state.phase = 'reference';
            state.referenceList = evt.data.reference_list ?? '';
            state.detail = '参考文献列表已生成，正在收尾...';
            break;
          case 'complete':
            state.phase = 'complete';
            state.detail = '综述生成完成。';
            break;
          case 'error':
            state.phase = 'error';
            state.error = evt.data.message;
            state.detail = evt.data.message;
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
