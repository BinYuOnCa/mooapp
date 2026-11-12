"""mooapp 阶段 0 冒烟测试：网关握手、协议支持、账户、模拟盘资金、SMTP。只读，不下单。"""
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import tomllib
from email.message import EmailMessage
from pathlib import Path

HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPEND_PORT", "11111"))
SECRETS = Path.home() / ".config" / "mooapp" / "secrets.toml"
BROKER_TOML = Path("config") / "broker.toml"
HANDSHAKE_TIMEOUT = 15
CURRENCY_OF = {"US": "USD", "CA": "CAD", "HK": "HKD"}
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []


def record(status, name, detail=""):
    results.append((status, name, detail))


def check(name, fn, on_error=FAIL):
    try:
        record(PASS, name, fn() or "")
    except Exception as exc:  # 冒烟测试：记下失败原因，继续检查下一项
        record(on_error, name, f"{type(exc).__name__}: {exc}")


def mask(acc_id):
    return "****" + str(acc_id)[-4:]


def load_secrets():
    if not SECRETS.exists():
        return {}
    return tomllib.loads(SECRETS.read_text())


def port_local_only():
    with socket.create_connection((HOST, PORT), timeout=3):
        pass
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    ).stdout
    rows = out.splitlines()[1:]
    exposed = [ln for ln in rows if "127.0.0.1:" not in ln and "[::1]:" not in ln]
    if exposed:
        raise RuntimeError("网关监听了非本机地址，请在设置里把监听地址改为 127.0.0.1")
    owner = rows[0].split()[0] if rows else "未知"
    return f"可连接，只监听本机（监听进程：{owner}）"


def handshake(ft, has_token):
    """异步连接并限时等待；SDK 的同步构造在鉴权失败时会无限重试，所以不用它。"""
    ctx = ft.OpenQuoteContext(host=HOST, port=PORT, is_async_connect=True)
    try:
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        while time.monotonic() < deadline:
            if ctx.status == ft.ContextStatus.READY:
                record(PASS, "握手与鉴权", "连接就绪，" + ("带 token" if has_token else "未带 token"))
                probe_quote_protocols(ft, ctx)
                return True
            time.sleep(0.5)
    finally:
        ctx.close()
    if has_token:
        hint = ("已带 token 仍被拒：核对 token 是否与客户端里显示的一致，"
                "运行 bash scripts/stage0_bootstrap.sh token 重新填写")
    else:
        hint = ("网关拒绝了连接（日志里的 Unauthorized）。常见原因：① 桌面客户端的应用连接要求连接 token，"
                "在该设置里找到后运行 bash scripts/stage0_bootstrap.sh token 填入；"
                "② 客户端弹出了授权确认，需要点允许；③ 客户端未登录")
    record(FAIL, "握手与鉴权", f"{HANDSHAKE_TIMEOUT} 秒内未完成握手。{hint}")
    return False


def call(fn, *args, **kwargs):
    """调用 SDK 接口，失败时抛出带网关原话的异常。"""
    from moomoo import RET_OK
    ret, data = fn(*args, **kwargs)
    if ret != RET_OK:
        raise RuntimeError(str(data))
    return data


def probe_quote_protocols(ft, ctx):
    """探测网关支持哪些行情类协议。失败记为 WARN：说明这个网关缺协议，不影响交易类检查。"""
    def global_state():
        d = call(ctx.get_global_state)
        return (f"server_ver={d.get('server_ver')}，行情已登录={d.get('qot_logined')}，"
                f"交易已登录={d.get('trd_logined')}")

    def snapshot():
        row = call(ctx.get_market_snapshot, ["US.SPY"]).iloc[0]
        return f"US.SPY last_price={row.get('last_price')}"

    check("全局状态（协议 1002）", global_state, on_error=WARN)
    check("行情快照（协议 3203）", snapshot, on_error=WARN)


def accounts(ft):
    """按市场列出账户；security_firm 用 SDK 默认值（自 10.4 起自动识别券商）。"""
    seen, sims, desc, errors = set(), [], [], {}
    for m in ("US", "CA", "HK"):
        market = getattr(ft.TrdMarket, m, None)
        if market is None:
            continue
        ctx = ft.OpenSecTradeContext(filter_trdmarket=market, host=HOST, port=PORT)
        try:
            ret, data = ctx.get_acc_list()
        finally:
            ctx.close()
        if ret != ft.RET_OK:
            errors[m] = str(data)
            continue
        for _, row in data.iterrows():
            acc_id = row["acc_id"]
            if acc_id in seen:
                continue
            seen.add(acc_id)
            firm = row["security_firm"] if "security_firm" in data.columns else "?"
            desc.append(f"{m} {row['trd_env']} {mask(acc_id)}（{firm}）")
            if str(row["trd_env"]) == "SIMULATE":
                sims.append((m, acc_id))
    if not desc:
        detail = "；".join(f"{m}: {msg}" for m, msg in errors.items()) or "没有返回任何账户"
        record(FAIL, "账户列表（协议 2001）", detail)
        return
    record(PASS, "账户列表（协议 2001）", "；".join(desc))
    if not sims:
        record(FAIL, "模拟账户", "没有 SIMULATE 账户，请在 moomoo App 里开启模拟交易")
        return
    m, acc_id = sims[0]
    check("模拟账户资金（协议 2101）", lambda: sim_funds(ft, m, acc_id))
    BROKER_TOML.parent.mkdir(exist_ok=True)
    BROKER_TOML.write_text(
        "# 由 scripts/stage0_smoke.py 自动生成，M1 直接使用\n"
        "# security_firm 不填：SDK 自动识别券商\n"
        f'[opend]\nhost = "{HOST}"\nport = {PORT}\nfilter_trdmarket = "{m}"\n'
        "# 模拟账户号不是密钥，可以提交；实盘账户号不写进仓库\n"
        f"simulate_acc_id = {int(acc_id)}\n")
    record(PASS, "交易上下文参数", f"filter_trdmarket={m}，simulate_acc_id={int(acc_id)} → {BROKER_TOML}")


