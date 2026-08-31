# Myanmar Typography Research and Styling Rules

## Sources

- [Noto Myanmar repository and QA proofs](https://github.com/notofonts/myanmar)
- [Noto font usage guidance](https://github.com/notofonts/noto-docs/blob/main/docs/website/use.md)
- [Microsoft Myanmar OpenType shaping specification](https://learn.microsoft.com/en-us/typography/script-development/myanmar)
- [SIL Padauk source](https://github.com/silnrsi/font-padauk)

## Font audit

- `Noto Sans Myanmar Black`: installed; selected for the expressive hook because its broader document metrics give stacked marks comfortable room.
- `Noto Sans Myanmar SemiCondensed Black`: installed; selected for the longest hook line to create width contrast without shrinking it.
- `Noto Sans Myanmar UI`: installed; selected for compact product proof. Noto's guidance positions UI variants for vertically constrained interfaces.
- `Noto Serif Myanmar`: installed and valid, but rejected here because it changes the campaign voice from practical/technical to editorial and weakens consistency with the AuriX system.
- `Myanmar Sangam MN`: installed fallback; rejected because mixed-platform rendering would be less predictable than the bundled Noto family.
- `Padauk`: credible pan-Myanmar family, but not installed in the production environment; therefore not used.

## Styling judgment

- Do not use synthetic glyph outlines: Myanmar upper/lower marks can become congested and the stroke changes internal counters.
- Do not use random glow or duplicated text merely to appear decorative.
- Use character through real weight, controlled width, palette contrast, and role contrast.
- Use shadows on the text region—not a heavy stroke around each glyph. The final uses a low-opacity navy drop shadow with generous blur.
- Use one amber curved underline behind the friction phrase. It reinforces meaning and reading order without looking like a fake button.
- Preserve `xml:lang="my"`, real Myanmar font families, and OpenType `mark`/`mkmk` features in SVG.

## Production validation

The final must pass the strict Myanmar SVG audit and remain readable in a 324 × 405 feed preview. A technically valid font does not pass unless the exact words also have adequate mark clearance and hierarchy.
