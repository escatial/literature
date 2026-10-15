# -*- coding: utf-8 -*-
"""写作模块测试公共设施:backend/src 与 backend 加入 import 路径。

与 tests/crawler/conftest.py 同构,但只做路径隔离,不初始化爬虫。
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
