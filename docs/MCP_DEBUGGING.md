# MCP debugging workflow

This repository has a checked-in MCP configuration template for the Supabase
project and the Render account. The template contains server URLs only; OAuth
tokens must remain in Codex's credential store. Read-only diagnostics can run
without an extra prompt, while write-capable operations require approval.
The Supabase entry targets the active Singapore project; if the project is
recreated or renamed, verify it with `get_project_url` and regenerate the
project URL instead of reusing an older regional reference.

## One-time authentication

From this repository, authenticate each server in Codex:

```text
codex mcp login supabase
codex mcp login render
```

If the CLI cannot load the global Codex configuration, use Codex desktop
Settings → MCP servers and add the URLs from
[`codex-mcp.toml.example`](../codex-mcp.toml.example). The current CLI reports
an incompatible global `[agents]` setting, so this is a configuration repair,
not an application failure.

Verify the connection in the Codex composer with:

```text
/mcp
```

Both servers should be listed as enabled and authenticated. Keep write tools on
prompt/approval until the intended target and scope are confirmed.

## Incident workflow

1. Start with the application latency log. Set `AURIX_LATENCY_LOG=1` in the
   Render service and reproduce one request. Capture `telegram_request`,
   `outline_request`, `commerce_*`, and `postgres_*` timings, but never paste
   tokens, SQL payloads, or customer data into a chat.
2. Use Render MCP to inspect the service deployment, instance state, recent
   deploys, and application logs around the request timestamp. Check for free
   tier spin-down/restarts, deploy churn, memory pressure, and region mismatch.
3. Use Supabase MCP to inspect database health, active connections, slow-query
   evidence, and project region. Check that `COMMERCE_DATABASE_URL` points to
   the Singapore project/pooler and that the pooler mode matches the workload.
4. Correlate timestamps before changing code or infrastructure. A slow
   `telegram_request` with fast database/Outline spans usually indicates
   Telegram/network or instance wake-up latency; a slow `postgres_*` span points
   to pooler, query, or connection pressure.
5. Make one change at a time, redeploy, then repeat the same probe. Record the
   before/after p50 and p95 latency and the exact Render/Supabase evidence.

## Current application safeguards

- Telegram polling and housekeeping run independently; a slow quota or expiry
  pass cannot stop the active long poll.
- PostgreSQL uses a small lazy connection pool instead of opening a new TLS
  connection for every operation.
- Free and paid quota checks reuse one Outline metrics snapshot per maintenance
  pass.
- Optional timing logs are bounded and redacted by design. Disable them after
  diagnosis with `AURIX_LATENCY_LOG=0`.
