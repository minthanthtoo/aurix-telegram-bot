# AuriX VPN product package

This package is the canonical implementation of the VPN product and its
control plane. It is separate from the AI gateway in `aurix_ai`.

```text
commerce*.py       paid commerce, plans, orders, subscriptions, jobs
entitlements.py    free/trial entitlements and quota policy
outline_adapter.py Outline Management API adapter
connectivity.py    endpoint registry and fleet operations
runtime.py         VPN service composition root
telegram_*.py      Telegram bot transport and product handlers
vpn_dashboard.py   customer VPN state projection
vpn_web_api.py     authenticated VPN portal entrypoint
```

Shared infrastructure remains at repository level (`migrations.py`,
`persistence.py`, `ports.py`, payment/receipt adapters, and Telegram signature
verification). Root modules with the old names are compatibility shims for
existing imports and deployment scripts.

Run the VPN runtime with:

```sh
python -m aurix_vpn
```
