"""发信（M5）：标准库 smtplib，不加依赖。465 用 SSL、其余端口用 STARTTLS。

每次发送前重新读 secrets 的 [smtp]（ADR-0032：改正密码不用重启）。密码不写日志、不出现在异常信息里。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

from mooapp.common import MooappError
from mooapp.config.models import SmtpSecrets

log = logging.getLogger(__name__)
SUBJECT_PREFIX = "[mooapp] "


@dataclass(frozen=True)
class Email:
    kind: str  # fill / risk_rejected / halt / alert / digest / heartbeat / eod
    subject: str
    body: str


class MailError(MooappError):
    """发信失败（读不到凭据、连不上、认证失败……）。调用方负责重试，不影响交易（SPEC 5.7）。"""


class Mailer(Protocol):
    def send(self, email: Email, recipients: Sequence[str]) -> None: ...


class SmtpMailer:
    def __init__(self, load_smtp: Callable[[], SmtpSecrets], timeout: float = 15.0, smtp_module: Any = smtplib):
        self._load = load_smtp
        self._timeout = timeout
        self._smtp = smtp_module

    def send(self, email: Email, recipients: Sequence[str]) -> None:
        if not recipients:
            raise MailError("没有收件人（settings.toml 的 [notify.email] recipients）")
        try:
            cfg = self._load()
        except Exception as e:  # 转成 MailError 交给重试；不吞（ADR-0032）
            raise MailError(f"读不到 secrets 的 [smtp]：{type(e).__name__}：{e}") from e
        msg = EmailMessage()
        msg["Subject"] = SUBJECT_PREFIX + email.subject
        msg["From"] = cfg.username
        msg["To"] = ", ".join(recipients)
        msg.set_content(email.body)
        mod = self._smtp
        try:
            if cfg.port == 465:
                conn = mod.SMTP_SSL(cfg.host, cfg.port, timeout=self._timeout, context=ssl.create_default_context())
            else:
                conn = mod.SMTP(cfg.host, cfg.port, timeout=self._timeout)
                conn.starttls(context=ssl.create_default_context())
            try:
                conn.login(cfg.username, cfg.app_password.get_secret_value())
                conn.send_message(msg, from_addr=cfg.username, to_addrs=list(recipients))
            finally:
                try:
                    conn.quit()
                except (OSError, mod.SMTPException) as e:
                    log.warning("断开 SMTP 连接时出错（邮件已处理）：%s", e)
        except (OSError, mod.SMTPException) as e:
            raise MailError(f"{type(e).__name__}：{e}") from e
