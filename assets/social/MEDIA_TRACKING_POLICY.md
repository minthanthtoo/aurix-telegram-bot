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

This policy reduces the media present in deployment checkouts from 325.5 MiB to 9.5 MiB. Historical Git objects remain in local repository history unless an explicit history rewrite is performed.
