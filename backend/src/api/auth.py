"""认证与用户管理 API。

公开端点: bootstrap / register / login(注册审核前的最小可用集);
其余全部要求 X-Auth-Token(main.py 鉴权中间件统一校验并注入 request.state.user);
admin/* 端点额外要求 admin 角色。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from audit.writer import audit
from db.models import UserModel
from db import session as db_session
from llm.client import fetch_models_for
from security import auth as sauth
from security import ratelimit

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class LLMConfigIn(BaseModel):
    llm_kind: str = "openai"
    llm_base_url: str = ""
    # 空 = 保持现有 key 不变(脱敏回显导致前端不回传原文)
    llm_api_key: str = ""
    llm_model: str = ""


class ModelsProbeIn(BaseModel):
    llm_kind: str = "openai"
    llm_base_url: str = ""
    llm_api_key: str = ""  # 空 = 使用已保存的 key


def _require_user(request: Request) -> UserModel:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "未登录")
    return user


def _require_admin(request: Request) -> UserModel:
    user = _require_user(request)
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


# B4:跨模块复用的公开别名(crawler_admin 管理面 router 级依赖)。
require_admin = _require_admin


def _client_meta(request: Request) -> dict:
    """B5 审计公共字段:ip 与 user_agent(缺省安全兜底)。"""
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": (request.headers.get("user-agent") or "")[:256] or None,
    }


@router.get("/bootstrap")
def bootstrap() -> dict:
    """登录页初始化:系统里是否已有用户(无 → 首个注册者自动成为管理员)。"""
    return {"has_users": sauth.has_any_user()}


@router.post("/register")
def register(req: RegisterIn, request: Request) -> dict:
    try:
        user, is_first = sauth.register_user(
            username=req.username, password=req.password, email=req.email,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # B5 审计:detail 只记脱敏字段,绝不落 password/email 明文
    audit(
        action="auth.register", user_id=user.user_id,
        resource_type="user", resource_id=user.user_id,
        detail={"username": user.username, "is_first_admin": bool(is_first),
                "role": user.role, "status": user.status},
        **_client_meta(request),
    )
    if is_first:
        return {"status": "approved", "role": "admin",
                "message": "注册成功:你是第一位用户,已自动成为管理员,现在可以登录。"}
    return {"status": "pending", "role": "user",
            "message": "注册申请已提交,请等待管理员审核通过后登录。"}


@router.post("/login")
def login(req: LoginIn, request: Request) -> dict:
    """登录(B2 失败限流:同 ip+用户名 10 分钟内错 5 次锁 15 分钟)。"""
    ip = request.client.host if request.client else "unknown"
    locked, remaining_seconds = ratelimit.is_locked(ip, req.username)
    if locked:
        raise HTTPException(
            429,
            f"登录失败次数过多,账号已临时锁定,请约 {max(1, remaining_seconds // 60)} 分钟后重试",
        )
    try:
        user, token = sauth.login_user(username=req.username, password=req.password)
    except ValueError as exc:
        ratelimit.register_failure(ip, req.username)
        # B5 审计:失败也留痕;reason 是服务端文案,不含口令明文
        audit(
            action="auth.login", success=False,
            detail={"username": req.username, "reason": str(exc)},
            **_client_meta(request),
        )
        raise HTTPException(403, str(exc))
    ratelimit.register_success(ip, req.username)
    audit(
        action="auth.login", user_id=user.user_id, success=True,
        resource_type="user", resource_id=user.user_id,
        detail={"username": user.username},
        **_client_meta(request),
    )
    return {"token": token, "user": sauth.user_public(user)}


@router.post("/logout")
def logout(request: Request) -> dict:
    sauth.revoke_token(request.headers.get("X-Auth-Token", ""))
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    return {"user": sauth.user_public(_require_user(request))}


@router.put("/llm-config")
def save_llm_config(req: LLMConfigIn, request: Request) -> dict:
    user = _require_user(request)
    try:
        kind = sauth.normalize_llm_kind(req.llm_kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    base_url = req.llm_base_url.strip()
    if base_url:
        # SSRF 防护:拒内网/环回/保留地址(apply_user_llm 会让服务端请求该地址)
        try:
            base_url = sauth.validate_external_url(base_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    with db_session.SessionLocal() as db:
        row = db.query(UserModel).filter_by(user_id=user.user_id).one_or_none()
        if row is None:
            raise HTTPException(404, "用户不存在")
        row.llm_kind = kind
        row.llm_base_url = base_url
        if req.llm_api_key.strip():
            row.llm_api_key = req.llm_api_key.strip()
        row.llm_model = req.llm_model.strip()
        db.commit()
        db.refresh(row)
        # B5 审计:detail 只记 kind/base_url/model,绝不落 api_key 明文
        audit(
            action="auth.llm_config.update", user_id=user.user_id,
            resource_type="user", resource_id=user.user_id,
            detail={
                "llm_kind": kind,
                "llm_base_url": base_url,
                "llm_model": req.llm_model.strip(),
                "api_key_changed": bool(req.llm_api_key.strip()),
            },
            **_client_meta(request),
        )
        return {"user": sauth.user_public(row),
                "message": "LLM 配置已保存,下次启动写作任务时生效。"}


@router.post("/llm-config/models")
def probe_models(req: ModelsProbeIn, request: Request) -> dict:
    """从用户自己的上游拉取可用模型列表(校验配置连通性,不落库)。"""
    _require_user(request)
    base_url = req.llm_base_url.strip()
    if not base_url:
        raise HTTPException(400, "请先填写 Base URL")
    try:
        base_url = sauth.validate_external_url(base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    api_key = req.llm_api_key.strip()
    if not api_key:
        with db_session.SessionLocal() as db:
            row = (
                db.query(UserModel)
                .filter_by(user_id=request.state.user.user_id)
                .one_or_none()
            )
            api_key = (row.llm_api_key if row else "") or ""
    if not api_key:
        raise HTTPException(400, "请填写 API Key(或先保存过配置后留空)")
    try:
        models = fetch_models_for((req.llm_kind or "openai").strip().lower(), base_url, api_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取模型列表失败:{exc}")
    return {"models": models}


# ───────────────────────── 管理员:用户审核 ─────────────────────────

def _set_status(user_id: str, status: str, *, admin: UserModel, request: Request) -> dict:
    with db_session.SessionLocal() as db:
        row = db.query(UserModel).filter_by(user_id=user_id).one_or_none()
        if row is None:
            raise HTTPException(404, "用户不存在")
        row.status = status
        row.reviewed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        # B5 审计:审核动作留痕(操作者 + 被审核者)
        audit(
            action=f"auth.admin.{status}", user_id=admin.user_id,
            resource_type="user", resource_id=user_id,
            detail={"target_username": row.username, "target_status": status},
            **_client_meta(request),
        )
        return {"user": sauth.user_public(row)}


@router.get("/admin/users")
def admin_list_users(request: Request) -> dict:
    _require_admin(request)
    with db_session.SessionLocal() as db:
        rows = db.query(UserModel).order_by(UserModel.created_at.asc()).all()
        return {"users": [sauth.user_public(r) for r in rows]}


@router.post("/admin/users/{user_id}/approve")
def admin_approve(user_id: str, request: Request) -> dict:
    admin = _require_admin(request)
    return _set_status(user_id, "approved", admin=admin, request=request)


@router.post("/admin/users/{user_id}/reject")
def admin_reject(user_id: str, request: Request) -> dict:
    admin = _require_admin(request)
    return _set_status(user_id, "rejected", admin=admin, request=request)


@router.delete("/admin/users/{user_id}")
def admin_delete(user_id: str, request: Request) -> dict:
    admin = _require_admin(request)
    if admin.user_id == user_id:
        raise HTTPException(400, "不能删除自己")
    with db_session.SessionLocal() as db:
        row = db.query(UserModel).filter_by(user_id=user_id).one_or_none()
        if row is None:
            raise HTTPException(404, "用户不存在")
        if row.role == "admin":
            remaining = (
                db.query(UserModel)
                .filter(UserModel.role == "admin", UserModel.user_id != user_id)
                .count()
            )
            if remaining == 0:
                raise HTTPException(400, "不能删除最后一名管理员")
        target_username = row.username
        db.query(UserModel).filter_by(user_id=user_id).delete()
        db.commit()
    # B5 审计:删除留痕(行已删,用户名先固定)
    audit(
        action="auth.admin.delete", user_id=admin.user_id,
        resource_type="user", resource_id=user_id,
        detail={"target_username": target_username},
        **_client_meta(request),
    )
    return {"ok": True}
