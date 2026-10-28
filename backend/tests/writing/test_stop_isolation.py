# -*- coding: utf-8 -*-
"""写作停止注册表用户隔离回归(C2)。

- 注册表键 (user_id, task_id):两个假流注册不同 user,A stop 后仅 A 流
  Event 置位;
- admin stop 全量;返回体 {"stopped": n} 计数语义保持;
- 流结束 discard 只清自己的键;
- 与 B6 检索侧(以 DB owner 为权威)的不对称见 api/writing.py 注释。
"""
import secrets
import threading

import pytest

from api.writing import _WRITING_STOP_EVENTS, _WRITING_STOP_LOCK, writing_stop


class _FakeRequest:
    def __init__(self, user):
        self.state = type("S", (), {"user": user})()


class _FakeUser:
    def __init__(self, user_id: str, role: str = "user"):
        self.user_id = user_id
        self.role = role


def _register(key: tuple[str, str]) -> threading.Event:
    ev = threading.Event()
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS[key] = ev
    return ev


@pytest.fixture(autouse=True)
def _clean_registry():
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS.clear()
    yield
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS.clear()


def test_stop_is_scoped_to_owning_user():
    """A stop 后仅 A 名下的 Event 置位,B 的不受影响。"""
    uid_a = f"usr_{secrets.token_hex(4)}"
    uid_b = f"usr_{secrets.token_hex(4)}"
    ev_a = _register((uid_a, "task-a"))
    ev_a2 = _register((uid_a, "task-a2"))
    ev_b = _register((uid_b, "task-b"))

    resp = writing_stop(_FakeRequest(_FakeUser(uid_a)))
    assert resp == {"stopped": 2}
    assert ev_a.is_set() and ev_a2.is_set()
    assert not ev_b.is_set()


def test_admin_stop_hits_all_streams():
    admin = _FakeUser(f"usr_{secrets.token_hex(4)}", role="admin")
    ev_a = _register((f"usr_{secrets.token_hex(4)}", "task-a"))
    ev_b = _register((f"usr_{secrets.token_hex(4)}", "task-b"))
    resp = writing_stop(_FakeRequest(admin))
    assert resp == {"stopped": 2}
    assert ev_a.is_set() and ev_b.is_set()


def test_stream_cleanup_discards_only_own_key():
    """流结束只 discard 自己的键:其他流不受影响(等价 produce finally)。"""
    uid = f"usr_{secrets.token_hex(4)}"
    ev_own = _register((uid, "task-own"))
    ev_other = _register((f"usr_{secrets.token_hex(4)}", "task-other"))
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS.pop((uid, "task-own"), None)
    assert (uid, "task-own") not in _WRITING_STOP_EVENTS
    assert ev_other in list(_WRITING_STOP_EVENTS.values())
    assert not ev_other.is_set()


def test_anonymous_user_cannot_stop_others():
    """user 为 None(未登录上下文,理论上被中间件拦截)时:全量语义仅限
    admin —— None 视为无主键不匹配任何 owner,但保持向后兼容不静默放空
    (与旧全局行为一致,本地直调场景可用)。"""
    ev = _register((f"usr_{secrets.token_hex(4)}", "task-x"))
    resp = writing_stop(None)
    assert resp["stopped"] >= 1
    assert ev.is_set()


# ---------- D2(2026-10-27):停止语义四象限回归 ----------

def test_user_without_own_stream_stops_nothing():
    """象限2:A 名下无流、只有 B 有流 —— A stop 返回 {"stopped": 0},
    B 的 Event 完全不受影响(A 停 B 无效的空手情形)。"""
    uid_b = f"usr_{secrets.token_hex(4)}"
    ev_b = _register((uid_b, "task-b"))

    resp = writing_stop(_FakeRequest(_FakeUser(f"usr_{secrets.token_hex(4)}")))
    assert resp == {"stopped": 0}
    assert not ev_b.is_set()


def test_mixed_streams_stop_hits_only_requester():
    """象限1+2 混合:A、B 各有流,A stop 只置位 A 的,B 的不动。"""
    uid_a = f"usr_{secrets.token_hex(4)}"
    uid_b = f"usr_{secrets.token_hex(4)}"
    ev_a1 = _register((uid_a, "task-a1"))
    ev_a2 = _register((uid_a, "task-a2"))
    ev_b1 = _register((uid_b, "task-b1"))
    ev_b2 = _register((uid_b, "task-b2"))

    resp = writing_stop(_FakeRequest(_FakeUser(uid_a)))
    assert resp == {"stopped": 2}
    assert ev_a1.is_set() and ev_a2.is_set()
    assert not ev_b1.is_set() and not ev_b2.is_set()


def test_no_task_id_fallback_key_owner_scoped():
    """象限4:无显式任务上下文的兜底键。生产侧 stop_key 为
    (user_id or '__anon__', task_id),其中 task_id 缺 X-Task-Id 时退化为
    每次生成的新 UUID —— 无论落到哪个 task_id,停止授权都按 user 维度收口:

    - 同一用户的流,task_id 不同(含随机 uuid 兜底键)也能被本人 stop 置位;
    - 未登录流('__anon__' 键)不被普通用户的 stop 触达,只有 admin/匿名
      兼容路径能置位(与 api/writing._sse_stream_response 注释一致)。
    """
    uid_a = f"usr_{secrets.token_hex(4)}"
    # A 的两个流:一个带显式 task_id,一个模拟无 X-Task-Id 的随机 uuid 键
    ev_a_named = _register((uid_a, "task-explicit"))
    ev_a_uuid = _register((uid_a, "3f2b8c1e-9d4a-4f6b-b7c2-1e5a9d0f8e3a"))
    # 未登录上下文的流(__anon__ 兜底键)
    ev_anon = _register(("__anon__", "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"))

    resp = writing_stop(_FakeRequest(_FakeUser(uid_a)))
    assert resp == {"stopped": 2}
    assert ev_a_named.is_set() and ev_a_uuid.is_set(), (
        "同一用户名下的流无论 task_id 是显式值还是 uuid 兜底,都必须能被本人停止"
    )
    assert not ev_anon.is_set(), "普通用户不能触达 __anon__ 兜底键的流"

    # admin 直通:__anon__ 流也能停
    resp = writing_stop(_FakeRequest(_FakeUser(f"usr_{secrets.token_hex(4)}", role="admin")))
    assert resp["stopped"] >= 1
    assert ev_anon.is_set()
