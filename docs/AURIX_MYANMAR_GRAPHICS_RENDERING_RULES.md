# AuriX Myanmar Graphics Rendering Rules

## Why Latin-style spacing fails

Myanmar text is shaped as syllable clusters. Kinzi and vowel/tone signs may sit above the base; other vowels and medials sit below it; mark-to-mark positioning can stack them further. The visible glyph therefore extends beyond a Latin-style cap-height/descender box. Baseline values that look numerically adequate can still produce cramped upper and lower margins.

Primary references:

- Microsoft OpenType Myanmar shaping specification: <https://learn.microsoft.com/en-us/typography/script-development/myanmar>
- Unicode Myanmar FAQ: <https://www.unicode.org/faq/myanmar.html>
- Unicode Burmese orthography notes: <https://r12a.github.io/scripts/mymr/my.html>
- Noto Myanmar font source and QA proofs: <https://github.com/notofonts/myanmar>

## Locked production rules

The references establish why Myanmar needs script-aware shaping and more vertical room. The numeric values below are conservative AuriX production floors derived from those metrics and verified against the installed Noto renderer; they are not values quoted by Unicode or Microsoft.

1. Use `Noto Sans Myanmar` first, `Myanmar Sangam MN` only as fallback. Do not let a generic Latin sans choose the Myanmar glyphs.
2. Add `xml:lang="my"` to SVG Myanmar text and leave OpenType `mark` and `mkmk` positioning enabled.
3. Treat every mixed Latin/Myanmar line as a Myanmar line for vertical metrics.
4. Consecutive Myanmar baselines use these minimum multipliers:
   - display: `1.55 × font size`
   - headline: `1.50 × font size`
   - body: `1.48 × font size`
   - small/supporting: `1.42 × font size`
5. Reserve at least `0.32 em` above and `0.36 em` below each Myanmar text block before placing logos, rules, icons, or another content region.
6. Do not center Myanmar text inside a box using Latin cap height. Align from a tested baseline or use a rendered bounding box.
7. Do not reduce leading to solve overflow. Shorten the copy, reduce the number of lines, or enlarge the region.
8. Keep Burmese headline weights at a real installed weight. Avoid synthetic bolding and outline strokes around combining marks.
9. Validate at 1080 × 1350 and at a 324 × 405 feed thumbnail. Reject any line whose top/bottom marks visually touch another line even if SVG boxes do not overlap.
10. Keep exact social copy in deterministic SVG. Image generation must not render Burmese lettering.

## Implementation

`assets/social/tools/myanmar_svg_typography.py` provides:

- a fixed Myanmar-aware font stack;
- Myanmar-run detection;
- SVG language and shaping attributes;
- role-based safe baseline calculations;
- block-clearance calculations.

All new AuriX social renderers should use this module instead of defining their own raw `<text>` helper.
