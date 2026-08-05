"""邮件发送：配置了 OMM_SMTP_HOST 时走真实 SMTP，否则由调用方走开发模式。"""

from __future__ import annotations

import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from .config import Settings

logger = logging.getLogger("omm.email")


def smtp_configured(settings: Settings) -> bool:
    return bool(settings.smtp_host)


def send_email(settings: Settings, to_address: str, subject: str, body: str) -> None:
    """同步发送纯文本邮件；失败抛出原始异常由调用方转译为业务错误。"""
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = settings.smtp_from
    message["To"] = to_address

    if settings.smtp_starttls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            client.starttls()
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password)
            client.sendmail(settings.smtp_from, [to_address], message.as_string())
    else:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password)
            client.sendmail(settings.smtp_from, [to_address], message.as_string())
    logger.info("verification email sent to %s", to_address)
