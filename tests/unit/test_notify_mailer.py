"""M5 发信（notify/mailer.py）：标准库 smtplib；465 用 SSL、587 用 STARTTLS；每次发送前重新读 [smtp]（ADR-0032）。
用假的 smtplib 模块，不连网络。"""

from __future__ import annotations

import smtplib

import pytest
from pydantic import SecretStr

from mooapp.config.models import SmtpSecrets
from mooapp.notify.mailer import Email, MailError, SmtpMailer

RCPT = ("me@example.com",)


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_login: Exception | None = None

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.ssl = False
        self.starttls_called = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, **kw):
        self.starttls_called = True

    def login(self, user, password):
        if FakeSMTP.fail_login is not None:
            raise FakeSMTP.fail_login
        self.logged_in = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


class FakeSMTPSSL(FakeSMTP):
    def __init__(self, host, port, timeout=None, **kw):
        super().__init__(host, port, timeout, **kw)
        self.ssl = True


class FakeModule:
    SMTP = FakeSMTP
    SMTP_SSL = FakeSMTPSSL
    SMTPException = smtplib.SMTPException
    SMTPAuthenticationError = smtplib.SMTPAuthenticationError


@pytest.fixture(autouse=True)
def reset():
    FakeSMTP.instances = []
    FakeSMTP.fail_login = None


def secrets(port=465, pw="right") -> SmtpSecrets:
    return SmtpSecrets(host="smtp.example.com", port=port, username="bot@example.com", app_password=SecretStr(pw))


def test_port_465_uses_ssl_and_logs_in():
    m = SmtpMailer(lambda: secrets(465), smtp_module=FakeModule)
    m.send(Email("fill", "成交 US.SPY", "正文"), RCPT)
    s = FakeSMTP.instances[-1]
    assert s.ssl and not s.starttls_called
    assert s.logged_in == ("bot@example.com", "right")
    msg = s.sent[0]
    assert msg["Subject"] == "[mooapp] 成交 US.SPY" and msg["From"] == "bot@example.com" and msg["To"] == "me@example.com"
    assert s.quit_called


def test_port_587_uses_starttls():
    SmtpMailer(lambda: secrets(587), smtp_module=FakeModule).send(Email("fill", "s", "b"), RCPT)
    s = FakeSMTP.instances[-1]
    assert not s.ssl and s.starttls_called


def test_auth_failure_raises_mail_error_without_password():
    FakeSMTP.fail_login = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(MailError) as ei:
        SmtpMailer(lambda: secrets(pw="wrong-pw"), smtp_module=FakeModule).send(Email("fill", "s", "b"), RCPT)
    assert "wrong-pw" not in str(ei.value)


def test_network_error_raises_mail_error():
    class Down(FakeModule):
        class SMTP_SSL(FakeSMTPSSL):
            def __init__(self, *a, **k):
                raise OSError("connection refused")

    with pytest.raises(MailError, match="connection refused"):
        SmtpMailer(lambda: secrets(), smtp_module=Down).send(Email("fill", "s", "b"), RCPT)


def test_secrets_loaded_on_every_send():
    """ADR-0032：改正密码后不用重启：每次发送前重新读 [smtp]。"""
    pw = {"v": "wrong"}
    m = SmtpMailer(lambda: secrets(pw=pw["v"]), smtp_module=FakeModule)
    m.send(Email("fill", "s", "b"), RCPT)
    pw["v"] = "right"
    m.send(Email("fill", "s", "b"), RCPT)
    assert [s.logged_in[1] for s in FakeSMTP.instances] == ["wrong", "right"]


def test_secrets_load_error_is_mail_error():
    def broken():
        raise ValueError("secrets.toml 解析失败")

    with pytest.raises(MailError, match="secrets"):
        SmtpMailer(broken, smtp_module=FakeModule).send(Email("fill", "s", "b"), RCPT)


def test_no_recipients_rejected():
    with pytest.raises(MailError, match="收件人"):
        SmtpMailer(lambda: secrets(), smtp_module=FakeModule).send(Email("fill", "s", "b"), ())
