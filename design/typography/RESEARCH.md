# Burmese + English typography for AuriX social content

Research date: 2026-09-16. Scope: AuriX's Myanmar-first social identity, but with rules intended to transfer to other local brands and scripts.

## Decision in one minute

- **Default Burmese:** Noto Sans Myanmar. Use a real weight, keep it as the information/body voice, and use it for most campaign headlines. This remains consistent with the brand guide.
- **Optional hook voice:** Noto Serif Myanmar can add warmth/editorial character to one short, high-priority Burmese phrase. Do not let it take over facts, how-to steps, prices, or long copy. Use it only when the campaign idea benefits from that voice.
- **Default Latin and numerals:** Inter, as already specified by the AuriX guide. In a bilingual post, Burmese should carry the reading path; English should identify the product, bot handle, technical terms, or a concise brand accent—not become the headline merely because it is easier to set.
- **Compact interface-only option:** Noto Sans Myanmar UI. The specimen confirms it renders correctly, but its compact voice is not a reason to shrink ad copy.
- **Alternative Burmese family:** Padauk 6.000 is a legitimate, open-licensed comparison option, not an automatic replacement for Noto. Its rendered voice is softer and somewhat more open; retest the exact copy and audience before adopting it.
- **Use selectively:** Oswald for short Latin numerals or a compact display label; Fraunces for a rare story-led/editorial accent; JetBrains Mono for technical metadata only. Never let an English face silently become the Burmese face through fallback.
- **Reject as a portable identity default:** Myanmar MN. It is a local system specimen, not a redistributable cross-device brand font. Its thinner, more calligraphic look also departs from AuriX's calm, practical service voice.

The best way to give AuriX typographic character is not to add a stroke to every glyph. Use a disciplined contrast of **scale + real weight + one semantic color accent**, with an occasional Burmese serif hook when the campaign deserves it. Keep the official AuriX logo artwork unchanged; a font specimen is not a logo redraw.

## What was tested

The same short Burmese/English content was rendered in deterministic SVG at 1080 px width using librsvg/Pango with explicit Myanmar families and `xml:lang="my"`. Six specimens cover Myanmar families, Latin/Myanmar pairings, static treatments, a motion storyboard, an integrated 1080×1350 type composition, and numeral systems. The integrated composition was also rendered at 324×405 for a feed-scale reading pass. Fontconfig sees the bundled comparison fonts from this project only; no system-wide font installation was performed.

All five Burmese SVGs pass the strict local Myanmar SVG audit. HarfBuzz shaping was also run on a stacked-mark phrase in Noto Sans Myanmar, Noto Serif Myanmar, and Padauk. The images were visually inspected at native size; the integrated composition was inspected at 324×405.

These are **directional judgments from one renderer, one machine, and a controlled specimen**, not a nationally representative preference study. Before changing AuriX's master family, test the finalist artwork on several actual Myanmar Android phones, Facebook compression, and with fluent readers from the intended audience.

## Typeface and pairing comparison

Scores are 1–5, based on the displayed specimen and project requirements (not a universal rating). “Portable” means the family can be legally and practically supplied with the project when its license is included; it is not a claim that every embedding/subsetting use has been legally reviewed.

### Myanmar families

| Family | Mark clarity / shaping | Mobile voice | Character for AuriX | Portability | Judgment |
|---|---:|---:|---:|---:|---|
| Noto Sans Myanmar | 5 | 5 | 4 | 5 | Primary all-purpose choice; sturdy and neutral enough to let copy and composition create personality. |
| Noto Sans Myanmar UI | 5 | 4 | 3 | 5 | Correct for constrained product/UI labels; similar enough to the default that it adds little display distinction. |
| Noto Serif Myanmar | 4 | 3 | 5 | 5 | Strongest new character in this comparison. Reserve for one short hook; keep mark room generous and avoid small sizes. |
| Padauk 6.000 | 4 | 4 | 4 | 5 | Credible alternative with a softer, open feel. The project comparison does not establish that it is universally more readable. |
| Myanmar MN | 3 | 3 | 2 | 1 | Useful only to understand local platform variation; not a stable production choice or asset to bundle. |

