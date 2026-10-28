# -*- coding: utf-8 -*-
"""agent 模块测试路径隔离(与 tests/writing/conftest.py 同构)。"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
