-- M7 收盘 P&L 与对账（PROGRESS「M7 计划」决定 4）。search_path 已指向当前 schema。
-- 只由 `mooapp eod` 经 Store 写入；存储过程不得写（SPEC 第 4 节）。同一交易日可以多次运行，只追加。

CREATE TABLE eod_reports (
    id           bigserial PRIMARY KEY,
    trade_date   date NOT NULL,
    generated_at timestamptz NOT NULL,
    env          text NOT NULL CHECK (env IN ('SIMULATE', 'REAL')),
    ok           boolean NOT NULL,                     -- 无 [BREAK]
    summary      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 合计、收盘价来源、备注
    breaks       jsonb NOT NULL DEFAULT '[]'::jsonb,
    rows         jsonb NOT NULL DEFAULT '[]'::jsonb    -- 逐标的 P&L（金额为报告口径的字符串，ROUND_HALF_UP 到 0.01）
);
CREATE INDEX eod_reports_date_idx ON eod_reports (trade_date, id);

CREATE TRIGGER eod_reports_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eod_reports
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();
