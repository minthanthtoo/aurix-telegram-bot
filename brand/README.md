# AuriX Brand Assets

Before using any mark, follow [`AURIX_LOGO_USAGE_PROTOCOL.md`](AURIX_LOGO_USAGE_PROTOCOL.md). It defines canonical asset selection, co-brand hierarchy, responsive behavior, and rejection tests.

These SVG files are editable master concepts for the AuriX brand system. V2 is the recommended working direction; the root-level V1 files are retained only as design history.

## Recommended V2 files

- `v2/aurix-logo-mark-v2.svg` — profile image, avatar, favicon source, and compact mark.
- `v2/aurix-profile-facebook-clean.svg` — full-bleed Facebook profile avatar; avoids a white halo in circular cropping.
- `v2/aurix-logo-horizontal-v2.svg` — horizontal logo for light backgrounds.
- `v2/aurix-logo-horizontal-reverse-v2.svg` — horizontal logo for dark backgrounds.
- `v2/facebook-cover-template-v2.svg` — 1640 × 624 durable Facebook page cover.
- `v3/facebook-cover-mobile-safe-v3.svg` — recommended Facebook cover for narrow Android center crops and profile overlap.
- `v3/aurix-telegram-bot-avatar.svg` — circular-safe Telegram bot avatar optimized for chat-list recognition.
- `v4/aurix-telegram-bot-avatar-v4.svg` — bot avatar with a segmented digital signal ring.
- `v4/aurix-telegram-group-avatar-v4.svg` — admin/community group avatar with connected nodes.
- `v4/aurix-telegram-channel-avatar-v4.svg` — public channel avatar with broadcast waves.

### Ready-to-use PNG exports

- `v2/exports/aurix-profile-v2-1080.png`
- `v2/exports/aurix-horizontal-v2-1600.png`
- `v2/exports/aurix-horizontal-reverse-v2-1600.png`
- `v2/exports/aurix-facebook-cover-v2-1640x624.png`
- `v4/exports/aurix-telegram-bot-avatar-v4-1024.png`
- `v4/exports/aurix-telegram-group-avatar-v4-1024.png`
- `v4/exports/aurix-telegram-channel-avatar-v4-1024.png`

## Superseded V1 files

- `aurix-logo-mark.svg` — profile image, avatar, favicon, and compact mark.
- `aurix-logo-horizontal.svg` — horizontal logo for light backgrounds.
- `aurix-logo-horizontal-reverse.svg` — horizontal logo for dark backgrounds.
- `facebook-cover-template.svg` — 1640 × 624 Facebook cover working canvas.

## Before public release

1. Approve the concept and wordmark.
2. Complete name/trademark/handle clearance.
3. Install Inter and Noto Sans Myanmar in the design environment.
4. Convert the final wordmark text to vector outlines.
5. Configure Facebook's native action button with the tested Telegram destination.
6. Preview Facebook desktop and mobile crops.
7. Export PNG derivatives from the approved V2 SVG masters.

## Suggested exports

```sh
rsvg-convert -w 1080 -h 1080 -o aurix-profile-1080.png v2/aurix-logo-mark-v2.svg
rsvg-convert -w 1640 -h 624 -o aurix-facebook-cover-1640x624.png v2/facebook-cover-template-v2.svg
```

Keep the SVG masters as the source of truth. Do not repeatedly edit compressed PNG exports.
