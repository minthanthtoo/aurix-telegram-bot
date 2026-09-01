# Social and Brand Media Tracking Policy

The application deployment must not carry the complete social-production archive.

## Tracked

- Canonical AuriX V2 logo masters.
- Mobile-safe V3 Facebook cover master and platform export.
- Approved V4 Telegram Bot, Group, and Channel avatars.
- Official Outline and accepted-payment recognition assets used by renderers.
- One approved PNG for each major active campaign.
- Self-contained editable SVG only for the current receipt and quota-control standards, plus the lightweight setup-rescue source.
- Captions, manifests, strategy, review notes, and renderer code because these are small text assets.

## Local-only

- Iteration renders.
- Thumbnail previews.
- Generated plates.
- Superseded exports.
- Duplicate campaign-source logos and plates.
- Experimental or superseded brand-avatar versions.

`git rm --cached` is used to stop tracking these assets without deleting their local copies. `.gitignore` contains the canonical allowlist. When a new final replaces an existing one, update the allowlist in the same commit: add the new final and remove the superseded final from Git tracking.

This policy reduces the media present in deployment checkouts from 325.5 MiB to 9.5 MiB.

## Historical rewrite record

On 2026-09-01, every media extension was removed from all existing commits with `git filter-repo`. The 29 canonical files were then restored in one new tip commit. Reflogs were expired and unreachable objects were pruned with aggressive garbage collection.

- Active `.git` directory: 9.0 MiB.
- Active packed objects: 8.79 MiB.
- Media paths reachable anywhere in active history: 29, matching the current allowlist.
- Pre-rewrite recovery repository: `/Users/min/projects/tg-AuriX-bot-pre-media-rewrite-20260901-202449.git`.

Because commit IDs changed, any future remote publication of this rewritten branch requires an explicit force-push decision. No remote was configured or updated during the rewrite.
