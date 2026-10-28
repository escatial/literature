# -*- coding: utf-8 -*-
"""出站请求 SSRF guard 专项(词法收窄断言 + spotcheck Crossref 回源)。

覆盖 Mimosa 门禁"SSRF"高危项第二轮回归保护:
- _assert_public_http_url:请求行上游静态可见的公网收窄断言
- _guard_outbound_url:validate_external_url + 断言双保险
- spotcheck._fetch_crossref_title:固定公网 base + DOI quote + 请求前校验
"""
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import pytest  # noqa: E402

from llm.client import _assert_public_http_url, _guard_outbound_url  # noqa: E402
from qa import spotcheck  # noqa: E402


class TestAssertPublicHttpUrl:
    """词法收窄断言:内网字面量/本机主机名/非 http scheme 必拒。"""

    @pytest.mark.parametrize("bad", [
        "http://localhost/v1",
        "http://127.0.0.1:8000/v1",
        "http://192.168.1.10/v1",
        "http://10.0.0.5/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.internal/v1",
        "ftp://llm.example.com/v1",
        "not-a-url",
    ])
    def test_blocks_internal_literals(self, bad):
        with pytest.raises(RuntimeError):
            _assert_public_http_url(bad)

    @pytest.mark.parametrize("good", [
        "https://api.minimax.cn/anthropic/v1/messages",
        "https://api.minimax.cn/v1/models",
        "https://llm.example.com/v1",
        "http://public.api.example.org/v1",
    ])
    def test_allows_public_gateways(self, good):
        # 公网域名放行(不可解析域名同样放行:本断言只看 scheme/host 字面量)
        _assert_public_http_url(good)


class TestGuardOutboundUrl:
    """guard 与断言双保险:内网两道都拦,公网放行。"""

    def test_blocks_loopback(self):
        with pytest.raises(RuntimeError):
            _guard_outbound_url("http://127.0.0.1:9000/v1/messages")

    def test_allows_public_gateway(self):
        url = _guard_outbound_url("https://api.minimax.cn/anthropic/v1/messages")
        assert url == "https://api.minimax.cn/anthropic/v1/messages"


class TestSpotcheckCrossrefGuard:
    """spotcheck Crossref 回源:DOI quote 编码 + 请求前公网校验。"""

    def test_url_is_fixed_public_base_with_quoted_doi(self, monkeypatch):
        seen = {}

        def fake_validate(url):
            seen["url"] = url
            return url

        def boom(*args, **kwargs):
            seen["reached"] = True
            raise OSError("network off")

        monkeypatch.setattr("security.auth.validate_external_url", fake_validate)
        monkeypatch.setattr(spotcheck.urllib.request, "urlopen", boom)
        with pytest.raises(OSError):
            spotcheck._fetch_crossref_title("10.1000/abc.def")
        # DOI 的 "/" 已编码:URL 不含可操控路径成分,host 固定 Crossref 公网
        assert seen["url"] == "https://api.crossref.org/works/10.1000%2Fabc.def"
        assert seen.get("reached")  # 公网地址放行后触达网络层

    def test_rejected_url_returns_none_as_unreachable(self, monkeypatch):
        def reject(_url):
            raise ValueError("Base URL 不允许指向内网/保留地址")

        monkeypatch.setattr("security.auth.validate_external_url", reject)

        def boom(*args, **kwargs):
            raise AssertionError("被拒地址不得触达网络层")

        monkeypatch.setattr(spotcheck.urllib.request, "urlopen", boom)
        assert spotcheck._fetch_crossref_title("10.1000/xyz") is None
