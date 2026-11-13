-- M3 OMS 核心（SPEC 5.4；M3 计划第 3 节）。
-- 由 `mooapp db migrate` 在当前环境的 schema 里、以该 schema 的属主角色执行；search_path 已指向该 schema，
-- 所以这里不写 schema 名。金额列用 NUMERIC，时间列用 timestamptz（UTC，ADR-0011）。
-- OMS 拥有的表只能由常驻进程经 Store 写入；存储过程不得写（SPEC 第 4 节，tests/unit/test_db_routines.py）。

CREATE TABLE orders (
    client_order_id   text PRIMARY KEY CHECK (client_order_id ~ '^[0-9a-f]{32}$'),
    broker_order_id   text UNIQUE,
    source            text NOT NULL,
    symbol            text NOT NULL,
    side              text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    kind              text NOT NULL CHECK (kind IN ('LIMIT', 'MARKET')),
    qty               integer NOT NULL CHECK (qty > 0),
    limit_price       numeric,
    cl_ord_id         text NOT NULL,
    status            text NOT NULL,
    base_status       text,
    cum_qty           integer NOT NULL DEFAULT 0,
    avg_fill_price    numeric NOT NULL DEFAULT 0,
    pending_qty       integer,
    pending_price     numeric,
    pending_cl_ord_id text,
    orig_cl_ord_ids   text[] NOT NULL DEFAULT '{}',
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    reason            text,
    raw_status        text,       -- 最近一次处理过的 Moomoo 原始状态（映射不了的也原样保存）
    raw_dealt_qty     integer,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    CHECK (cum_qty >= 0 AND cum_qty <= qty)
);
CREATE INDEX orders_open_idx ON orders (status) WHERE status NOT IN ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED');

-- 只追加：每个事件记录来源 origin（push / poll / local）与结果 outcome
CREATE TABLE order_events (
    id              bigserial PRIMARY KEY,
    client_order_id text NOT NULL REFERENCES orders (client_order_id),
    event_type      text NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('push', 'poll', 'local')),
    outcome         text NOT NULL CHECK (outcome IN ('applied', 'ignored', 'rejected', 'unmapped')),
    from_status     text,
    to_status       text,
    exec_id         text,
    qty             integer,
    price           numeric,
    raw_status      text,
    reason          text,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts              timestamptz NOT NULL
);
CREATE INDEX order_events_order_idx ON order_events (client_order_id, id);

-- 按成交 id 去重：实盘 deal_id；模拟盘 "order_id:累计成交量"
CREATE TABLE fills (
    fill_id         text PRIMARY KEY,
    client_order_id text NOT NULL REFERENCES orders (client_order_id),
    broker_order_id text NOT NULL,
    symbol          text NOT NULL,
    side            text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty             integer NOT NULL CHECK (qty > 0),
    price           numeric NOT NULL,
    ts              timestamptz NOT NULL,
    synthetic       boolean NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('push', 'poll', 'local'))
);
CREATE INDEX fills_order_idx ON fills (client_order_id);

-- 只追加：启动对齐与运行中发现的差异、外部订单告警（client_order_id 可能不在 orders 里，不加外键）
CREATE TABLE recon_events (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL,
    client_order_id text,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts              timestamptz NOT NULL
);

CREATE TABLE kv (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ADR-0025：journal 回放按 entry_id 幂等（与写操作同一事务提交）
CREATE TABLE journal_applied (
    entry_id   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- 只追加表：语句级触发器，UPDATE / DELETE / TRUNCATE 一律报错（属主也拦得住；表为空时同样生效）
CREATE FUNCTION oms_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% 只追加，禁止 %', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TRIGGER order_events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON order_events
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();
CREATE TRIGGER recon_events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON recon_events
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();

REVOKE UPDATE, DELETE, TRUNCATE ON order_events, recon_events FROM PUBLIC;
