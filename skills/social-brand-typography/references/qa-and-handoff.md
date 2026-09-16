# Comparison, QA, and handoff checklist

## Controlled comparison

1. Lock a real audience-facing sentence, a mixed-script sentence (if relevant), a short numeral/price string, and a common post size.
2. Keep wording, text region, baseline, color field, and position fixed. Compare one variable at a time.
3. Choose a short list covering distinct jobs rather than many near-identical files. Record the exact face/weight/variation axes and resolved file.
4. Render true script shaping and combine-mark cases. Use the final production renderer.
5. Compare at native dimensions and at a feed-scale reduction. A candidate that is attractive only at 100% is not approved.
6. Score for: script coverage/shaping, readability, brand fit, role flexibility, mobile/compression robustness, licensing/distribution risk, and export stability. Mark subjective scores as directional and name the environment.
7. Select a small role system and write the reasons and disallowed uses. A font shortlist without implementation rules is not a brand system.

## Static visual checks

- Is there one obvious first read, then a sensible second read, then one action/source?
- Can a user tell what the product/service is without decoding vague headline language?
- Do labels each provide a distinct piece of information? Remove repeated microcopy and decorative labels.
- Are lines grouped by proximity/alignment and do they have enough breathing room?
- Does any word, diacritic, punctuation, logo clear-space, or unit clip into another region?
- Does a color or effect obscure rather than encode meaning?
- Does the post still make sense without color and at reduced brightness?
- Are exact prices, quotas, dates, and eligibility values entered as editable text and correct?

## Device and access checks

- Inspect the native master, 25–33% thumbnail, and at least one real Android feed render when available.
- Ask readers who use the target language to check copy and natural line breaks. Font shaping QA cannot certify idiomatic language.
- Add the essential message in the caption/alt description where platform tools allow; do not make an image the only place where an offer or limitation exists.
- Use contrast checks as a floor, then review antialiasing, compression, low-quality screens, and busy photo texture.
- Make an evidence note: renderer/version, dimensions, resolved faces, strict-audit result, thumbnail result, audience proof status, and remaining platform uncertainty.

## Handoff format

Provide:

- selected family files and real weights, variable-axis values, and role map;
- a sample sheet showing chosen and rejected directions;
- approved palette pairings and disallowed low-contrast pairs;
- language/script-specific leading and block-clearance rules;
- static effects rules and motion unit/timing/hold rules;
- source/license ledger for downloaded typefaces;
- final editable source and only the few necessary PNG/MP4 previews;
- a short “do not” list tied to observed failures (not generic instruction noise).

## This AuriX project: acceptance pattern

For deterministic Myanmar SVGs in this repository: run the strict audit, render 1080×1350 masters and 324×405 previews, inspect at native and feed size, and preserve the existing local-only Git preference. Do not push generated social work to GitHub unless explicitly requested.
