# Refactor Phase 8: Telegram Feature Modules

Phase 8 decomposes the remaining Telegram transport monolith into cohesive
feature mixins while preserving the exact `TelegramBot` type and behavior.

## Delivered

- `telegram_admin.py` owns the privileged-operation authorization boundary.
- `telegram_admin_panels.py` owns paginated admin views, state fingerprints,
  confirmation challenges, and order detail/action presentation.
- `telegram_callbacks.py` owns callback-query routing.
- `telegram_commands.py` owns customer and administrator message-command
  routing.
- `telegram_maintenance.py` owns notification outbox delivery, termination
  notices, maintenance heartbeat state, maintenance passes, and shutdown.
- `telegram_transport.py` retains Telegram HTTP primitives, keyboards, receipt
  ingestion, customer status/usage presentation, lifecycle construction, and
  long polling.

Every module is below 800 lines. `TelegramBot` composes the four behavior
mixins and remains identity-compatible through `telegram_transport` and
`app`, so no deployment command, callback format, or consumer import changes.

## Guardrails

Boundary tests pin the owning mixin for command, callback, maintenance, and
admin-panel methods. Feature modules depend on service interfaces through the
composed bot instance and never import `app` or runtime composition.
