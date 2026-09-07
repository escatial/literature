"""用户 + 密码 + RBAC(方案 §9 生产化:RBAC)。

设计要点:
- 密码哈希:开发环境用 hashlib.pbkdf2_hmac(标准库),无第三方依赖;
- 角色三档:user(普通)、reviewer(审稿)、admin(管理员);
- 简易权限检查函数 has_role(user, required_role);
- 生产环境应替换为 bcrypt / argon2,本骨架用 PBKDF2 已可避免明文存储。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid

from db.models import UserModel
from db.session import SessionLocal

log = logging.getLogger(__name__)

# 角色枚举(从低到高)
ROLE_USER = "user"
ROLE_REVIEWER = "reviewer"
ROLE_ADMIN = "admin"
ROLE_HIERARCHY = {ROLE_USER: 0, ROLE_REVIEWER: 1, ROLE_ADMIN: 2}

# PBKDF2 迭代次数(开发环境 100k,生产环境建议 >= 600k)
_PBKDF2_ITER = int(os.getenv("PBKDF2_ITER", "100000"))
_PBKDF2_SALT_LEN = 16


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 哈希。"""
    salt = secrets.token_bytes(_PBKDF2_SALT_LEN)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码。"""
    try:
        algo, iter_s, salt_hex, digest_hex = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iter_n = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iter_n)
    return hmac.compare_digest(actual, expected)


def create_user(
    *,
    username: str,
    email: str,
    password: str,
    role: str = ROLE_USER,
    tenant_id: str | None = None,
) -> UserModel:
    """新建用户(幂等 upsert by email)。"""
    with SessionLocal() as db:
        row = db.query(UserModel).filter_by(email=email).one_or_none()
        if row is not None:
            log.info("user %s 已存在,跳过创建", email)
            return row
        row = UserModel(
            user_id=f"usr_{uuid.uuid4().hex[:12]}",
            username=username,
            email=email,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
            enabled=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def authenticate(*, email: str, password: str) -> UserModel | None:
    """登录校验。"""
    with SessionLocal() as db:
        row = db.query(UserModel).filter_by(email=email).one_or_none()
        if row is None or not row.enabled:
            return None
        if not verify_password(password, row.password_hash):
            return None
        return row


def has_role(user: UserModel | None, required: str) -> bool:
    """判断用户角色是否 >= required。"""
    if user is None:
        return False
    user_rank = ROLE_HIERARCHY.get(user.role, -1)
    req_rank = ROLE_HIERARCHY.get(required, 99)
    return user_rank >= req_rank


__all__ = [
    "create_user",
    "authenticate",
    "has_role",
    "hash_password",
    "verify_password",
    "ROLE_USER",
    "ROLE_REVIEWER",
    "ROLE_ADMIN",
]