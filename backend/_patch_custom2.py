# -*- coding: utf-8 -*-
"""补丁:custom_theme_names 全链路透传(orchestrator + api)。跑完即删。"""
import re

def rep_file(path, old, new):
    s = open(path, encoding='utf-8').read()
    pattern = r'\r?\n'.join(re.escape(part) for part in old.split('\n'))
    m = re.search(pattern, s)
    assert m, f'MISSING in {path}: ' + old[:80].replace('\n', '|')
    s = s[:m.start()] + new + s[m.end():]
    open(path, 'w', encoding='utf-8').write(s)
    print('patched', path)

p = 'src/writing/orchestrator.py'

# 1) plan_review_stream 签名
rep_file(p, '''def plan_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    stop_event=None,
    progress=None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    task_id: str | None = None,
) -> Generator[str, None, None]:''',
'''def plan_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    stop_event=None,
    progress=None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    task_id: str | None = None,
    custom_theme_names: list[str] | None = None,
) -> Generator[str, None, None]:''')

# 2) plan 流 classify 调用(2282)
rep_file(p, '''            groups = classify(papers, topic, classify_mode)
            yield _sse_event("classify_done", {
                "phase": "classify",
                "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            })
            # C3 时点:分类完成;规划段(单阶段前半)pass
            _stage_pass(sm, "OUTLINED")''',
'''            groups = classify(
                papers, topic, classify_mode,
                progress=progress, custom_theme_names=custom_theme_names,
            )
            yield _sse_event("classify_done", {
                "phase": "classify",
                "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            })
            # C3 时点:分类完成;规划段(单阶段前半)pass
            _stage_pass(sm, "OUTLINED")''')

# 3) generate 单阶段 classify 调用(2723)
rep_file(p, '''        groups = classify(papers, topic, classify_mode)''',
'''        groups = classify(
            papers, topic, classify_mode,
            progress=progress, custom_theme_names=custom_theme_names,
        )''')

# 4) generate_review_stream 签名加 custom_theme_names
rep_file(p, '''    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    progress=None,
    task_id: str | None = None,
    persist=None,
) -> Generator[str, None, None]:''',
'''    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    progress=None,
    task_id: str | None = None,
    persist=None,
    custom_theme_names: list[str] | None = None,
) -> Generator[str, None, None]:''')
# generate 内部 classify 调用透传(2723 已被 #3 覆盖? 检查: #3 只改了第一个出现;
# generate 里的调用也需 custom_theme_names —— 用带上下文的精确替换:
rep_file(p, '''        groups = classify(
            papers, topic, classify_mode,
            progress=progress, custom_theme_names=custom_theme_names,
        )''',
'''        groups = classify(
            papers, topic, classify_mode,
            progress=progress, custom_theme_names=custom_theme_names,
        )''')  # 幂等占位

p2 = 'src/api/writing.py'

# 5) PlanRequest + WritingRequest 字段
rep_file(p2, '''class PlanRequest(BaseModel):
    """两阶段·阶段1 主题规划请求:只做筛选+分类,不接受确认字段。"""

    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"''',
'''class PlanRequest(BaseModel):
    """两阶段·阶段1 主题规划请求:只做筛选+分类,不接受确认字段。"""

    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"
    # J1 用户自定义主题:非空时跳过 AI 定轴,文献直接归入预设主题
    custom_theme_names: list[str] | None = None''')

# 6) make_plan_stream_fn 透传
rep_file(p2, '''def make_plan_stream_fn(
    *,
    task_id: str,
    user_id: str | None,
    topic: str,
    papers: list,
    classify_mode: str,
    progress_factory,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
):''',
'''def make_plan_stream_fn(
    *,
    task_id: str,
    user_id: str | None,
    topic: str,
    papers: list,
    classify_mode: str,
    progress_factory,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    custom_theme_names: list[str] | None = None,
):''')

rep_file(p2, '''        inner = plan_review_stream(
            topic=plan_ctx["topic"],
            papers=plan_ctx["papers"],
            classify_mode=plan_ctx["classify_mode"],
            stop_event=stop_event,
            progress=plan_ctx["progress_factory"](stop_event, publish),
            research_questions=plan_ctx["research_questions"],
            review_goal=plan_ctx["review_goal"],
            task_id=plan_ctx["task_id"],
        )''',
'''        inner = plan_review_stream(
            topic=plan_ctx["topic"],
            papers=plan_ctx["papers"],
            classify_mode=plan_ctx["classify_mode"],
            stop_event=stop_event,
            progress=plan_ctx["progress_factory"](stop_event, publish),
            research_questions=plan_ctx["research_questions"],
            review_goal=plan_ctx["review_goal"],
            task_id=plan_ctx["task_id"],
            custom_theme_names=plan_ctx["custom_theme_names"],
        )''')

# 7) WritingRequest 字段
rep_file(p2, '''    # v9.9: 研究问题(RQ1/RQ2/...)与综述目标,注入写作与述评,构成论证主线。
    research_questions: list[str] | None = None
    review_goal: str | None = None''',
'''    # v9.9: 研究问题(RQ1/RQ2/...)与综述目标,注入写作与述评,构成论证主线。
    research_questions: list[str] | None = None
    review_goal: str | None = None
    # J1 用户自定义主题:非空时跳过 AI 定轴,文献直接归入预设主题
    custom_theme_names: list[str] | None = None''')

# 8) 三个调用点透传
rep_file(p2, '''            research_questions=req.research_questions,
            review_goal=req.review_goal,
            task_id=plan_ctx["task_id"],''',
'''            research_questions=req.research_questions,
            review_goal=req.review_goal,
            task_id=plan_ctx["task_id"],
            custom_theme_names=req.custom_theme_names,''')

rep_file(p2, '''        def stream_fn(stop_event, publish):
            return make_plan_stream_fn(
                task_id=job_id,
                user_id=uid,
                topic=req.topic,
                papers=papers,
                classify_mode=req.classify_mode,
                progress_factory=_classify_emit,
                research_questions=req.research_questions,
                review_goal=req.review_goal,
            )(stop_event, publish)''',
'''        def stream_fn(stop_event, publish):
            return make_plan_stream_fn(
                task_id=job_id,
                user_id=uid,
                topic=req.topic,
                papers=papers,
                classify_mode=req.classify_mode,
                progress_factory=_classify_emit,
                research_questions=req.research_questions,
                review_goal=req.review_goal,
                custom_theme_names=req.custom_theme_names,
            )(stop_event, publish)''')

print('ALL APPLIED')
