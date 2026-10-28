# -*- coding: utf-8 -*-
"""crawler 管理面测试的鉴权身份注入(B4 收权适配)。

tests/crawler/conftest.py 已把本目录加入 sys.path,测试文件直接
`from admin_auth_helper import attach_admin_auth`。
"""
import secrets

from db.models import UserModel


def attach_admin_auth(app, role: str = "admin"):
    """给独立挂载 crawler_admin.router 的 app 注入鉴权身份。

    B4 收权后 crawler_admin.router 依赖 require_admin(读 request.state.user,
    生产由 main 鉴权中间件注入);这些测试用独立 FastAPI 实例、无中间件,
    此处以中间件等价物注入随机凭据的内存 UserModel(不落库,crawler 端点
    不触用户库)。role="user" 可用于 403 断言。
    """
    user = UserModel(
        user_id=f"usr_{secrets.token_hex(6)}",
        username=f"crawler-{role}-{secrets.token_hex(4)}",
        email=f"crawler-{secrets.token_hex(4)}@local.dev",
        password_hash="not-a-real-hash",
        role=role,
        status="approved",
        enabled=True,
    )

    @app.middleware("http")
    async def _inject_state_user(request, call_next):
        request.state.user = user
        return await call_next(request)

    return user
