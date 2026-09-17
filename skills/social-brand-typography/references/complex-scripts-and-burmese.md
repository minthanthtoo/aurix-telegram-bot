# Complex scripts and Burmese rendering

## Discover before typesetting

Identify all writing systems in the brief. Confirm font coverage for every codepoint, actual shaping support, language tags, fallback behavior, and mixed-script metrics. Do not use a font's name or a successful installation as evidence that it has the right glyphs or shaping for the intended language. Test actual copy with vowels, medials, kinzi, tone marks, stacked forms, numerals, punctuation, Latin terms, and spaces where they occur.

## Myanmar / Burmese requirements

Myanmar shaping treats sequences as syllable clusters, may reorder signs, applies substitutions, and positions base and combining marks. Microsoft documents the shaping stages and the required Myanmar GSUB/GPOS behavior, including `mark` and `mkmk`: <https://learn.microsoft.com/en-us/typography/script-development/myanmar>.

- Prefer an explicitly named, tested Myanmar family. Do not leave Burmese glyph selection to generic `sans-serif` fallback.
- For SVG, set `xml:lang="my"`, preserve OpenType shaping, and provide a real font/weight. Use `font-kerning: normal`; do not disable mark positioning.
- Test actual output in its renderer: the design app, SVG rasterizer, browser, or motion package may differ.
- Use a shaped word/phrase as the smallest design and animation unit. Do not insert tracking between codepoints or move a tone mark independently.
- Do not outline/embolden the glyph contours synthetically to “make it pop.” Increase the real font weight, type size, contrast, or text region instead.
- Treat a line containing any Myanmar codepoint as Myanmar for baseline planning, even if Latin/digits dominate its character count.
- Align by rendered baselines and visible blocks, not Latin cap height. Keep short English mixed with Burmese on one optical line only after checking real output.
- If a line overflows, shorten it, break at a natural phrase, widen the type region, or remove secondary copy. Never distort the glyph run.

## Vertical rhythm and space

The following are conservative social-art production floors, not universal font laws:

| Role | Minimum baseline gap |
|---|---:|
| Display | 1.55× the larger line size |
| Headline | 1.50× the larger line size |
| Body | 1.48× the larger line size |
| Support | 1.42× the larger line size |

For a complete Myanmar text block, reserve at least `0.32 em` above and `0.36 em` below before another region starts; increase this when the chosen font/word has unusually tall marks. These values intentionally sit near the W3C 1.5× user-adjustable line-spacing benchmark but include script-specific production judgment. The [W3C criterion](https://www.w3.org/WAI/WCAG21/Understanding/text-spacing) applies to adaptable web content and is not itself a fixed social-image layout prescription.

## Type character that survives shaping

- Use size, real weight, one script-aware display accent, or a whole-word color region.
- Keep ornamental rules below the full word/phrase's visible extent. Never place them through the lower/upper mark zone.
- Use shadows on the text group, not heavy per-glyph strokes.
- Select display serif only for concise wording with generous line spacing. Use a neutral tested sans for instructions and facts.
- Use no more than one display family plus one text family unless the campaign has a specific, testable reason.

## Myanmar audit and visual QA

For deterministic SVG, verify that Myanmar text has language metadata, explicit Myanmar families, shaping features, valid grouped baselines, no overflow, and a successful XML parse. In this workspace, the established strict checker is:

```sh
python3 /Users/min/.codex/skills/myanmar-social-graphics/scripts/audit_myanmar_svg.py final.svg --strict
```

Then render the exact production dimensions, inspect all tone/stacked marks at 100%, and review a channel-scale preview (AuriX uses 324×405 for 1080×1350). A structural pass does not certify natural Burmese copy or readable composition.
