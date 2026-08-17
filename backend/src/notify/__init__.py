"""通知/邮件框架(框架先行,未配置 SMTP 授权码时静默跳过)。"""
from .mailer import send_alert_email, send_report_email, smtp_configured

__all__ = ["send_alert_email", "send_report_email", "smtp_configured", "resolve_recipients"]


def resolve_recipients(db, usage: str) -> list[str]:
    """按用途解析启用的收件人邮箱列表(all 通配 report/alert/api 任意用途)。"""
    from sqlalchemy import select

    from db.models import NotifyContactModel

    allowed = {"all", usage}
    stmt = select(NotifyContactModel).where(
        NotifyContactModel.enabled.is_(True),
        NotifyContactModel.usage.in_(allowed),
    )
    return [c.email for c in db.execute(stmt).scalars().all()]
