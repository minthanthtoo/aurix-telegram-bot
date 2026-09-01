# Refactor Phase 6: Telegram Transport Boundary

Phase 6 removes Telegram presentation and privileged-command routing from the
application composition module.

## Delivered

- `telegram_transport.py` owns polling, message rendering, pagination,
  callbacks, command menus, maintenance scheduling, and Telegram HTTP calls.
- `AdminOperations` remains the allowlist boundary for privileged commerce and
  free-entitlement operations; transport buttons cannot confer permission.
- `app.TelegramBot` and `app.AdminOperations` remain identity-compatible
  exports for the existing bot entrypoint and tests.
- The transport imports domain services and typed ports directly; no extracted
  module depends on `app`, preventing a reverse application dependency.

## Invariant

This is an extraction only. Customer/admin command surface, callback formats,
pagination behavior, confirmation TTL, maintenance cadence, and polling
semantics are unchanged.
