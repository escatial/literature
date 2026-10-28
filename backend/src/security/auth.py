"""登录态令牌 + 注册审核 + 按用户 LLM 配置应用。

- 令牌:secrets 随机 32 字节,库(auth_tokens)只存 SHA-256 哈希;
  前端通过 X-Auth-Token 头携带,main.py 的鉴权中间件统一校验。
- 注册:库中无任何用户时,首个注册者直接成为管理员(approved);
  之后注册一律 pending,由管理员在用户管理页审核。
- LLM 配置:用户自定义 base_url/api_key/model 后,写作任务启动时
  应用为 llm.client 的运行时覆盖(C1 起为 ContextVar 上下文级覆盖:
  SSE worker 线程内 set、finally reset,按请求隔离;apply_user_llm
  返回 reset 句柄由调用方负责恢复)。未配置的用户在任务启动时
  显式清除覆盖,回落到系统 .env。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from db.models import AuthTokenModel, UserModel
# 经模块属性访问 SessionLocal:reset_engine(测试)会重绑 db.session 的全局,
# `from ... import SessionLocal` 的对象绑定会停留在旧 engine 上导致写穿。
from db import session as db_session
from security.rbac import hash_password, verify_password

# B1(2026-10-27):登录令牌 TTL(秒),默认 7 天;env AUTH_TOKEN_TTL_SECONDS 可覆盖。
AUTH_TOKEN_TTL_SECONDS = int(os.environ.get("AUTH_TOKEN_TTL_SECONDS") or 7 * 86400)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{2,32}$")
# 接口类型 = 协议维度(不是厂商):OpenAI 兼容(chat completions)/
# Anthropic 兼容(Messages);历史 "minimax" 值由 llm.client 归一化为 openai
_VALID_KINDS = ("openai", "anthropic")


def normalize_llm_kind(kind: str | None) -> str:
    """归一化并校验 LLM 接口协议类型;非法值抛 ValueError(前端直显)。"""
    normalized = (kind or "openai").strip().lower()
    if normalized not in _VALID_KINDS:
        raise ValueError(
            f"不支持的接口类型: {normalized},可用: {', '.join(_VALID_KINDS)}"
        )
    return normalized


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc_now() -> datetime:
    """SQLite DATETIME 列无时区,读回 naive 值;expires_at 的读写与比较
    必须统一用 naive UTC —— 拿 aware 的 _now() 与库内读回值比较会 TypeError。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(user: UserModel) -> str:
    """签发新令牌并落库(哈希),返回原文。过期时间 = now + TTL。"""
    raw = secrets.token_hex(32)
    with db_session.SessionLocal() as db:
        db.add(AuthTokenModel(
            token_hash=_hash_token(raw),
            user_id=user.user_id,
            expires_at=_naive_utc_now() + timedelta(seconds=AUTH_TOKEN_TTL_SECONDS),
        ))
        db.commit()
    return raw


def revoke_token(raw: str) -> None:
    if not raw:
        return
    with db_session.SessionLocal() as db:
        db.query(AuthTokenModel).filter_by(token_hash=_hash_token(raw)).delete()
        db.commit()


def user_from_token(raw: str | None) -> UserModel | None:
    """按令牌取用户;令牌无效/用户不可用/已过期返回 None。

    B1(2026-10-27) 过期三段语义(均为同会话内原子处理):
    1. expires_at 为 NULL(存量令牌)→ 首次校验回填 now+TTL,不踢存量会话;
    2. 已过期 → 删行并返回 None(死行不残留);
    3. 剩余 < TTL/2 → 滑动顺延一个整 TTL(减半写放大:半程内的每次请求
       都不回写,只有跨过半程的第一次请求写库)。
    """
    if not raw:
        return None
    with db_session.SessionLocal() as db:
        row = (
            db.query(AuthTokenModel)
            .filter_by(token_hash=_hash_token(raw.strip()))
            .one_or_none()
        )
        if row is None:
            return None
        now = _naive_utc_now()
        need_write = False
        if row.expires_at is None:
            # 段1:存量令牌回填(行为兼容:不拒绝,只补过期时间)
            row.expires_at = now + timedelta(seconds=AUTH_TOKEN_TTL_SECONDS)
            need_write = True
        elif row.expires_at <= now:
            # 段2:过期 → 删行拒绝
            db.delete(row)
            db.commit()
            return None
        elif (row.expires_at - now).total_seconds() < AUTH_TOKEN_TTL_SECONDS / 2:
            # 段3:滑动续期
            row.expires_at = now + timedelta(seconds=AUTH_TOKEN_TTL_SECONDS)
            need_write = True
        user = db.query(UserModel).filter_by(user_id=row.user_id).one_or_none()
        if user is None or not user.enabled:
            return None
        if need_write:
            db.commit()
            # commit 会 expire 全部对象;脱离会话(expunge)前重载标量,
            # 否则调用方读 user_id 触发 DetachedInstanceError
            db.refresh(user)
        # 脱离会话后仍要读字段,先固定标量
        db.expunge(user)
        return user


def has_any_user() -> bool:
    with db_session.SessionLocal() as db:
        return db.query(UserModel.user_id).first() is not None