def sim_funds(ft, m, acc_id):
    ctx = ft.OpenSecTradeContext(filter_trdmarket=getattr(ft.TrdMarket, m), host=HOST, port=PORT)
    kwargs = {"trd_env": ft.TrdEnv.SIMULATE, "acc_id": int(acc_id)}
    currency = getattr(ft.Currency, CURRENCY_OF.get(m, ""), None)
    if currency is not None:
        kwargs["currency"] = currency
    try:
        data = call(ctx.accinfo_query, **kwargs)
    finally:
        ctx.close()
    row = data.iloc[0]
    cols = [c for c in ("currency", "total_assets", "cash", "power") if c in data.columns]
    return "，".join(f"{c}={row[c]}" for c in cols)


def smtp_test(secrets):
    cfg = secrets.get("smtp")
    if not cfg:
        raise RuntimeError(f"{SECRETS} 里没有 [smtp] 段，请重跑 stage0_bootstrap.sh")
    host, port = cfg["host"], int(cfg["port"])
    msg = EmailMessage()
    msg["Subject"] = "[mooapp] 阶段 0 测试邮件"
    msg["From"] = cfg["username"]
    msg["To"] = cfg["username"]
    msg.set_content("收到这封邮件，说明 SMTP 配置可用。")
    # 显式给 EHLO 名称，避免 smtplib 连上之后再做本机名反查（macOS 上可能很慢）
    # Gmail 对家庭宽带可能延迟约 10 秒才发问候，所以超时放宽到 60 秒
    stage, t0 = "建立连接并等待问候", time.monotonic()
    try:
        # smtplib 默认不校验服务器证书，这里显式用校验证书和主机名的 TLS 设置
        tls = ssl.create_default_context()
        # 465 端口一连上就要 TLS 握手（SMTP_SSL）；587 先明文问候再升级（STARTTLS）
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, local_hostname="localhost", timeout=60, context=tls)
        else:
            server = smtplib.SMTP(host, port, local_hostname="localhost", timeout=60)
        with server:
            if port != 465:
                stage = "STARTTLS 升级加密"
                server.starttls(context=tls)
            stage = "登录"
            # 只用 AUTH PLAIN。smtplib 的 login() 在第一种方式被拒后会接着试下一种；
            # 服务器若拒绝后立即断开，真正的拒绝原因会被「连接断开」盖住。
            server.ehlo_or_helo_if_needed()
            server.user, server.password = cfg["username"], cfg["app_password"]
            server.auth("PLAIN", server.auth_plain)
            stage = "发送"
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        reply = exc.smtp_error
        if isinstance(reply, bytes):
            reply = reply.decode(errors="replace")
        raise RuntimeError(
            f"{host}:{port} 登录被拒，服务器回复：{exc.smtp_code} {' '.join(str(reply).split())}") from exc
    except ssl.SSLCertVerificationError as exc:
        raise RuntimeError(
            f"{host}:{port} 服务器证书校验失败（{exc.verify_message}）。可能是网络里有设备在拦截加密连接，"
            "也可能是这个 Python 找不到系统根证书；把这一行贴给 Claude") from exc
    except (smtplib.SMTPServerDisconnected, ssl.SSLError, socket.gaierror,
            TimeoutError, ConnectionError) as exc:
        raise RuntimeError(
            f"{host}:{port} 在「{stage}」阶段断开，用时 {time.monotonic() - t0:.1f} 秒"
            f"（{type(exc).__name__}: {exc}）") from exc
    return f"测试邮件已经由 {host}:{port} 发到发件邮箱本身"


def main():
    secrets = load_secrets()
    token = secrets.get("opend", {}).get("token", "")
    import moomoo as ft
    if token:
        ft.SysConfig.set_token(token)

    check("网关端口与监听地址", port_local_only)
    try:
        ready = handshake(ft, bool(token))
    except Exception as exc:
        ready = False
        record(FAIL, "握手与鉴权", f"{type(exc).__name__}: {exc}")
    if ready:
        try:
            accounts(ft)
        except Exception as exc:
            record(FAIL, "账户列表（协议 2001）", f"{type(exc).__name__}: {exc}")
    check("SMTP 发信", lambda: smtp_test(secrets))

    print()
    for status, name, detail in results:
        print(f"    [{status}] {name}  {detail}")
    if any(s == WARN for s, _, _ in results):
        print("\n    WARN 不影响阶段 0 通过，但说明当前网关缺少这些协议，M1 起会用到。")
    sys.stdout.flush()
    # SDK 的后台线程可能不退出，直接结束进程
    os._exit(0 if results and all(s != FAIL for s, _, _ in results) else 1)


if __name__ == "__main__":
    main()
