# AuriX dynamic Shadowsocks profiles

The VPN portal can issue one stable `ssconf://` profile URL per active account.
Set `AURIX_SSCONF_PUBLIC_BASE_URL` to the public HTTPS origin of the portal,
for example `https://vpn.example.com`. The authenticated endpoint
`GET /api/ssconf` returns the profile URL. The profile itself is fetched by the
Outline client at `GET /api/ssconf/<opaque-token>` and never requires Telegram
headers.

The profile token is stored only as a SHA-256 lookup hash and an encrypted
value. The served document is built from active, observed AuriX generations;
provider URLs are decrypted only while rendering the response. Suspended or
closed accounts return an Outline-compatible error document, and an unknown or
revoked token returns `404`.

## Multi-server behavior

The AuriX control plane may have several active Outline generations for an
account after a regional assignment or failover. The official Outline dynamic
key format currently consumes one tunnel object, not an array of server
objects. Therefore AuriX keeps the multi-server pool internally and selects
one eligible route on every profile refresh, preferring a fresh healthy
endpoint observation and then the newest generation. It does not claim seamless in-session
failover, latency racing, or client-side multi-server support.

This is deliberately different from changing `OUTLINE_API_URL` into a list:
each Outline server has independent key identity, metrics, and native quota
enforcement. AuriX's generation-level accounting remains authoritative across
route changes.

## Operational boundary

- `ssconf` is a delivery profile, not a new provider lifecycle protocol.
- It currently renders only static `ss://` Outline credentials into the
  single-object dynamic-key document.
- Xray/VLESS, Hysteria2, VMess, Trojan, and WireGuard continue to use their
  existing adapter/profile evidence gates; enabling a scheme in a browser or
  device allowlist does not make its server lifecycle production-ready.
- The profile endpoint is bearer-authenticated by its opaque URL. Use HTTPS,
  keep the portal origin stable, and rotate/revoke the profile in the database
  if the URL is exposed.

## Verification

Run the local contract checks:

```bash
python -m unittest test_ssconfig.py test_vpn_web_api.py test_migrations.py
```

These checks cover static key parsing, multi-generation selection, account
suspension, encrypted token persistence, and the authenticated/public HTTP
boundary. They do not prove Outline client behavior on a customer network or
capacity of any live VPN node.
