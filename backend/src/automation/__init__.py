"""自动化访问模块:中国知网、维普、万方。

远程交互式架构:服务器无头运行,WebSocket 推帧到前端,
用户在前端画布上完成人机验证。
"""

from .browser_automation import (
    ScholarBrowser,
    BrowserMode,
    VerificationType,
    VerificationResult,
    PageResult,
    scholar_browser,
    visit_cnki_sync,
)
from .remote_browser import RemoteBrowserManager, BrowserSession, manager

__all__ = [
    "ScholarBrowser",
    "BrowserMode",
    "VerificationType",
    "VerificationResult",
    "PageResult",
    "scholar_browser",
    "visit_cnki_sync",
    "RemoteBrowserManager",
    "BrowserSession",
    "manager",
]
