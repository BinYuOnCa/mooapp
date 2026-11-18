-- M8 Trading Agent 层（PROGRESS「M8 计划」）。search_path 已指向当前 schema。
-- 编号 0006：避开 M7 的 0004、M5 的 0005。approvals 由 MCP server 写入提议（SPEC 5.9），状态由常驻进程更新；
-- agent_calls 只追加（每次工具调用的输入、输出、时间、调用方）。

CREATE TABLE approvals (
    id          text PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('order', 'rule')),
    payload     jsonb NOT NULL,
    reason      text NOT NULL,          -- Agent 给的理由：外部文本，只当数据
    caller      text NOT NULL,
    status      text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    decided_at  timestamptz,
    result      jsonb
);
CREATE INDEX approvals_status_idx ON approvals (status, created_at);

CREATE TABLE agent_calls (
    id      bigserial PRIMARY KEY,
    tool    text NOT NULL,
    args    jsonb NOT NULL,
    result  jsonb NOT NULL,
    caller  text NOT NULL,
    ok      boolean NOT NULL,
    ts      timestamptz NOT NULL
);
CREATE TRIGGER agent_calls_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON agent_calls
    FOR EACH STATEMENT EXECUTE FUNCTION oms_append_only();
