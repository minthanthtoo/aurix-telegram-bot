# AuriX Telegram staff roles

The bot keeps one customer VPN and commerce service while giving staff separate, permission checked workspaces.

## Configure deployment identities

Set these server environment values:

```dotenv
ADMIN_TELEGRAM_IDS=123456789,234567890
OWNER_TELEGRAM_IDS=123456789
SELLER_TELEGRAM_IDS=
AURIX_STAFF_AUTHORIZATION_MODE=roles
```

`ADMIN_TELEGRAM_IDS` remains the existing list of operational administrators. Every listed ID keeps the existing admin operations unless an Owner disables an individual Admin capability. An ID listed in `OWNER_TELEGRAM_IDS` becomes an Owner even when it is also in `ADMIN_TELEGRAM_IDS`. The Owner list must name the Owner explicitly; the bot never promotes the first Admin by position. Seller IDs can be added in the Owner workspace after startup or supplied as a comma-separated `SELLER_TELEGRAM_IDS` list.

Role mode applies a versioned `staff_access` schema migration when the bot starts. Existing customer, order, payment, receipt, subscription, wallet, and VPN credential records are not rewritten. If role mode is enabled, startup requires at least one active Owner in environment or durable staff policy. If no role setting is supplied, the existing Admin allowlist behavior stays active.

Remove `OWNER_TELEGRAM_IDS` only after another durable Owner is granted. Deployment-configured Owner, Admin, and Seller identities must be changed in environment before the Owner UI will let them be demoted or revoked. Keep the Owner in `ADMIN_TELEGRAM_IDS` when existing scripts or external dashboards rely on the old Admin list.

## Workspaces and boundaries

- **VPN user:** Home, purchase, VPN access, order history, wallet, and help remain customer activities. Back returns to the parent screen; Home returns to the activity root. Retained wallet and payment records remain visible through their existing paths.
- **Seller:** sees assigned customer cases and a read-only order stage summary. A case is checked against the current assignment each time it opens. Sellers do not see global queues, payment references, receipts, wallet history, or VPN keys.
- **Admin:** keeps existing sales, receipt, recovery, fleet, promotion, and account operations. Admins cannot grant roles or customer assignments. Owner capability overrides can limit an Admin by operation category.
- **Owner:** keeps Admin operations and can grant/revoke database-backed staff roles, set Admin capability overrides, and assign/reassign customer cases. Role and scope changes require an inline confirmation and create an audit event. The last active Owner cannot be removed or demoted.

All privileged operations recheck the current role and capability at execution. Legacy commands and callback buttons still pass through the same AdminOperations boundary. The Seller case view only calls customer-bounded order reads after rechecking assignment; it never runs AdminOperations.

## Owner screen

Open **Home → Staff workspace → Staff roles & customer cases**. Role and assignment forms accept numeric Telegram IDs and show a review screen before confirmation. Admin capability names are `sales.read`, `sales.decide`, `recovery.manage`, `fleet.read`, `fleet.manage`, `accounts.read`, `accounts.manage`, `promotions.manage`, and `wallet.read`. Effects are `allow`, `deny`, or `inherit`; overrides apply only to Admins. Sellers cannot receive global queue or wallet capabilities.

The Owner screen lists staff and active case assignments. Reassigning an active customer case to another Seller atomically expires the previous assignment, so the previous Seller loses access on their next action.
