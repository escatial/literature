"""自动化访问模块:中国知网(嵌入的 HTTP 爬虫,不再依赖 Playwright)。

爬虫代码嵌入在 `automation/cnki/`(crawler.py + cjy_client.py + config.yaml),
adapter 直接 import 调用;cookie / config.yaml / 超级鹰验证码识别链路由包内维护。
"""

from .cnki_adapter import (
    build_query,
    check_cookies_health,
    run_cnki_full_auto,
)

__all__ = [
    "build_query",
    "check_cookies_health",
    "run_cnki_full_auto",
]
