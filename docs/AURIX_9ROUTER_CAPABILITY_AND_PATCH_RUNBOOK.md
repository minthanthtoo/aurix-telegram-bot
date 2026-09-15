# AuriX / 9Router capability and patch recovery runbook

Status: implemented locally; production account discovery and 9Router source
recovery are not complete until the authenticated Singapore router checkout is
available.

## What this adds

AuriX now has two safe operator tools:

- `GET /api/admin/capabilities` performs a live, authenticated 9Router
  discovery for chat, embeddings, speech-to-text, text-to-speech, image, and
  video categories, plus quota and today's usage when the router exposes them.
- `scripts/aurix_9router_capability_probe.py` produces the same report from a
  shell without exposing the router key, prompts, or response bodies.
- `scripts/aurix_9router_patch_guard.py` snapshots a separate 9Router source
  checkout into a working-tree patch, contribution patch, Git bundle, and
  safe untracked-file archive. It can verify and restore that snapshot.

Discovery and health are separate. A model can appear in a 9Router catalog and
still fail because the connected account lacks entitlement, has exhausted
quota, or the provider rejects the requested feature.

## Capability discovery

Run this on the AI host or another machine that can reach the private router:

```sh
python scripts/aurix_9router_capability_probe.py \
  --output /secure/aurix-capability-$(date -u +%Y%m%dT%H%M%SZ).json
```

The script uses `AURIX_AI_ROUTER_BASE_URL`, `AURIX_AI_ROUTER_API_KEY`, and
`AURIX_AI_MODEL`. It defaults to discovery only. A small paid or quota-consuming
probe is explicit:

```sh
python scripts/aurix_9router_capability_probe.py \
  --probe-chat ag/gemini-3.7-flash-high
```

The report records model IDs, category status, capability labels, quota data,
usage data, and sanitized errors. It does not store bearer keys or prompt and
response content. Keep the JSON report private because quota and model
availability are operational data.

The browser administrator can read the same report from:

```text
GET /api/admin/capabilities
```

This route requires the existing Telegram administrator session or
`AURIX_AI_ADMIN_TOKEN`.

## 9Router source preservation

Keep the 9Router source in a separate checkout. This AuriX repository only
contains the integration and must not become a copy of the 9Router application.

The separate checkout should have:

```text
origin   your private fork or mirror
upstream official 9Router repository
```

Capture a baseline before updating:

```sh
python scripts/aurix_9router_patch_guard.py inventory \
  --repo /opt/src/9router

python scripts/aurix_9router_patch_guard.py snapshot \
  --repo /opt/src/9router \
  --output /secure/9router-snapshots/$(date -u +%Y%m%dT%H%M%SZ) \
  --base upstream/main
```

The snapshot contains:

- `manifest.json` with the commit, branch, remotes, dirty state, and patch
  inventory;
- `source.bundle` containing all Git refs;
- `working-tree.patch` containing tracked uncommitted changes;
- `commits/local-contributions.patch` when a base ref is supplied;
- `untracked/` for recoverable non-secret files;
- `untracked-files.json` listing copied and skipped files.

Sensitive-looking files are skipped or rejected. Back up credentials through
the deployment secret manager, not through this source snapshot.

Verify the backup before an update:

```sh
python scripts/aurix_9router_patch_guard.py verify \
  --snapshot /secure/9router-snapshots/20260916T000000Z
```

Restore into a clean checkout after a dry check:

```sh
python scripts/aurix_9router_patch_guard.py restore \
  --snapshot /secure/9router-snapshots/20260916T000000Z \
  --repo /opt/src/9router

python scripts/aurix_9router_patch_guard.py restore \
  --snapshot /secure/9router-snapshots/20260916T000000Z \
  --repo /opt/src/9router \
  --apply
```

The apply form refuses a dirty target checkout and refuses to overwrite an
existing untracked destination.

## Upstream update procedure

1. Create a snapshot and verify it.
2. Tag the currently deployed source and image digest.
3. Fetch the chosen upstream release into a new candidate branch.
4. Replay local contribution commits in dependency order.
5. Run 9Router tests, AuriX integration tests, security checks, and the
   capability report.
6. Build a versioned canary image.
7. Confirm chat streaming, tools, vision, Lisu text, STT, TTS, images, video,
   quota reporting, and usage accounting.
8. Deploy only the candidate image and retain the previous image for rollback.

When upstream accepts a contribution, compare the upstream commit with the
local patch using `git range-diff`, then mark the local patch as superseded.
When upstream rejects or ignores it, keep the contribution branch and replay it
on every supported release. Never delete the original patch just because a PR
was closed.

## Release identity

Record a release as:

```text
9router-v<upstream-version>+aurix.<patch-sequence>
```

Store the source commit, patch manifest, image digest, configuration checksum,
database migration result, capability report, and rollback image together.
An image rollback without a compatible database recovery procedure is not a
complete rollback.
