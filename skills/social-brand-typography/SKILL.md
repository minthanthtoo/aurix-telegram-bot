---
name: social-brand-typography
description: Research, compare, select, and apply typography for local-brand social marketing in Burmese, English, or other scripts. Use for font exploration, brand type systems, bilingual ads, static graphics, motion typography, and type-focused design reviews. Includes script-aware rendering, font licensing checks, and feed-scale QA; it does not replace brand strategy or language proofreading.
---

# Social brand typography

Build a repeatable type system from the brand, audience, scripts, and channel—not a font mood board alone. This skill applies to local brands broadly. Do not transfer AuriX colors, font choices, copy, or product assumptions to a different client.

## Required workflow

1. **Establish the brief.** Read the active brand guide, audience, exact claims, platform, dimensions, and approved logo assets. Identify every writing system in the artwork and which language should lead the reading path. Preserve exact copy unless the user asks for copy development. If copy does not fit, propose concise alternatives separately; never solve it by squeezing script forms or shrinking critical text.
2. **Audit the current type environment.** List actual installed/project font faces and weights, script coverage, fallbacks, variable axes, rendering engine, and licenses. Inspect the family actually resolved by the export tool—not only the intended CSS/SVG name. If the required font is missing, compare a small range of appropriately licensed alternatives; keep license files and sources with downloaded fonts. Do not install fonts system-wide without authorization.
3. **Map design possibilities.** Read `references/type-systems-and-layout.md`, then select candidates across genuinely different roles (for example, a highly legible text sans, a distinctive display serif, a condensed numeric face, and a mono utility face). Treat category associations as hypotheses to test, not universal psychology.
4. **Render a controlled comparison.** Hold copy, dimensions, color field, and baseline constant while changing one variable at a time: family, weight, width, role contrast, numeral feature, color, shadow, outline, or underline. Include at least one real Burmese phrase when Myanmar script is involved. Save source and raster proof for the few comparisons that materially affect the decision.
5. **Select roles, not a pile of fonts.** Recommend a compact system: display/hook, headline, body, utility/UI, numbers, and optional campaign accent. Most brands need only 2 families and 3 weights. State when an optional face is allowed and what it must never own.
6. **Set the reading path.** Use size and grouping first; alignment and whitespace second; weight and color as supporting signals. Apply Gestalt proximity/similarity/continuity intentionally. Use one distinctive treatment tied to meaning. Do not scatter equal-size labels, add decorations to fill space, or turn every text block into a card.
7. **Handle scripts correctly.** Read `references/complex-scripts-and-burmese.md` for Myanmar or any script with shaping/combining behavior. Confirm shaping, language tags, mark attachment, real weights, baseline rhythm, and mixed-script behavior. Never track, split, outline, or animate combining marks independently.
8. **Design motion as a reading sequence.** Read `references/motion-type.md` when animation is requested. Give one idea time to enter and settle; preserve complete words/phrases as the animation unit; hold factual copy long enough to read; keep logo and CTA on screen long enough to register.
9. **Review at real viewing scales.** Render at production dimensions, inspect at native size, then reduce to the channel's likely preview size (for AuriX: 324×405 from a 1080×1350 post). Check line collisions, overflows, contrast, text/logo clearance, reading order, and whether the idea can be understood without zooming. Run available structural script audits but never treat them as visual approval.
10. **Hand off a usable rulebook.** Document the chosen families/weights/roles, allowed accents, line/space floors, brand colors and tested combinations, motion behavior, licensed assets, sources, rejected options, and device/renderer caveats. Make a future agent able to repeat the test without copying arbitrary coordinates.

## Deliverables

- a short evidence-led recommendation and candidate comparison;
- a reusable brand type charter with examples for static and motion use;
- a small number of well-labeled specimen boards, including a mobile preview for multilingual work;
- a font/licensing ledger for any downloaded assets;
- validation results and remaining uncertainty, especially where real-device or audience testing is unavailable.

## Quality bar

The winning candidate must make the message clearer and feel right for the audience at feed scale. “Interesting” at 100% is not enough. Do not declare a universal winner from one operating system, a single text sample, or personal taste. If the winner depends on campaign tone, state the conditions for each option. Keep official logos as supplied artwork; do not recreate a wordmark by typing its name in the proposed brand font.

For theory, read `references/type-systems-and-layout.md`, `references/complex-scripts-and-burmese.md`, `references/motion-type.md`, and `references/qa-and-handoff.md` as relevant. If working on AuriX inside its repository, also read `docs/AURIX_BRAND_GUIDE.md`, `docs/AURIX_SOCIAL_STATIC_STANDARD_S1.md`, and `design/typography/RESEARCH.md`; those are client-specific evidence, not defaults for other brands.
