"""邮件发送模块。

设计原则:
- 框架先行:未配置 SMTP_HOST/PORT/USER/PASS 或 NOTIFY_ENABLED=false 时静默跳过,仅记 info 日志;
- 发送失败不抛异常,仅记 warning,绝不中断主流程(检索/综述等核心任务);
- 收件人由调用方显式传入(从 notify_contacts 表按 usage 查询后传入)。

SMTP 配置(读 .env):
    SMTP_HOST=smtp.163.com
    SMTP_PORT=465            # 163 邮箱 SSL
    SMTP_USER=xx@163.com
    SMTP_PASS=授权码(留空=不发信)
    NOTIFY_ENABLED=true
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    """SMTP 是否配置完整(SMTP_PASS 非空 且 NOTIFY_ENABLED=true)。"""
    if os.getenv("NOTIFY_ENABLED", "true").strip().lower() == "false":
        return False
    return bool((os.getenv("SMTP_PASS") or "").strip())


def _send(subject: str, body: str, recipients: list[str]) -> bool:
    """核心发送逻辑;未配置或失败均返回 False,不抛异常。"""
    if not smtp_configured():
        log.info("[mailer] SMTP 授权码未配置,跳过发送(subject=%r)", subject)
        return False
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465") or "465")
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    if not host or not user:
        log.info("[mailer] SMTP_HOST/USER 未配置,跳过发送(subject=%r)", subject)
        return False
    valid = [r for r in (recipients or []) if r and "@" in r]
    if not valid:
        log.warning("[mailer] 无有效收件人,跳过发送(subject=%r)", subject)
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("文献综述助手", "utf-8")), user))
    msg["To"] = ", ".join(valid)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
                server.sendmail(user, valid, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(user, valid, msg.as_string())
        log.info("[mailer] 发送成功 subject=%r -> %s", subject, valid)
        return True
    except Exception as exc:  # noqa: BLE001 - 发送失败不中断主流程
        log.warning("[mailer] 发送失败 subject=%r err=%s", subject, exc)
        return False


def send_report_email(subject: str, body: str, recipients: list[str]) -> bool:
    """发送关键报告邮件(如任务完成总结)。"""
    return _send(subject, body, recipients)


def send_alert_email(subject: str, body: str, recipients: list[str]) -> bool:
    """发送告警邮件(如任务失败)。"""
    return _send(subject, body, recipients)
