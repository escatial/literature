# -*- coding: utf-8 -*-
"""文献池 API 测试公共设施:backend/src 与 backend 加入 import 路径。

与 tests/writing/conftest.py 同构,只做路径隔离。
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
