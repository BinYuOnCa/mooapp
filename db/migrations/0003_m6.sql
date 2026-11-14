-- M6b 规则引擎（M6b 计划）。search_path 已指向当前 schema。
-- 规则状态只由常驻进程经 Store 写入；存储过程不得写这些表（SPEC 第 4 节，tests/unit/test_db_routines.py）。

CREATE TABLE rules (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    symbol      text NOT NULL,
    spec_yaml   text NOT NULL,          -- 规则文件原文（恢复时重新加载、重新编译表达式）
    status      text NOT NULL CHECK (status IN ('PENDING', 'ARMED', 'TRIGGERED', 'CLOSED', 'VOID', 'ERROR', 'CANCELED')),
    entry_cid   text NOT NULL,          -- 入场单的 client_order_id
    state       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 状态机快照（strategy/machine.py to_state）
    actor       text NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL
);
-- ADR-0020：每个标的同时最多一条活动规则（数据库层面兜底）
CREATE UNIQUE INDEX rules_one_active_per_symbol ON rules (symbol) WHERE status IN ('PENDING', 'ARMED', 'TRIGGERED');

-- 订单 → 规则（入场单、卖单）
CREATE TABLE rule_orders (
    client_order_id text PRIMARY KEY,
    rule_id         text NOT NULL REFERENCES rules (id),
    role            text NOT NULL CHECK (role IN ('entry', 'sell')),
    created_at      timestamptz NOT NULL
);
CREATE INDEX rule_orders_rule_idx ON rule_orders (rule_id, created_at);

-- 只追加：规则的每一步（冻结、触发、卖出、拒单、结束、取消……）
CREATE TABLE rule_events (
    id       bigserial PRIMARY KEY,
    rule_id  text NOT NULL REFERENCES rules (id),
    ts       timestamptz NOT NULL,
    kind     text NOT NULL,
    message  text NOT NULL,
    details  jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX rule_events_rule_idx ON rule_events (rule_id, id);

CREATE TRIGGER rule_events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON rule_events
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();
REVOKE UPDATE, DELETE, TRUNCATE ON rule_events FROM PUBLIC;
