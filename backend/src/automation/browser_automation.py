"""学术数据库自动化访问模块：中国知网、维普、万方。

核心功能：
1. 无头模式静默访问
2. 人机验证检测与自动切换可视化模式
3. 浏览器生命周期管理
4. 验证完成后自动切回无头模式

使用示例：
    with ScholarBrowser() as browser:
        result = browser.visit_cnki("人工智能")
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)


class BrowserMode(Enum):
    HEADLESS = "headless"      # 无头模式（静默后台）
    HEADFUL = "headful"        # 可视化模式（人工干预）


class VerificationType(Enum):
    NONE = "none"
    CAPTCHA = "captcha"          # 图形验证码
    SLIDER = "slider"            # 滑块验证
    SMS = "sms"                  # 短信验证
    FACE = "face"                # 人脸识别
    LOGIN = "login"              # 登录验证
    UNKNOWN = "unknown"


@dataclass
class VerificationResult:
    detected: bool
    type: VerificationType
    message: str = ""
    selector: str | None = None


@dataclass
class PageResult:
    url: str
    title: str
    content_preview: str
    success: bool
    error: str | None = None
    verification_required: bool = False


class ScholarBrowser:
    """学术数据库浏览器自动化控制器。"""

    # 各数据库的人机验证特征
    VERIFICATION_PATTERNS = {
        "cnki": {
            "captcha": [
                r"验证码",
                r"请输入.*验证",
                r"captcha",
                r"verify.*code",
            ],
            "slider": [
                r"滑块",
                r"拖动.*滑块",
                r"slide.*verify",
                r"nc\.tm",  # 阿里滑块
                r"geetest",  # 极验
            ],
            "login": [
                r"登录",
                r"账号.*密码",
                r"用户.*登录",
                r"请.*登录",
                r"login",
            ],
            "face": [
                r"人脸",
                r"面部.*识别",
                r"face.*verify",
            ],
            "sms": [
                r"短信",
                r"手机.*验证",
                r"sms",
            ],
        },
        "cqvip": {
            "captcha": [r"验证码", r"captcha", r"verify"],
            "slider": [r"滑块", r"拖动"],
            "login": [r"登录", r"账号"],
        },
        "wanfang": {
            "captcha": [r"验证码", r"captcha"],
            "slider": [r"滑块", r"geetest"],
            "login": [r"登录", r"用户"],
        },
    }

    # CSS 选择器（更精确的验证元素定位）
    VERIFICATION_SELECTORS = {
        "captcha_img": "img[src*='captcha'], img[alt*='验证码'], .verify-img",
        "slider_track": ".slider-track, .nc-lang-cnt, .geetest_slider_track",
        "slider_button": ".slider-button, .nc_iconfont, .geetest_slider_button",
        "login_form": "form[action*='login'], .login-form, #loginForm",
        "iframe_verify": "iframe[src*='captcha'], iframe[src*='verify']",
    }

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 30,
        user_agent: str | None = None,
    ):
        self.default_headless = headless
        self.timeout = timeout * 1000  # Playwright 用毫秒
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._mode = BrowserMode.HEADLESS if headless else BrowserMode.HEADFUL
        self._verification_event = asyncio.Event()
        self._verification_completed = False
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self) -> None:
        """启动浏览器进程。"""
        if self._browser:
            return

        self._playwright = await async_playwright().start()
        await self._launch_browser(self._mode)
        log.info("浏览器已启动，模式: %s", self._mode.value)

    async def _launch_browser(self, mode: BrowserMode) -> None:
        """根据模式启动浏览器。"""
        headless = (mode == BrowserMode.HEADLESS)

        # 关闭现有浏览器
        if self._browser:
            await self._browser.close()

        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        # 注入脚本隐藏自动化特征
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)

        self._page = await self._context.new_page()
        self._mode = mode

        # 设置页面事件监听
        self._page.on("console", lambda msg: log.debug("Console: %s", msg.text))
        self._page.on("pageerror", lambda err: log.error("Page error: %s", err))

    async def _switch_mode(self, new_mode: BrowserMode) -> None:
        """切换浏览器模式（无头 ↔ 可视化）。"""
        if self._mode == new_mode:
            return

        async with self._lock:
            log.info("切换浏览器模式: %s -> %s", self._mode.value, new_mode.value)

            # 保存当前页面状态
            current_url = self._page.url if self._page else None
            cookies = await self._context.cookies() if self._context else []

            # 关闭旧浏览器
            await self._browser.close()

            # 启动新浏览器
            await self._launch_browser(new_mode)

            # 恢复状态
            if cookies:
                await self._context.add_cookies(cookies)
            if current_url and current_url != "about:blank":
                await self._page.goto(current_url, wait_until="networkidle")

            log.info("模式切换完成")

    async def detect_verification(self, db_type: str = "cnki") -> VerificationResult:
        """检测当前页面是否存在人机验证。"""
        if not self._page:
            return VerificationResult(detected=False, type=VerificationType.NONE)

        content = await self._page.content()
        title = await self._page.title()
        url = self._page.url

        patterns = self.VERIFICATION_PATTERNS.get(db_type, {})

        # 检查各种验证类型
        for vtype, pattern_list in patterns.items():
            for pattern in pattern_list:
                if re.search(pattern, content, re.I) or re.search(pattern, title, re.I):
                    log.warning("检测到验证: %s (%s)", vtype, pattern)
                    return VerificationResult(
                        detected=True,
                        type=VerificationType(vtype),
                        message=f"页面包含 {vtype} 验证特征",
                        selector=pattern,
                    )

        # 检查特定元素
        for name, selector in self.VERIFICATION_SELECTORS.items():
            try:
                element = await self._page.query_selector(selector)
                if element:
                    log.warning("检测到验证元素: %s", selector)
                    vtype = VerificationType.CAPTCHA if "captcha" in name else VerificationType.SLIDER
                    return VerificationResult(
                        detected=True,
                        type=vtype,
                        message=f"发现验证元素: {name}",
                        selector=selector,
                    )
            except Exception:
                pass

        return VerificationResult(detected=False, type=VerificationType.NONE)

    async def wait_for_verification_complete(
        self,
        check_interval: float = 2.0,
        max_wait: float = 300.0,
    ) -> bool:
        """等待用户完成验证（可视化模式下）。"""
        if self._mode != BrowserMode.HEADFUL:
            return True

        log.info("等待用户完成验证...")
        start_time = time.time()

        while time.time() - start_time < max_wait:
            # 检测验证是否消失
            result = await self.detect_verification()
            if not result.detected:
                # 额外检查：确保页面已正常加载
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=5000)
                    log.info("验证已完成，页面恢复正常")
                    return True
                except PlaywrightTimeout:
                    pass

            await asyncio.sleep(check_interval)

        log.error("等待验证超时")
        return False

    async def visit(
        self,
        url: str,
        db_type: str = "cnki",
        auto_handle_verification: bool = True,
    ) -> PageResult:
        """访问指定数据库页面，自动处理验证。"""
        if not self._page:
            await self.start()

        try:
            log.info("访问: %s", url)
            await self._page.goto(url, wait_until="networkidle", timeout=self.timeout)

            # 检测验证
            verification = await self.detect_verification(db_type)

            if verification.detected and auto_handle_verification:
                log.warning("需要人工验证: %s", verification.message)

                # 切换到可视化模式
                await self._switch_mode(BrowserMode.HEADFUL)

                # 通知用户（通过回调或事件）
                self._verification_event.set()

                # 等待用户完成
                success = await self.wait_for_verification_complete()

                if success:
                    # 切回无头模式
                    await self._switch_mode(BrowserMode.HEADLESS)
                    self._verification_completed = True
                else:
                    return PageResult(
                        url=url,
                        title="",
                        content_preview="",
                        success=False,
                        error="验证超时",
                        verification_required=True,
                    )

            # 获取页面信息
            title = await self._page.title()
            content = await self._page.content()
            preview = re.sub(r"\s+", " ", content)[:500]

            return PageResult(
                url=url,
                title=title,
                content_preview=preview,
                success=True,
            )

        except PlaywrightTimeout:
            return PageResult(
                url=url,
                title="",
                content_preview="",
                success=False,
                error="页面加载超时",
            )
        except Exception as e:
            log.exception("访问失败")
            return PageResult(
                url=url,
                title="",
                content_preview="",
                success=False,
                error=str(e),
            )

    async def visit_cnki(self, keyword: str) -> PageResult:
        """访问中国知网并搜索。"""
        search_url = f"https://kns.cnki.net/kns8s/defaultresult/index?kw={keyword}"
        return await self.visit(search_url, db_type="cnki")

    async def visit_cqvip(self, keyword: str) -> PageResult:
        """访问维普网并搜索。"""
        search_url = f"http://qikan.cqvip.com/Qikan/Search/Index?key={keyword}"
        return await self.visit(search_url, db_type="cqvip")

    async def visit_wanfang(self, keyword: str) -> PageResult:
        """访问万方数据并搜索。"""
        search_url = f"https://s.wanfangdata.com.cn/paper?q={keyword}"
        return await self.visit(search_url, db_type="wanfang")

    async def screenshot(self, path: str) -> None:
        """截图当前页面。"""
        if self._page:
            await self._page.screenshot(path=path, full_page=True)
            log.info("截图已保存: %s", path)

    async def close(self) -> None:
        """关闭浏览器，释放资源。"""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None
            self._page = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        log.info("浏览器已关闭")

    @property
    def is_verification_pending(self) -> bool:
        """是否有待处理的验证。"""
        return self._verification_event.is_set() and not self._verification_completed

    async def get_cookies(self) -> list[dict]:
        """获取当前 cookies（用于 session 保持）。"""
        return await self._context.cookies() if self._context else []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """设置 cookies。"""
        if self._context:
            await self._context.add_cookies(cookies)


# 便捷函数
@asynccontextmanager
async def scholar_browser(headless: bool = True):
    """上下文管理器：自动管理浏览器生命周期。"""
    browser = ScholarBrowser(headless=headless)
    try:
        await browser.start()
        yield browser
    finally:
        await browser.close()


# 同步包装器（用于非异步环境）
def visit_cnki_sync(keyword: str, headless: bool = True) -> dict[str, Any]:
    """同步访问知网（供 FastAPI 等同步框架调用）。"""
    async def _run():
        async with scholar_browser(headless=headless) as browser:
            result = await browser.visit_cnki(keyword)
            return {
                "success": result.success,
                "title": result.title,
                "error": result.error,
                "verification_required": result.verification_required,
            }

    return asyncio.run(_run())


if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)

    async def test():
        async with scholar_browser(headless=False) as browser:
            result = await browser.visit_cnki("人工智能")
            print(f"成功: {result.success}")
            print(f"标题: {result.title}")
            print(f"需要验证: {result.verification_required}")

    asyncio.run(test())
