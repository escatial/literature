# -*- coding: utf-8 -*-
"""security 域测试:backend/src 加入 import 路径。"""
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
