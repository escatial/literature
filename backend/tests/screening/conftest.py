# -*- coding: utf-8 -*-
"""筛选模块测试公共设施:backend/src 与 backend 加入 import 路径。"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
