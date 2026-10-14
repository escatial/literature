from agent.harness import (
    AgentCheck,
    AgentLoopError,
    AgentStage,
    AgentState,
    HarnessPolicy,
    run_repair_loop,
)


def test_repair_loop_rechecks_until_passes():
    state = AgentState(task_id="t1", topic="任意主题")
    produced = []

    def produce(_state):
        produced.append("draft")
        return {"version": 1}

    def inspect(output, _state):
        return [AgentCheck("quality", output["version"] >= 2, ["版本过旧"] if output["version"] < 2 else [])]

    def repair(output, _checks, _state):
        return {"version": output["version"] + 1}

    result = run_repair_loop(
        state,
        stage=AgentStage.THEME,
        policy=HarnessPolicy(max_attempts=3),
        produce=produce,
        inspect=inspect,
        repair=repair,
    )
    assert result["version"] == 2
    assert state.status.value == "passed"
    assert len(state.repairs) == 1


def test_repair_loop_blocks_partial_result_by_default():
    state = AgentState(task_id="t2", topic="任意主题")

    try:
        run_repair_loop(
            state,
            stage=AgentStage.QA,
            policy=HarnessPolicy(max_attempts=1),
            produce=lambda _state: "draft",
            inspect=lambda _output, _state: [AgentCheck("qa", False, ["引用未绑定"])],
        )
    except AgentLoopError:
        pass
    else:
        raise AssertionError("QA 未通过时不应交付半成品")
    assert state.stage == AgentStage.BLOCKED