### Latin and mixed-script roles

| Family / pairing | Best role | What the rendered comparison says | Guardrail |
|---|---|---|---|
| Inter + Noto Sans Myanmar | Everyday bilingual default | Balanced, contemporary, and closest to the existing AuriX direction. | Keep real Inter and Myanmar fonts available in export; do not rely on a silent fallback. |
| Avenir Next + Noto Sans Myanmar | Warmer service tone | Human and polished, but it contributes less distinct value than its licensing/platform friction. | System font only; do not redistribute or embed without checking the applicable license. |
| Oswald + Noto Sans Myanmar | Campaign numerals / short Latin display | Adds compact rhythm to short Latin material. | Burmese remains in its own verified face; do not horizontally compress Myanmar to imitate Oswald. |
| Fraunces + Noto Serif Myanmar | Editorial or human-story campaign | A more expressive, soft old-style direction; high character, lower everyday utility. | One short phrase only. Do not mix it with multiple other display voices. |
| JetBrains Mono + Noto Sans Myanmar | Technical utility | Clearly codes as technical metadata and specs. | Not a headline or body font. Use sparingly so the brand does not become a terminal UI. |

Fraunces is especially instructive: its variable design has weight, optical-size, softness, and “wonk” axes. An axis is not a license to slide arbitrarily; select a value for the actual use size, then compare the output. [Fraunces' project documentation](https://github.com/googlefonts/fraunces) describes the axes and the way optical size affects contrast, spacing, and character proportions. Variable fonts more generally allow continuous values, rather than only named weights; this is a practical tool for fine hierarchy, not a substitute for design judgment. [Google Fonts' variable-font lesson](https://github.com/google/fonts/blob/main/cc-by-sa/knowledge/modules/introducing_type/lessons/introducing_variable_fonts/content.md)

## Major design dimensions: where type character comes from

Typeface categories are useful search filters, not reliable personality laws. “Serif means trustworthy” or “geometric sans means innovative” is too broad to decide a brand. Compare the actual letters in the actual language and let audience testing settle ambiguous choices.

That caution is evidence-led, not just taste: one on-screen font-persona study found group-level associations (serifs more traditional, sans serifs more casual, monospaced fonts plain/dull), but those labels came from a limited font set and should not be treated as a universal rule. A separate experiment with a hypothetical brand found that type alone did not significantly change the measured personality in its tested categories, while color and typography interacted; a 65-person print-ad eye-tracking study found that stylistic figuration affected attention/attitude differently by product category. None of those studies tested Burmese social ads or Myanmar audiences. The practical implication is to use these associations to form candidates, then test the actual local message, visual, and audience—not to infer a brand identity from “serif vs sans.” [Shaikh et al., 2006](https://journals.sagepub.com/doi/10.1177/154193120605001725) · [Jain & Pasricha, 2017](https://indianjournalofmarketing.com/index.php/ijom/article/view/114240) · [Puškarević et al., 2016](https://bop.unibe.ch/JEMR/article/view/2829).

| Dimension | Useful choice | AuriX application | Common failure |
|---|---|---|---|
| **Family structure** | Sans for practical instructions; serif as a short editorial countervoice; mono for machine-like data; condensed faces for a few short labels or numerals. | Sans carries product truth; optional serif carries one campaign hook; mono/condensed are utility accents. | Combining multiple “expressive” families until nothing owns the hierarchy. |
| **Scale and hierarchy** | A clear dominant message, one supporting idea, one action. Size contrast should show priority before color does. | Start with one Burmese hook; explain the product or benefit; use the Telegram destination last. | Every sentence set in a separate pill or at the same apparent size, which creates scattered labels rather than a reading path. |
| **Weight and width** | Use authored weights and genuinely designed widths. Let weight clarify role; width solve only real fit constraints. | A bold/black display line with a medium support line can create character without outlines. | Synthetic bold, excessive weight, or condensed Burmese used to cram copy. |
| **Optical size and contrast** | Display cuts may have different proportions from text cuts. Variable axes can tune contrast and spacing for the actual size. | Use large optical settings on short hooks only when the specimen remains legible after reduction. | Applying the most dramatic axis value at every size, so small copy becomes too fragile. |
| **Numeral style** | Lining figures align to caps; oldstyle figures blend into prose; proportional figures read naturally; tabular figures align in price columns. | Use clear, large lining figures for quota/pricing. For several price rows, test `tnum` or a monospaced numeral role and align the labels separately. | Mixing baseline heights, widths, or units, making comparable plans look irregular. OpenType distinguishes lining/oldstyle (`lnum`/`onum`) from proportional/tabular width (`pnum`/`tnum`); test the feature in the renderer instead of assuming support. [Microsoft OpenType feature registry](https://learn.microsoft.com/en-us/typography/opentype/spec/featurelist) |
| **Color and figure–ground** | Establish readable luminance first; use brand hue second. One accent color should encode one semantic reason for attention. | Midnight/Deep Space grounds white or cyan text; gold can identify a single offer or threshold; violet is a selective accent. | Cyan/gold as small text on white, or violet as small text on every background. Hue difference alone cannot rescue low luminance contrast. |
| **Grouping and alignment** | Proximity and shared alignment communicate which lines belong together; repetition establishes a system; asymmetry is useful when it clarifies direction. | Place hook and explanation in one block; keep numbers and their units together; align shared feature steps to a common axis. | Distributing micro-labels evenly around the canvas without an explicit reading order. |
| **Whitespace and measure** | Clear space is part of the type design. A shorter line is better than narrower type or smaller type when marks or counters feel congested. | Leave room above and below Burmese blocks, especially where stacked marks rise/fall beyond Latin metrics. | Treating empty space as wasted, then adding outlines, shadows, badges, or labels to fill it. |
| **Emphasis treatment** | Word-level color, one semantic underline below the shaped glyphs, or a restrained group-level shadow can clarify a reading cue. | The specimen shows word-level gold/lavender, a below-text underline, and a soft whole-group shadow. | Thick outlines or glows around Myanmar glyphs: they alter counters and make upper/lower marks look congested. |
| **Motion** | Sequence attention: settle → reveal → hold → move to proof/action. Animate the phrase as a phrase, not its individual Unicode parts. | A short feature ad can introduce a hook, one benefit, one proof point, then the bot handle. Keep the last frame readable. | Word-by-word speed that outruns Burmese reading, tracking applied to marks, or multiple simultaneous movement effects. |

The [W3C text-spacing guidance](https://www.w3.org/WAI/WCAG21/Understanding/text-spacing) tests content under user-adjusted line height of at least 1.5× and larger letter/word spacing. It is a web-content criterion, not a universal rule for every social image, but it supports the practical lesson that cramped line rhythm is a real reading barrier. For AuriX raster artwork, follow the more script-aware internal floors below and inspect the rendered image. W3C's [contrast technique G18](https://www.w3.org/WAI/WCAG21/Techniques/general/G18.html) explicitly includes images of text; use 4.5:1 as a useful minimum benchmark for ordinary-size copy and 3:1 for genuinely large/bold text, while keeping important copy more robust where it will be compressed by social platforms.

### AuriX palette contrast sample

Calculated from the canonical palette's relative luminance (ratios rounded to 3 decimals). This is a starting pairing check; gradients, photos, antialiasing, and compression still require image inspection.

| Pair | Ratio | Production reading |
|---|---:|---|
| Midnight / Cloud | 17.463:1 | Excellent for primary copy. |
| Deep Space / Cloud | 15.224:1 | Excellent for primary copy. |
| Midnight / Cyan | 11.918:1 | Strong accent text on dark. |
| Midnight / Gold | 12.070:1 | Strong accent text on dark. |
| Midnight / Violet | 4.500:1 | Just at the ordinary-text threshold; prefer larger/bolder violet or more robust combinations after compression. |
| Deep Space / Violet | 3.923:1 | Not for ordinary small copy. |
| Cloud / Violet | 3.880:1 | Restrict to genuinely large/bold display use; use dark ink for small text on lavender. |
| Cloud / Slate | 5.890:1 | Good quiet body/support ink. |
| Cloud / Cyan | 1.465:1 | Fail for text. |
| Cloud / Gold | 1.447:1 | Fail for text. |

The last two results matter: brand colors may remain useful as fields, dividers, or accents, but they are not interchangeable text colors.

## Myanmar-specific rendering rules

Myanmar is not Latin with unusual ascenders. Shaping divides text into syllable clusters, reorders characters, substitutes forms, and positions marks through OpenType tables. Microsoft's implementation note documents required Myanmar substitutions plus GPOS `mark` and `mkmk` placement. [Microsoft Myanmar shaping guide](https://learn.microsoft.com/en-us/typography/script-development/myanmar)

- Use a verified Myanmar family and actual installed weight. Set `xml:lang="my"` in SVG and keep shaping features on; never let a generic sans family choose the Burmese face.
- Treat any mixed line containing Myanmar as a Myanmar line for baseline and clearance decisions.
- Internal AuriX production floors: display leading ≥1.55× the larger line size; headline ≥1.50×; body ≥1.48×; support ≥1.42×. Keep at least 0.32 em above and 0.36 em below the complete Myanmar block before another region begins. These are conservative house values, not published font standards.
- Never “fix” fit by tracking individual Myanmar codepoints, separating combining marks, horizontally compressing a glyph run, or applying an arbitrary outline. Shorten copy, wrap a phrase, or enlarge the text region.
- Judge the actual copy, in the actual family/weight, at final dimensions and reduced feed size. Technical SVG checks do not prove visual comfort.

## Static and motion styling verdict

- **Keep:** real weight contrast; modest scale contrast; one brand-color accent tied to a word/number; a rule drawn beneath the entire phrase; a soft shadow behind a complete text group when a photo needs separation; open composition with fewer, larger text units.
- **Use carefully:** serif/sans contrast, gradients within a single short display word, condensed Latin numerals, monochrome text over a calm photo region. Each adds a voice; do not stack all of them at once.
- **Reject by default:** random text strokes, duplicate offset glyphs, heavy glows, letter spacing on Myanmar, tiny all-caps labels whose only role is decoration, “tech” monospace everywhere, and motion applied one Myanmar codepoint/mark at a time.
- **Motion timing specimen (7 s):** 0.0–1.8 hook; 1.8–3.4 benefit; 3.4–5.4 proof; 5.4–7.0 brand/next step. This is a tested storyboard pattern, not a universal pace. Extend the hold if unprompted readers cannot finish the Burmese line at normal speed. The existing AuriX guide separately requires the final brand/CTA hold to last at least 1.5 s.

## Proofs

- [Myanmar-family comparison](proofs/01-myanmar-families.png)
- [Bilingual pairings](proofs/02-bilingual-pairings.png)
- [Static treatment comparison](proofs/03-static-treatments.png)
- [Motion storyboard](proofs/04-motion-storyboard.png)
- [Integrated feed-scale type study](proofs/05-feed-scale-composite.png)
- [Price and numeral comparison](proofs/06-numeral-systems.png)

The integrated study is not approved advertising copy; it exists to stress-test a hierarchy, the palette, Myanmar line spacing, and feed-scale legibility.

## Sources and asset provenance

See [FONT_ASSETS.md](FONT_ASSETS.md) for downloaded font versions, licenses, hashes, system-only specimens, and retrieval links. Key technical sources are linked above; other consulted sources:

- [Noto Myanmar project and QA](https://github.com/notofonts/myanmar)
- [SIL Padauk current download and release](https://software.sil.org/padauk/download/) · [character coverage and features](https://software.sil.org/padauk/)
- [Google Fonts family licensing repository](https://github.com/google/fonts)
- [OpenType registered feature list](https://learn.microsoft.com/en-us/typography/opentype/spec/featurelist)
