# Typography research font assets

Retrieved 2026-09-16 from the upstream sources below. These files are bundled for reproducible comparison rendering in this repository. They have **not** been installed into the operating system. Keep each license beside its font if copying or redistributing the assets.

## Bundled comparison files

| Family | Bundled file(s) | License | Source | SHA-256 |
|---|---|---|---|---|
| Inter variable | `fonts/inter/Inter[opsz,wght].ttf`, `fonts/inter/OFL.txt` | SIL Open Font License 1.1 | [Google Fonts Inter directory](https://github.com/google/fonts/tree/main/ofl/inter) | `Inter[opsz,wght].ttf`: `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` |
| Fraunces variable | `fonts/fraunces/Fraunces[SOFT,WONK,opsz,wght].ttf`, `fonts/fraunces/OFL.txt` | SIL Open Font License 1.1 | [Google Fonts Fraunces directory](https://github.com/google/fonts/tree/main/ofl/fraunces); [axis documentation](https://github.com/googlefonts/fraunces) | `Fraunces[SOFT,WONK,opsz,wght].ttf`: `177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb` |
| Oswald variable | `fonts/oswald/Oswald[wght].ttf`, `fonts/oswald/OFL.txt` | SIL Open Font License 1.1 | [Google Fonts Oswald directory](https://github.com/google/fonts/tree/main/ofl/oswald) | `Oswald[wght].ttf`: `5b38c246e255a12f5712d640d56bcced0472466fc68983d2d0410ec0457c2817` |
| Padauk 6.000 | `fonts/padauk/Padauk-Regular.ttf`, `Padauk-Bold.ttf`, `OFL.txt` | SIL Open Font License 1.1 | [SIL Padauk download](https://software.sil.org/padauk/download/) · 6.000 release dated 2025-10-08 | Regular: `3dd5406194518d903c423fc77822be4e8b6c9e6a75dfacda2eafc1a54e64cade` · Bold: `61aa2f322143229e477a3eeefdc157020ad8f477d9574685f73ecdcbe16fa0b5` |

The font binaries and their unmodified upstream `OFL.txt` files are intentionally retained; the font packages' additional documentation and webfont builds were not copied because this study uses the desktop TTFs only. Check each bundled license before making subsets, changing reserved names, or delivering font files to a third party.

## System specimens (not bundled)

| Family | Location observed on this workstation | Use / limitation |
|---|---|---|
| Noto Sans Myanmar | `/System/Library/Fonts/NotoSansMyanmar.ttc` | AuriX default; multiple weights. Host-supplied Noto collection, not copied into this project. |
| Noto Serif Myanmar | `/System/Library/Fonts/NotoSerifMyanmar.ttc` | Tested short-hook alternative; not copied into this project. |
| Noto Sans Myanmar UI | `/Users/min/Library/Fonts/NotoSansMyanmarUI-Medium.ttf` and other local weights | Tested UI-oriented option; host font installation, not copied into this project. |
| Avenir Next | `/System/Library/Fonts/Avenir Next.ttc` | macOS system font. Do not redistribute/encode without checking Apple's applicable terms and the output use. |
| JetBrains Mono | `/Users/min/Library/Fonts/JetBrainsMono[wght].ttf` | Already installed locally; technical comparison only. Not bundled here. |
| Myanmar MN | `/System/Library/Fonts/Supplemental/Myanmar MN.ttc` | Platform-only contrast sample. Not a portable or bundled production choice. |

`Inter` was specified in the brand guide but was not installed in the local Fontconfig view when this study began. The project bundle makes the specimen reproducible without changing system fonts. A future export workstation should verify the actual resolved face (`fc-match` or equivalent) and not assume that a font-family name means the intended file rendered.

## Re-rendering

From the repository root:

```sh
python3 design/typography/scripts/render_comparisons.py
```

The script creates a temporary project-local Fontconfig file/cache, renders SVG/PNG comparison boards, and does not install fonts system-wide. Temporary Fontconfig data and generated SVG proofs are ignored by the local `.gitignore`; the rendered PNG boards are the concise visual evidence set.
