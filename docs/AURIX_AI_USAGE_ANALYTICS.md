# AuriX AI usage analytics

This document describes the privacy-safe aggregate usage endpoint and the admin
dashboard backed by it. It is for AuriX operators and future maintainers; it is
not an external partner API.

## What is measured

Every usage-ledger row contributes to the selected range and can be grouped by:

- overall AuriX traffic;
- partner site/account;
- API key (only the safe label and token prefix are returned);
- model/provider;
- API endpoint; and
- UTC calendar day.

Each aggregate contains:

| Field | Meaning |
| --- | --- |
| `requests` | All recorded requests, regardless of outcome. |
| `successful_requests` | Requests whose ledger status is `completed`. |
| `failed_requests` | Requests whose ledger status is `failed`. |
| `usage_reported_requests` | Requests with at least one provider token field. |
| `input_tokens` | Sum of provider-reported input/prompt tokens. |
| `output_tokens` | Sum of provider-reported output/completion tokens. |
| `total_tokens` | Sum of provider-reported total tokens. |
| `cached_tokens` | Sum of provider-reported cached tokens when available. |
| `cost` | Provider/router-reported estimated cost, not a billing invoice. |
| `success_rate` | `successful_requests / requests * 100`, or `null` when there are no requests. |
| `last_used_at` | Latest ledger timestamp in the group, or `null`. |

Missing provider usage remains unknown. It is never converted into a fabricated
zero request cost or token count; the numeric aggregate is zero while
`usage_reported_requests` shows how much of the traffic had token data.

## Admin endpoint

```http
GET /api/admin/usage?format=analytics&from=2026-09-01T00:00:00Z&to=2026-10-01T00:00:00Z
```

The normal AuriX admin authentication rules apply: a valid browser Telegram
session or the configured operator bearer token is required. The endpoint does
not return prompts, responses, raw usage payloads, full API keys, or provider
credentials.

Optional filters are the same as the activity report:

```text
account_id, key_id, model_id, endpoint, status, user_id
```

`status` accepts `completed` or `failed`. `from` is inclusive and `to` is
exclusive. Timestamps must be ISO-8601 values with a timezone; the server
normalizes them to UTC.

## Response shape

```json
{
  "period": {"start_at": "...", "end_at": "..."},
  "summary": {"requests": 0, "successful_requests": 0, "failed_requests": 0},
  "accounts": [],
  "keys": [],
  "models": [],
  "endpoints": [],
  "daily": [],
  "filters": {}
}
```

`summary` is the overall aggregate. The other arrays contain the same metric
fields plus their grouping identity. Accounts and keys are included even when
their selected-period usage is zero, which keeps the inventory useful for
finding inactive or newly issued credentials.

## Dashboard behavior

The admin console's **Analytics** section provides:

- Today, 24-hour, 7-day, 30-day, 60-day, and current-month ranges;
- total, successful, failed, input, output, total, cached, and estimated-cost
  cards;
- an input/output token trend, switchable to successful/failed request trend;
- breakdowns by site, API key, model, and endpoint; and
- last-used timestamps without exposing message content.

The existing **Activity** section remains the prompt-free request ledger for
individual request IDs and pagination. Analytics is intentionally separate so
large dashboards do not require loading every event.

## Implementation notes

The aggregates are calculated directly from `api_usage`, the same ledger used
by the existing account report and 9Router-compatible export. There is no
second counter to drift and no schema migration for this feature. The
`APIKeyStore.usage_breakdown()` method is the single source for the dashboard's
overall, grouped, and daily numbers.