def ensure_default_admin(*, username: str = "admin", password: str = "admin") -> bool:
    """系统初始化:用户表为空时预置默认管理员(admin/admin,已审核)。

    仅空库时创建一次,已有任何用户(含改名/禁用)绝不覆盖或改密。
    返回是否执行了创建。不走注册校验(admin/5 位密码是用户点名的本地测试账号)。
    """
    if has_any_user():
        return False
    with db_session.SessionLocal() as db:
        db.add(UserModel(
            user_id=f"usr_{uuid.uuid4().hex[:12]}",
            username=username,
            email=f"{username}@local.dev",
            password_hash=hash_password(password),
            role="admin",
            status="approved",
            reviewed_at=_now(),
            enabled=True,
        ))
        db.commit()
    return True


def register_user(*, username: str, password: str, email: str | None = None) -> tuple[UserModel, bool]:
    """注册新用户。返回 (user, is_first_admin)。

    首个用户自动成为管理员并通过审核;后续用户 status=pending 等待审核。
    """
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名须为 2-32 位中文/字母/数字/下划线")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    email = (email or "").strip() or f"{username}@local.dev"
    first = not has_any_user()
    with db_session.SessionLocal() as db:
        if db.query(UserModel).filter_by(username=username).one_or_none():
            raise ValueError("用户名已存在")
        if db.query(UserModel).filter_by(email=email).one_or_none():
            raise ValueError("该邮箱已被占用,请更换用户名或填写其他邮箱")
        row = UserModel(
            user_id=f"usr_{uuid.uuid4().hex[:12]}",
            username=username,
            email=email,
            password_hash=hash_password(password),
            role="admin" if first else "user",
            status="approved" if first else "pending",
            reviewed_at=_now() if first else None,
            enabled=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, first


def login_user(*, username: str, password: str) -> tuple[UserModel, str]:
    """登录校验,通过则签发令牌。状态不合格抛 ValueError(前端直显)。"""
    username = (username or "").strip()
    with db_session.SessionLocal() as db:
        row = (
            db.query(UserModel)
            .filter((UserModel.username == username) | (UserModel.email == username))
            .one_or_none()
        )
        if row is None or not verify_password(password or "", row.password_hash):
            raise ValueError("用户名或密码错误")
        if not row.enabled:
            raise ValueError("账号已被禁用")
        if row.status == "pending":
            raise ValueError("账号待管理员审核,请稍后再试")
        if row.status == "rejected":
            raise ValueError("注册申请已被拒绝")
        db.expunge(row)
    token = issue_token(row)
    return row, token


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def validate_external_url(url: str) -> str:
    """用户自定义接口 URL 的 SSRF 防护:仅 http/https,host 不得指向
    本机/环回/私有/保留地址。返回归一化 URL,不合格抛 ValueError。

    字面 IP 直接判段;域名解析成功按解析结果判,解析失败(NXDOMAIN 等)
    放行——真实请求时自然报出 DNS 错误,不在保存阶段替 DNS 下判词。
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket

    def _bad_ip(ip_text: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        return (ip.is_loopback or ip.is_private or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified)

    raw = (url or "").strip()
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Base URL 必须是 http(s)://主机 形式")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("Base URL 不允许指向本机/内网主机名")
    try:
        ipaddress.ip_address(host)
        resolved = [host]  # 字面 IP,无需 DNS
    except ValueError:
        try:
            resolved = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            return raw  # 域名暂不可解析:放行,连接期报真实错误
    for ip_text in resolved:
        if _bad_ip(ip_text):
            raise ValueError("Base URL 不允许指向内网/保留地址")
    return raw


def user_public(user: UserModel) -> dict:
    """脱敏序列化(绝不包含密码哈希与完整 API key)。"""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "llm_kind": user.llm_kind or "openai",
        "llm_base_url": user.llm_base_url or "",
        "llm_api_key_masked": mask_key(user.llm_api_key or ""),
        "llm_model": user.llm_model or "",
    }


def apply_user_llm(user: UserModel | None):
    """把用户的 LLM 配置应用为运行时覆盖;未配置则清除(回落系统 .env)。

    C1:返回 reset 句柄 —— 调用方(请求作用域)在 finally 中调用以恢复
    上下文,防线程池复用泄漏。SSE worker 线程内 bind() 在首行调用、
    finally 调 reset(线程一次性,天然按请求隔离)。
    """
    from llm import client as llm_client

    if user is not None and (user.llm_base_url or "").strip() and (user.llm_api_key or "").strip():
        token = llm_client.set_runtime_llm(
            kind=(user.llm_kind or "openai").strip().lower(),
            base_url=user.llm_base_url.strip(),
            api_key=user.llm_api_key.strip(),
            model=(user.llm_model or "").strip(),
        )

        def _reset() -> None:
            try:
                llm_client.reset_runtime_llm(token)
            except Exception:  # noqa: BLE001  reset 只能在原上下文调用;跨上下文兜底清除
                llm_client.clear_runtime_llm()

        return _reset
    llm_client.clear_runtime_llm()
    return lambda: None
