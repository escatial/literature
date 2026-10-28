/**
 * parseSSELine / phaseCanRegressTo 单测(D3)。
 * 锁定 api/sseTypes.ts 的解析契约:正常帧、多行 data 帧(逐行解析,注释行
 * 跳过)、心跳注释行、未知事件容错、坏 JSON/缺字段容错。
 */
import { describe, expect, it } from 'vitest';

import { parseSSELine, phaseCanRegressTo, SSE_EVENT_NAMES } from '@/api/sseTypes';

describe('parseSSELine', () => {
  it('正常帧:解析出事件名与 data 载荷', () => {
    const line = 'data: {"event":"section_token","data":{"key":"theme_1","delta":"增量"}}';
    const frame = parseSSELine(line);
    expect(frame).not.toBeNull();
    expect(frame!.event).toBe('section_token');
    expect(frame!.data).toEqual({ key: 'theme_1', delta: '增量' });
  });

  it('data: 后无空格也能解析(trim 容错)', () => {
    const frame = parseSSELine('data:{"event":"start","data":{"total_papers":12}}');
    expect(frame).not.toBeNull();
    expect(frame!.event).toBe('start');
    expect(frame!.data.total_papers).toBe(12);
  });

  it('多行 data 帧:逐行独立解析,注释行与空行跳过(与 streaming.ts 消费方式一致)', () => {
    // 模拟一帧多行:注释行 + 两个 data 行
    const frame = ': heartbeat\n: ' + ' '.repeat(16) + '\ndata: {"event":"heartbeat","data":{"elapsed_seconds":3}}\n\n';
    const parsed = frame
      .split('\n')
      .map((l) => parseSSELine(l))
      .filter((x) => x !== null);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].event).toBe('heartbeat');
    expect(parsed[0].data.elapsed_seconds).toBe(3);
  });

  it('心跳注释行:不以 data: 开头,返回 null', () => {
    expect(parseSSELine(': heartbeat')).toBeNull();
    expect(parseSSELine(': ' + ' '.repeat(1024))).toBeNull();
  });

  it('未知事件名:返回 null(容错,不抛异常)', () => {
    expect(parseSSELine('data: {"event":"agent_state","data":{}}')).toBeNull();
    expect(parseSSELine('data: {"event":"future_event","data":{}}')).toBeNull();
  });

  it('坏 JSON / 缺字段 / 空 data 行:一律 null', () => {
    expect(parseSSELine('data: {not json')).toBeNull();
    expect(parseSSELine('data: {"event":"start"}')).toBeNull();
    expect(parseSSELine('data: {"data":{}}')).toBeNull();
    expect(parseSSELine('data: ')).toBeNull();
    expect(parseSSELine('event: start')).toBeNull();
    // data 非 object(payload 是标量)→ data 归一为 {}
    const frame = parseSSELine('data: {"event":"error","data":"plain"}');
    expect(frame).not.toBeNull();
    expect(frame!.data).toEqual({});
  });

  it('W 批通读事件:4 个新事件名全部可解析(readthrough_*/section_notes_done)', () => {
    const names = [
      'readthrough_started',
      'readthrough_chunk_done',
      'readthrough_done',
      'section_notes_done',
    ] as const;
    for (const name of names) {
      expect(SSE_EVENT_NAMES as readonly string[]).toContain(name);
      const frame = parseSSELine(
        `data: {"event":"${name}","data":{"done":1,"total":8,"notes":true}}`,
      );
      expect(frame, name).not.toBeNull();
      expect(frame!.event).toBe(name);
      expect(frame!.data.total).toBe(8);
    }
  });

  it('SSE_EVENT_NAMES 枚举:33 个事件名,parseSSELine 全部可解析', () => {
    expect(SSE_EVENT_NAMES).toHaveLength(33);
    for (const name of SSE_EVENT_NAMES) {
      const frame = parseSSELine(`data: {"event":"${name}","data":{}}`);
      expect(frame, name).not.toBeNull();
      expect(frame!.event).toBe(name);
    }
  });
});

describe('phaseCanRegressTo', () => {
  it('低位事件不得把高位阶段回退', () => {
    // writing(6) 已进入,start(1) 不允许回退
    expect(phaseCanRegressTo('writing', 'start')).toBe(false);
    // complete(9) 之后,classify(3) 不允许回退
    expect(phaseCanRegressTo('complete', 'classify')).toBe(false);
  });

  it('同位/升位/未知目标允许', () => {
    expect(phaseCanRegressTo('start', 'screening')).toBe(true);
    expect(phaseCanRegressTo('classify', 'classify')).toBe(true);
    expect(phaseCanRegressTo('writing', 'complete')).toBe(true);
    // qa(7) 与 reference(7) 同位:qa 事件可在 reference 后到达
    expect(phaseCanRegressTo('reference', 'qa')).toBe(true);
  });
});
