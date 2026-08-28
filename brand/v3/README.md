# AuriX Facebook Cover V3

V3 is the recommended Facebook cover master for cross-device use.

## Why V3 exists

Some Android Facebook builds display the 1640 × 624 cover through a much narrower center-cropped viewport. They may also place page controls over the upper-right and the profile picture over the lower center. V2 placed meaningful content too far left and too low for that behavior.

## Protected composition

- Essential lockup: approximately `x=500–1120`, `y=158–302` on the 1640 × 624 master.
- Lower center remains empty for the profile-picture overlap.
- Upper-right contains only decorative artwork, so page controls may cover it safely.
- Left and right edges contain no essential copy and may be cropped.

## Files

- `facebook-cover-mobile-safe-v3.svg` — editable master.
- `exports/aurix-facebook-cover-mobile-safe-v3-1640x624.png` — upload-ready Facebook cover.

The V3 artwork was checked at full width and through a simulated 1030 × 624 center crop resized to 709 × 430, matching the narrow viewport behavior visible in the supplied Android screenshot.
