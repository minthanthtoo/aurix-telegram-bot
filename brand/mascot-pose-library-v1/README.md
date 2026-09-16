# AuriX / Auri core pose library

2026-09-16. Eight static, transparent 3D-style PNGs. Proposed reusable library;
not a new approved logo, editable 3D model, or rigged animation asset.

Open `index.html` to browse and download, or use the ZIP. All eight PNGs are
1122 × 1402 RGBA, with genuine alpha and transparent corners. Preserve alpha
when importing into Canva, Figma, Photoshop, slides or a compositor.

## Design judgment

Original hero: appearance 8/10, reusable system 5/10 (subjective design judgment).
Its distinctive palette and readable crest worked, but one smile/presenter pose
was applied to every message, regardless of meaning. Opaque white canvas limited
background choice. Repeated mascot placement became decoration rather than help.

This pack adds gaze direction, body-weight shifts, attention, listening, reflection,
gratitude and celebration. Recognition is stronger than a pack of unrelated cute
expressions. Static reuse is substantially improved; animation consistency remains
unproven because these were generated individually, not rendered from one rig.

## Pose selection

| File suffix | Use | Placement / warning |
|---|---|---|
| 01-neutral | Default introductions, quiet identity | Use when no gesture is needed |
| 02-present-left | Explain a benefit or offer | Character right, content left |
| 03-present-right | Explain a benefit or offer | Character left, content right |
| 04-inspect | Quota check, consideration, FAQ | Pair gaze with a real information element; not payment success |
| 05-reminder | Gentle low-quota notice | Friendly heads-up, not emergency or fraud warning |
| 06-listening | Setup help and support invitation | No implied 24/7 human presence |
| 07-thank-you | Appreciation, welcome-back | Bow reads gratitude better than transaction completion |
| 08-celebrate | Giveaway, launch, milestone | Only positive occasions; not errors or quota exhaustion |

For the four recent sample posts: quota → inspect/reminder; receipt workflow →
present-right paired with explicit process copy; setup help → listening;
daily free → present-left, or celebration only for a genuine promotional occasion.
Keep payment confirmation as verified text plus an appropriate status icon rather
than expecting a character expression to certify a financial event.

## Identity and layout contract

Keep navy body, cyan cheek mask with dark throat, amber swept single crest,
brown eyes, cyan wing-leading shapes and restrained violet feathers. The character
has two wings and two feet; never add human fingers, chest logos or costumes by default.
Use the exact approved AuriX logo separately. No Outline/Telegram logos on its body.

One mascot per ordinary post. Start with 25–40% of the image area and let the
message lead; stickers may be character-first. Point gaze/gesture inward toward
copy. Preserve aspect ratio and at least 8% external safe space around the visible
silhouette. Some originals have less internal padding than requested: add layout
space rather than assuming equal transparent margins. Align by visible feet/body,
not identical file edges. Never stretch or clip crest/wingtips to force alignment.

Do not mechanically mirror images: light direction and characteristic crest change.
Choose the supplied left/right pair. Use light/pastel backgrounds for easiest navy
body separation; dark backgrounds need clear contrast, not arbitrary bright outlines.
Keep shadows separate and subtle; these assets have no baked-in floor.

At about 48px, fine expression distinctions disappear. Use the approved brand
mark for tiny navigation/avatar identity, not full-body mascot. This pack does not
replace approved Bot/Group/Channel avatars.

## Review and iteration record

Plan compared prop-led poses, exaggerated reaction stickers, and restrained
role-based poses. Chose role-based poses: usable across VPN and AI, no device/UI
dependency, and less childish. Improvements over initial plan: create two genuine
presenting directions, require alpha, avoid fingers, separate gratitude from success.

Every output was inspected during generation for silhouette, gesture, palette,
complete limbs, crop and identity. Reminder first attempt returned a service
moderation error; an identical bounded retry succeeded. No fallback model used.
Review reclassified requested success pose as thank-you because closed eyes and
bow communicate gratitude more clearly. It is not labeled verified-success.

Remaining weaknesses: small variations in torso width, crest length, eye size and
feather count; thinking wing can read as finger-like feathers at small sizes;
listening pose may read shy as well as helpful; celebration is noticeably more cute.
Keep these as static assets; do not interpolate them into animation frames. A truly
production-rigged family requires a locked turnaround, mesh/materials and shared rig.

Built-in image generation created each PNG from the same original 3D hero reference:
`brand/mascot-v2-3d/20260916_aurix-auri-3d-hero-r2.png`.
Exact common prompt and per-pose deltas are in `PROMPTS.json`; the original prompt
ID `07-success` corresponds to final `07-thank-you` after review.

## Packaging and local history

`package.py` performs read-only pixel/alpha validation, creates SVG contact-sheet
layouts, renders light/dark previews and writes the ZIP. It does not retouch cutouts.
Raw PNGs are preserved unchanged. Keep generated media local and Git-ignored;
record only reusable prompts, documentation, metadata and packaging source.
No publishing or GitHub push is part of this task.

Alpha caveat: automated checks found faint low-alpha generation residue beyond
the visible character, reaching some canvas edges. The visible silhouette at
alpha >32 is contained on all eight, and corners are transparent. This is not a
perfectly cleaned professional matte; test on the actual final background. Files
were not silently thresholded or retouched. Manifest records both raw and visible
alpha bounds so future agents do not mistake this check for perfect edge cleanup.

Final sheet review: the light-background sheet keeps all eight silhouettes readable.
On Midnight/Deep Space, dark feet and outer feathers merge into the backdrop; use
a lighter brand-tinted placement area instead of treating dark placement as approved
by default. The faint alpha residue is not conspicuous at contact-sheet scale, but
that is not proof of a clean matte at every size. Suggested starting core: present-left,
present-right, inspect, listening and reminder. Gratitude/celebration are occasional
supporting assets. Palette recognition is consistent; exact pose-to-pose anatomy is not.
