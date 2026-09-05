# Managed Outline-key repair runbook

This runbook covers the failure mode where AuriX still has an active free,
trial, promo, or paid entitlement but the corresponding key is absent from the
server's successful Outline inventory. It is intentionally separate from
untracked/orphan cleanup: an orphan is never adopted, and a missing managed key
is never replaced from one failed API call.

## Safety contract

- Identity is always `(server_id, outline_key_id)`.
- A successful inventory must observe the key missing twice; a transient or
  unreachable endpoint is not evidence of deletion.
- Usage must come from a fresh transfer-metrics observation or a persisted
  sample younger than the bounded repair window. Unknown or stale usage is
  escalated to `manual`; no automatic quota reset is allowed.
- A key at or above its configured limit is not repaired. Quota enforcement
  owns that case and hard-deletes it.
- The worker is the only component allowed to call the Outline Management API.
  Telegram callbacks and admin panels create durable decisions only.
- Every create/recreate, local cutover, old binding, usage value, and customer
  notice is audited. Access URLs are encrypted at rest and never shown in
  admin inventory panels.
- Customer VPN views join repair state by `(server_id, local_key_ref)`. When a
  managed key has disappeared, `/myvpn` and the focused paid-key view show
  **key recovery in progress** or **key recovery needs review** instead of
  presenting a misleading active key. The message confirms that quota was not
  reset and that no replacement was issued while usage is untrusted.

## Lifecycle

```text
present → missing observation 1 → missing observation 2
       → pending repair → leased worker
       → done (replacement + remaining quota) or failed/manual
```

The job is unique per `(server_id, kind, local_key_ref)`. Repeated maintenance
runs update observation timestamps without opening duplicate jobs or inflating
the audit log. A later, distinct disappearance can reopen the same durable row.

## Automatic worker decision

For each due job the worker rechecks, in order:

1. the local entitlement is still active and unexpired;
2. the old key ID is still the entitlement's source identity;
3. the old key is not already present (read-after-ambiguous recovery);
4. fresh usage, or a recent cached usage sample, is available and below quota;
5. the endpoint is healthy and admits the operation;
6. a deterministic replacement can be created or recovered.

The replacement allowance is `original_quota - observed_usage`, never the
original quota. The local key row, encrypted URL, registry binding, ledger
metadata, and repair job transition are committed together. If any remote
effect is ambiguous, the worker first reads the deterministic key ID/name and
only then retries; it does not create a second key blindly.

## Owner review

Admins open **Admin → 🧩 Key Repairs** (or `/repairs`). Each item is paginated
and refreshes in place. Selecting one shows server, customer suffix, old key
ID, planned human-readable name, expiry, usage/quota, attempts, and the exact
reason without exposing a secret key.

Only the owner can confirm a manual/failed item. The normal button preserves
freshly observed usage. When usage is unavailable, a second, visibly warned
**full-quota override** is offered; it requires a separate one-time Telegram
confirmation and records `owner_approved_unknown_usage`. This is the only path
that can restore a full allowance without a fresh source metric.

Every newly opened repair episode also creates one deduplicated staff alert for
each active owner/admin whose **Missing-key repairs** notification is enabled.
The alert contains only the repair ID, customer suffix/ID, endpoint, old key
ID, usage evidence state, and decision state; it never contains an access URL.
The **🧩 Open Key Repairs** button opens the in-place review queue. Repeated
inventory polls do not spam staff, and each staff member can toggle this alert
type from `/notifications` without affecting customer or quota-enforcement
messages.

`/reconcile` reports `managed_key_missing`, pending, failed, and manual counts.
The maintenance worker processes bounded leased jobs automatically; a manual
or failed decision leaves the customer's existing database quota unchanged.

## Environment controls

```text
AURIX_KEY_REPAIR_REQUIRED_OBSERVATIONS=2
AURIX_KEY_REPAIR_OBSERVATION_INTERVAL_SECONDS=60
AURIX_KEY_REPAIR_MAX_ATTEMPTS=8
AURIX_KEY_REPAIR_ALLOW_UNKNOWN_USAGE=0
AURIX_KEY_REPAIR_ALLOW_STALE_USAGE=0
AURIX_KEY_REPAIR_CACHED_USAGE_MAX_AGE_SECONDS=900
```

Keep the defaults for production. The cached window is deliberately short and
only applies to a key whose ID was explicitly present in a prior metrics
response; a missing map entry does not refresh it. Enabling unknown/stale usage
is an explicit owner risk decision and should be temporary, documented, and
followed by a manual receiving-account/traffic review.

## Incident checklist

1. Check `/capacity` and `/reconcile`; confirm the endpoint is reachable and
   the key is truly `missing`, not merely an unreachable inventory.
2. Open `/repairs`, inspect the job and its observation times.
3. For `pending`, allow the maintenance worker to process it; do not create a
   manual key in Outline.
4. For `manual`, obtain fresh endpoint metrics or use the owner override only
   after confirming the old key cannot still be used.
5. After completion, verify the new key appears in `/myvpn`, the old tuple is
   revoked locally, the remote inventory has exactly the replacement, and the
   audit/notification entries exist.
6. If the endpoint remains unreliable, keep it blocked for new admission and
   resolve health/capacity before approving more repairs.
