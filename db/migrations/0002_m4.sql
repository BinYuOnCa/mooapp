-- M4 风控闸门与 Kill switch（M4 计划设计要点）。search_path 已指向当前 schema。

-- ADR-0026：确认之前收到的撤单，确认后自动发出
ALTER TABLE orders ADD COLUMN cancel_pending boolean NOT NULL DEFAULT false;

-- 只追加：每次闸门判定（通过与拒绝）、halt、resume 都写一行（手册 M4：每次拒单写审计日志）
CREATE TABLE audit (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL,
    action          text NOT NULL,        -- place / cancel / halt / resume
    source          text NOT NULL,        -- cli / rule / agent / system
    actor           text NOT NULL,
    client_order_id text,
    symbol          text,
    side            text,
    qty             integer,
    price           numeric,
    allowed         boolean NOT NULL,
    rule            text,                 -- 命中的规则名；通过时为空
    reason          text NOT NULL,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_ts_idx ON audit (ts);

CREATE TRIGGER audit_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();
REVOKE UPDATE, DELETE, TRUNCATE ON audit FROM PUBLIC;

-- SPEC 3.4：命令行只写命令，常驻进程按顺序取出交给闸门，把判定写回同一行
CREATE TABLE commands (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL,
    kind        text NOT NULL,            -- place / cancel / halt / resume（其他值由常驻进程判为 error）
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    actor       text NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'error')),
    result      jsonb,
    claimed_at  timestamptz,
    finished_at timestamptz
);
CREATE INDEX commands_pending_idx ON commands (id) WHERE status = 'pending';

-- 新命令唤醒常驻进程（LISTEN mooapp_commands；另有 1 秒轮询兜底）
CREATE FUNCTION commands_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('mooapp_commands', NEW.id::text);
    RETURN NEW;
END;
$$;
CREATE TRIGGER commands_notify_insert AFTER INSERT ON commands FOR EACH ROW EXECUTE FUNCTION commands_notify();
