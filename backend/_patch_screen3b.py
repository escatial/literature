# -*- coding: utf-8 -*-
"""补丁 3(修正版):剩余块。跑完即删。"""
import re

p = 'src/writing/orchestrator.py'
s = open(p, encoding='utf-8').read()

def rep(old, new):
    global s
    pattern = r'\r?\n'.join(re.escape(part) for part in old.split('\n'))
    m = re.search(pattern, s)
    assert m, 'MISSING: ' + old[:80].replace('\n', '|')
    s = s[:m.start()] + new + s[m.end():]

# 0) tap 工厂(插在 _screen_papers_stream 定义前)
rep('''def _screen_papers_stream(
    topic: str,
    papers: list[Paper],
    do_screening: bool,
    thinking_sink=None,
) -> Generator[dict, None, tuple[list[Paper], list[str], dict]]:''',
'''def _screening_tap_factory(thinking_sink):
    """筛选批思考/输出直播:按批构造 ThinkingTap,增量经 thinking_sink 外发。"""
    if thinking_sink is None:
        return None

    def factory(batch_index: int):
        from src.writing.readthrough import ThinkingTap

        def sink(payload: dict) -> None:
            thinking_sink({"kind": "stage_thinking", "stage": "screening", **payload})

        return ThinkingTap(sink, "screening", chunk=batch_index)

    return factory


def _screen_papers_stream(
    topic: str,
    papers: list[Paper],
    do_screening: bool,
    thinking_sink=None,
) -> Generator[dict, None, tuple[list[Paper], list[str], dict]]:''')

# 1) 两个 screen_batch_stream 调用点(签名已改,此处补 tap_factory)
rep('''    decisions: list[dict] | None = None
    for event in screen_batch_stream(papers, topic):''',
'''    tap_factory = _screening_tap_factory(thinking_sink)
    decisions: list[dict] | None = None
    for event in screen_batch_stream(papers, topic, tap_factory=tap_factory):''')

rep('''    merged_decisions: list[dict] | None = None
    for event in screen_batch_stream(merged, topic):''',
'''    merged_decisions: list[dict] | None = None
    for event in screen_batch_stream(merged, topic, tap_factory=tap_factory):''')

# 2) 两个筛选调用点传 sink
rep("        screening_iterator = iter(_screen_papers_stream(topic, papers, True))",
    "        screening_sink = (lambda payload: progress(payload)) if progress is not None else None\n"
    "        screening_iterator = iter(_screen_papers_stream(topic, papers, True, thinking_sink=screening_sink))")

rep("            screening_iterator = iter(_screen_papers_stream(topic, papers, do_screening))",
    "            screening_sink = (lambda payload: progress(\"stage_thinking\", payload)) if progress is not None else None\n"
    "            screening_iterator = iter(_screen_papers_stream(topic, papers, do_screening, thinking_sink=screening_sink))")

open(p, 'w', encoding='utf-8').write(s)
print('ORCHESTRATOR DONE')

# 3) worker publish seq 加锁
p2 = 'src/api/writing.py'
s2 = open(p2, encoding='utf-8').read()

old = '''        # D1:流内事件序号 + run 级落盘日志
        seq_counter = 0
        run_logger = _RunEventLogger(reg_key[1] if reg_key else None)

        def publish(kind: str, payload: Any) -> None:
            nonlocal seq_counter
            # 停止后丢弃尚未入队的进度，避免前端在点击停止后又收到旧事件。
            if stop_event.is_set() and kind == "chunk":
                return
            if kind == "chunk":
                seq_counter += 1
                payload = _inject_seq(payload, seq_counter)
            run_logger.log(kind, payload)
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))'''
new = '''        # D1:流内事件序号 + run 级落盘日志
        seq_counter = 0
        seq_lock = threading.Lock()
        run_logger = _RunEventLogger(reg_key[1] if reg_key else None)

        def publish(kind: str, payload: Any) -> None:
            nonlocal seq_counter
            # 停止后丢弃尚未入队的进度，避免前端在点击停止后又收到旧事件。
            if stop_event.is_set() and kind == "chunk":
                return
            if kind == "chunk":
                # J1:筛选 tap 在 4 个并行批线程发事件,seq 分配必须互斥
                with seq_lock:
                    seq_counter += 1
                    payload = _inject_seq(payload, seq_counter)
            run_logger.log(kind, payload)
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))'''
pattern = r'\r?\n'.join(re.escape(part) for part in old.split('\n'))
m = re.search(pattern, s2)
assert m, 'worker publish anchor missing'
s2 = s2[:m.start()] + new + s2[m.end():]
open(p2, 'w', encoding='utf-8').write(s2)
print('WORKER LOCK DONE')
