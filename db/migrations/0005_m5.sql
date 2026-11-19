-- M5 邮件通知（PROGRESS「M5 计划」决定 2）。search_path 已指向当前 schema。
-- 编号 0005：避开 M7 分支的 0004_m7.sql。任何进程都可以写入待发邮件（mooapp eod 直接发送失败时也写这里），常驻进程投递并退避重试。
-- 密码不进这张表（ADR-0032）。

CREATE TABLE outbox (
    id              bigserial PRIMARY KEY,
    created_at      timestamptz NOT NULL,
    kind            text NOT NULL,          -- fill / risk_rejected / halt / alert / digest / heartbeat / eod
    subject         text NOT NULL,
    body            text NOT NULL,
    recipients      jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    last_error      text,
    sent_at         timestamptz
);
CREATE INDEX outbox_due_idx ON outbox (next_attempt_at) WHERE status = 'pending';
