"""Topic name must reject coordinating conjunctions like 与/和/及/以及 which indicate the LLM merged multiple themes."""
from writing.classifier import _group_name_acceptable


def test_coordinating_conjunction_in_title_is_rejected():
    # Parallel structure (e.g., "demand and uncertain") is rejected
    assert not _group_name_acceptable("需求与响应不确定性与鲁棒优化")
    assert not _group_name_acceptable("不确定环境下动态重规划与鲁棒优化")
    assert not _group_name_acceptable("公共卫生应急与城市末端配送")
    assert not _group_name_acceptable("选址路径和网络设计")
    assert not _group_name_acceptable("面向应急场景的多目标以及公平性研究")


def test_titles_without_coordinators_are_kept():
    # Pure no-op topic names without connectors are still allowed
    assert _group_name_acceptable("动态重规划")
    assert _group_name_acceptable("鲁棒优化方法")
    assert _group_name_acceptable("末端配送路径")
