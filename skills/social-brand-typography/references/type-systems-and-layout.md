# Type systems and social layout

## Start with a job, not a category label

Choose type to perform roles: attract, explain, prove, label, or direct. Serif, sans, slab, script, mono, humanist, grotesque, geometric, rounded, and condensed are candidate descriptions—not audience truths. “Serif is trustworthy” and “rounded is friendly” are hypotheses; test the actual alphabet, script, weight, and composition with intended readers. Empirical work reports group-level associations, but also context/product-category interactions and null effects for isolated type in some studies; see [Shaikh et al.](https://journals.sagepub.com/doi/10.1177/154193120605001725), [Jain & Pasricha](https://indianjournalofmarketing.com/index.php/ijom/article/view/114240), and [Puškarević et al.](https://bop.unibe.ch/JEMR/article/view/2829). These are not Myanmar social-ad audience studies, so use them to generate testable directions, not universal rules.

## Type dimensions to explore

| Dimension | What it changes | How to test it |
|---|---|---|
| Family / construction | Stroke contrast, terminals, rhythm, counter size, personality | Compare the same hook and sentence; include a real mixed-language line. |
| Weight | Priority, tone, density, counter closure | Compare real authored weights; check small marks and counters after compression. |
| Width | Horizontal footprint and cadence | Use an authored width/condensed family. Do not apply transform-scale to script glyphs as a fit hack. |
| Optical size | Details, spacing, contrast, and robustness at different sizes | Use a variable `opsz` axis only if present and supported by the export engine; judge both display and small use. |
| Slant / italic | Direction, emphasis, voice | Test whether it helps scanning; do not use fake obliques or slant a script that does not use that convention. |
| Numeral design | Monetary/product-data legibility and alignment | Compare lining vs oldstyle, proportional vs tabular, and mono numerals in the actual plan layout. OpenType `lnum`, `onum`, `pnum`, and `tnum` control distinct choices; a renderer may not support all features. |
| Spacing / measure | Reading speed, line tracking, texture, emphasis | Adjust line breaks and block width first. Apply tracking only where the script and face support it. |
| Variable/custom axes | Fine degrees of weight, width, optical size, softness, or designed custom traits | Record family, axis names, and tested values. Do not equate maximum axis expression with best brand fit. |

Variable fonts interpolate authored designs along named axes; an optical-size axis may alter spacing and detail as well as size. Treat axis values as design choices: compare at the output size and retain the selected value in the design handoff. See the [Google Fonts variable-font lesson](https://github.com/google/fonts/blob/main/cc-by-sa/knowledge/modules/introducing_type/lessons/introducing_variable_fonts/content.md) and [Fraunces axis notes](https://github.com/googlefonts/fraunces).

## Build a small hierarchy

Most social ads work with a small role set:

1. **Hook / display:** one short promise, question, tension, or number.
2. **Explanation:** what the product/service actually is and why the hook matters.
3. **Proof or offer:** exact facts, terms, price, limit, availability.
4. **Action / source:** one destination or next step.
5. **Utility:** only necessary labels, units, and technical metadata.

Make order obvious before applying color. A reliable reading path usually comes from size and grouping, not a chain of arrows. Keep a phrase, its qualification, and its units close enough to be perceived as one unit. Consistent alignment and proximity establish groups; a single repeated accent creates continuity; one contrast break directs attention. Use asymmetry to move the eye only when the next item is clear.

Do not distribute every label evenly around the canvas. Equal weight creates multiple competing entry points. Remove a label if it adds no new meaning. Move detail to the caption instead of compressing it into the image.

## Type personality without “effects soup”

Prefer this order: **family choice → role contrast → weight → scale → color → composition**. Effects are the last, narrowest tool.

- A contrasting display face can add an editorial/human note; keep the support face stable.
- A short whole-word color highlight can name the benefit, product, or amount; avoid coloring each syllable or mark.
- An underline is a graphic rule placed outside the glyph box, not a stroke riding the letters.
- A shadow can separate a complete text group from a photograph. Keep it soft and subordinate; do not duplicate text to manufacture weight.
- Outlines/glows are high-risk for small, high-detail scripts and can close counters or crowd marks. Test them only against a specific low-contrast problem and reject them if they deform forms.
- Gradients are for one short display phrase or scene accent; they should not reduce letter/background contrast or spread rainbow color through factual copy.
- Condensed styles belong to short Latin words or numerals. A wide script gets its own fitted text box, not a forced narrow transform.

## Color and contrast

Establish luminance contrast before hue harmony. A two-color complementary palette may look vivid but still fail to separate text from its field. Use W3C's 4.5:1 ordinary-text and 3:1 large/bold-text contrast values as practical image-text benchmarks; these are not a substitute for mobile-preview inspection or accessible text equivalents. On photographs or gradients, check the local background behind every line. Add a calm text region or darken/lighten the plate before adding a thick glyph outline.

Map an accent to one semantic reason (for example: offer, urgency, active state, or one keyword). Do not give the same highlight treatment to every label. Recheck colored copy in grayscale and common reduced-brightness scenarios so meaning does not depend on hue alone.

## AuriX implementation example

For AuriX only: Midnight `#071421`, Deep Space `#0D2235`, Cyan `#36E2FF`, Violet `#7765FF`, Gold `#FFC857`, Cloud `#F6F8FC`, Slate `#526273`. Use Cloud/white/cyan/gold over the dark foundations after contrast check; Slate is a useful quiet ink on Cloud. Cyan and Gold as small type on Cloud are low contrast; Violet is also not a reliable small-text choice against Cloud or Deep Space. The exact AuriX comparison and scores live in `aurix-case-study.md`.

## Logo and wordmark boundary

Use the approved logo file, colors, clear space, and minimum size from the brand's logo protocol. Do not use typography studies as an excuse to retype, stretch, add a stroke to, recolor, or composite the official wordmark. A brand may choose a display typeface for headings without changing the logo's own lettering.
