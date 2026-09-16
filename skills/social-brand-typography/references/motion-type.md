# Motion typography for social brands

## First set the reading order

Motion has limited reading time. One entrance should establish one idea, then hold still long enough to read. Plan the message as a small sequence—hook, meaning/benefit, proof, action—not as all copy appearing simultaneously. Prefer clear time for reading over decorative movement.

## Shaping-safe motion

- Shape the exact phrase in its chosen font before animation.
- Move, fade, mask, scale, or recolor complete words/phrases or whole lines. Keep Myanmar syllables and attached marks intact; never animate Unicode codepoints or combining marks independently.
- Do not apply letter-spacing interpolation to a Myanmar run. Avoid perspective or 3D rotations that make diacritics unreadable.
- Animate only one main property at a time (e.g. position plus a subtle opacity fade). Keep the text's baseline and line spacing stable once it settles.
- Use low-distance movement and calm easing for trustworthy/helpful services. Save fast cuts for a campaign with an intentional high-energy voice, not as the default “tech” effect.
- Hold factual prices, quotas, eligibility, and terms; viewers must be able to read them without pausing.
- Keep logo artwork unmodified and provide a final clear brand/CTA hold. For AuriX, the canonical brand guide specifies at least 1.5 seconds for the end frame.

## Example 7-second structure

This is a starting rhythm to test, not a universal template:

| Time | Role | Behavior |
|---|---|---|
| 0.0–1.8 s | Hook | One short phrase enters as a complete unit, settles, and holds. |
| 1.8–3.4 s | Benefit | Reveal a single supporting phrase; use color or a small position change to connect it to the hook. |
| 3.4–5.4 s | Proof | Present one credible feature/fact with still, high-contrast type. |
| 5.4–7.0 s | Brand / next step | Resolve to approved logo and one CTA; avoid adding new claims. |

If readers cannot finish the text at normal speed, shorten the wording or extend the hold. Do not compensate with faster word-by-word animation. Test playback at phone size and with sound off; key information must remain understandable without audio.

## Export checks

- Verify the final font is embedded/available or convert text to outlines only in a controlled production copy; preserve an editable source with text and license notes.
- Scrub through every frame for clipped marks, reflow, fallback substitution, weight flicker, background collision, and thumbnail contrast.
- Inspect a still from the first readable frame, the proof frame, and the final CTA hold.
- Keep motion graphics within platform-safe crops and ensure no key word or logo sits at the edge.
