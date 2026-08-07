"""浏览器自动化模块测试。

测试场景：
1. 无头模式正常访问
2. 验证检测与可视化切换
3. 验证完成后自动回收
4. 资源泄漏检查
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.automation.browser_automation import (
    ScholarBrowser,
    BrowserMode,
    VerificationType,
    VerificationResult,
    PageResult,
)


class TestVerificationDetection:
    """验证检测逻辑测试。"""

    @pytest.fixture
    def browser(self):
        return ScholarBrowser(headless=True)

    @pytest.mark.asyncio
    async def test_detect_captcha(self, browser):
        """测试验证码检测。"""
        # Mock 页面内容包含验证码特征
        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value='<html>请输入验证码</html>')
        mock_page.title = AsyncMock(return_value="验证页面")
        mock_page.url = "https://example.com"
        mock_page.query_selector = AsyncMock(return_value=None)

        browser._page = mock_page

        result = await browser.detect_verification("cnki")
        assert result.detected is True
        assert result.type == VerificationType.CAPTCHA

    @pytest.mark.asyncio
    async def test_detect_slider(self, browser):
        """测试滑块验证检测。"""
        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value='<html>请拖动滑块完成验证</html>')
        mock_page.title = AsyncMock(return_value="安全验证")
        mock_page.url = "https://example.com"
        mock_page.query_selector = AsyncMock(return_value=None)

        browser._page = mock_page

        result = await browser.detect_verification("cnki")
        assert result.detected is True
        assert result.type == VerificationType.SLIDER

    @pytest.mark.asyncio
    async def test_detect_login(self, browser):
        """测试登录验证检测。"""
        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value='<html>请登录账号</html>')
        mock_page.title = AsyncMock(return_value="用户登录")
        mock_page.url = "https://example.com"
        mock_page.query_selector = AsyncMock(return_value=None)

        browser._page = mock_page

        result = await browser.detect_verification("cnki")
        assert result.detected is True
        assert result.type == VerificationType.LOGIN

    @pytest.mark.asyncio
    async def test_no_verification(self, browser):
        """测试无验证页面。"""
        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value='<html><body>搜索结果</body></html>')
        mock_page.title = AsyncMock(return_value="知网搜索")
        mock_page.url = "https://kns.cnki.net"
        mock_page.query_selector = AsyncMock(return_value=None)

        browser._page = mock_page

        result = await browser.detect_verification("cnki")
        assert result.detected is False
        assert result.type == VerificationType.NONE


class TestModeSwitching:
    """模式切换测试。"""

    @pytest.mark.asyncio
    async def test_switch_to_headful(self):
        """测试切换到可视化模式。"""
        browser = ScholarBrowser(headless=True)

        # Mock playwright
        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        browser._playwright = mock_playwright
        browser._browser = mock_browser
        browser._context = mock_context
        browser._page = mock_page
        browser._mode = BrowserMode.HEADLESS

        # Mock launch
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.add_init_script = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        mock_page.url = "https://example.com"
        mock_page.goto = AsyncMock()

        await browser._switch_mode(BrowserMode.HEADFUL)

        assert browser._mode == BrowserMode.HEADFUL
        # close 会被调用两次：_switch_mode 中一次，_launch_browser 中一次
        assert mock_browser.close.call_count == 2

    @pytest.mark.asyncio
    async def test_switch_preserves_state(self):
        """测试切换时保留状态。"""
        browser = ScholarBrowser(headless=True)

        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        browser._playwright = mock_playwright
        browser._browser = mock_browser
        browser._context = mock_context
        browser._page = mock_page
        browser._mode = BrowserMode.HEADLESS

        # 保存的 cookies
        saved_cookies = [{"name": "session", "value": "abc123"}]
        mock_context.cookies = AsyncMock(return_value=saved_cookies)
        mock_page.url = "https://kns.cnki.net/search"

        # 新浏览器
        new_context = AsyncMock()
        new_page = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=new_context)
        new_context.new_page = AsyncMock(return_value=new_page)
        new_context.add_init_script = AsyncMock()
        new_context.add_cookies = AsyncMock()
        new_page.goto = AsyncMock()

        await browser._switch_mode(BrowserMode.HEADFUL)

        # 验证 cookies 被恢复
        new_context.add_cookies.assert_called_once_with(saved_cookies)
        # 验证 URL 被恢复
        new_page.goto.assert_called_once_with("https://kns.cnki.net/search", wait_until="networkidle")


class TestLifecycle:
    """生命周期管理测试。"""

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """测试上下文管理器自动清理。"""
        browser = ScholarBrowser(headless=True)

        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()

        with patch.object(browser, '_launch_browser', new_callable=AsyncMock):
            browser._playwright = mock_playwright
            browser._browser = mock_browser

            await browser.close()

            mock_browser.close.assert_called_once()
            mock_playwright.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_double_close_safe(self):
        """测试重复关闭安全。"""
        browser = ScholarBrowser(headless=True)
        browser._browser = None
        browser._playwright = None

        # 不应抛出异常
        await browser.close()
        await browser.close()


class TestIntegration:
    """集成测试（需要网络，可选）。"""

    @pytest.mark.skip(reason="需要真实网络环境")
    @pytest.mark.asyncio
    async def test_real_cnki_visit(self):
        """真实访问知网（跳过 CI）。"""
        async with ScholarBrowser(headless=True) as browser:
            result = await browser.visit_cnki("人工智能")
            assert result.success or result.verification_required


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
