# AuriX Social Static Prompt Library

Version: 1.0 draft  
Primary format: Facebook feed portrait, 1080 × 1350, 4:5  
Rule: Generate the visual plate without text or logos; apply approved brand typography afterward

## 1. Shared generation language

Append this style block to each prompt unless a prompt overrides it:

```text
Premium mobile-first editorial 3D illustration for AuriX, a clear and human Myanmar-first VPN service. Deep Midnight Ink (#071421) and Deep Space (#0D2235), luminous Aurora Cyan (#36E2FF), Signal Violet (#7765FF), restrained Auri Gold (#FFC857), soft volumetric light, crisp rounded geometry, realistic material response, calm confident mood, one dominant focal object, generous negative space for a headline, highly legible silhouette at phone size, polished commercial art direction, 4:5 portrait composition, no text, no letters, no numbers, no logo, no watermark, no fake app UI, no flags, no political symbols, no padlock, no shield, no hooded hacker, no Matrix code, no cyberpunk clutter.
```

### Consistency seed

Maintain these recurring motifs across a campaign:

- one A-shaped luminous gateway;
- one cyan-to-violet signal ribbon;
- one small gold action accent;
- dark calm environment;
- rounded, tactile objects;
- empty copy zone in the upper-left or left third.

### Overlay rules

- Add text and the official SVG logo in the design tool.
- Use Inter for Latin/numerals and Noto Sans Myanmar for Burmese.
- Keep an outer safe margin of at least 96 px on a 1080 × 1350 canvas.
- Use one headline, one support line, one CTA, and one terms line at most.
- Never ask an image model to render the logo, price, quota, receipt, or Burmese text.

## 2. Prompt `ST-01` — Brand launch: The clear gateway

Status: Safe for brand preview

Visual prompt:

```text
A monumental but welcoming A-shaped portal standing in a dark minimal digital landscape, the inside of the portal opening into clean soft daylight, one elegant cyan-to-violet signal ribbon traveling through the opening toward the viewer, a subtle gold edge marking the entrance, no people, quiet premium atmosphere, camera at human eye level, portal placed on the right half, large clean negative space on the left for launch copy. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `VPN should feel simple.`
- Support: `Clear access. Human help.`
- CTA: `Start on Telegram`

Motion derivative:

The signal ribbon enters the closed A silhouette, the gateway opens, and the final frame resolves to the logo.

## 3. Prompt `ST-02` — Telegram-first human help

Status: Safe after the Telegram support path is staffed

Visual prompt:

```text
A modern smartphone on a clean dark desk beside a warm desk lamp, an abstract cyan message ribbon travels from the phone through a small A-shaped gateway and becomes a gentle gold human-shaped light presence, no visible chat interface, no readable screen content, premium contemporary Myanmar home-workspace cues without stereotypes, phone in lower-right, clear upper-left space for copy, reassuring and human rather than futuristic. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Start in Telegram.`
- Support: `A real person can help when you need it.`
- CTA: `Message AuriX`

## 4. Prompt `ST-03` — Public starter access

Status: Gated; publish only after public issuance and abuse controls are approved

Visual prompt:

```text
A small glowing data capsule emerging from an A-shaped gateway into a clean mobile-ready space, the capsule visibly compact and introductory rather than abundant, one cyan path leading forward, a gold start marker, no coins, no gift-box cliché, right-weighted composition with large left text-safe area, optimistic first-step mood. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Start small. See how it feels.`
- Offer: `300 MiB daily starter access`
- Terms: `Once per rolling 24 hours. Availability applies.`
- CTA: `Start on Telegram`

## 5. Prompt `ST-04` — Monthly 3 GiB free access

Status: Gated; publish only after live trial verification and capacity approval

Visual prompt:

```text
Three luminous rounded data forms moving in sequence through one A-shaped gateway, suggesting a meaningful trial allowance without showing digits, a subtle circular light arc implying one day, midnight environment, cyan and violet motion accents, small gold activation point, visual weight on the right and lower center, generous clean headline space above-left. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Your monthly AuriX allowance.`
- Offer: `3 GiB · 30 days · free every month`
- CTA: `Claim in Telegram`

## 6. Prompt `ST-05` — Basic 50 GiB plan

Status: Gated; publish only when the plan, price, payment channel, and capacity are live

Visual prompt:

```text
A substantial transparent data vessel filled with organized cyan light, passing through a premium A-shaped gateway, a restrained gold band indicating value, visual metaphor for a clearly measured allowance rather than unlimited volume, no numbers in the image, no currency, no server racks, clean product-ad composition, vessel on the right, large left copy zone, polished trustworthy commercial lighting. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `One clear plan.`
- Offer: `50 GiB · 30 days · 3,000 MMK`
- Support: `Receipt reviewed by AuriX staff.`
- CTA: `Buy in Telegram`

## 7. Prompt `ST-06` — How it works carousel

Status: Safe after each represented step works live

Card 1 prompt:

```text
A smartphone facing a luminous A-shaped gateway, one clean signal path between them, simple question-led composition, object on right, empty left half. [SHARED GENERATION LANGUAGE]
```

Card 2 prompt:

```text
A selected rounded plan card represented only by abstract blocks and a gold selection ring, no readable interface and no text, one clear choice among three muted cards, centered composition. [SHARED GENERATION LANGUAGE]
```

Card 3 prompt:

```text
A payment receipt represented as a blank white paper shape with a cyan verification beam and a warm human review light, no bank branding, no readable fields, no approval check mark, clean evidence-review metaphor. [SHARED GENERATION LANGUAGE]
```

Card 4 prompt:

```text
A secure-looking but non-shield-shaped access ribbon delivered from the A gateway into a smartphone, final cyan signal path continuing forward, optimistic completion frame, room for CTA. [SHARED GENERATION LANGUAGE]
```

Overlay sequence:

1. `How AuriX works`
2. `Choose a clear plan`
3. `Send your receipt for review`
4. `Receive access in Telegram`

## 8. Prompt `ST-07` — Clear limits, no fake unlimited

Status: Safe brand principle; do not attack named competitors

Visual prompt:

```text
Two contrasting paths in a minimal dark landscape: one foggy endless path fading into confusion and one clearly illuminated cyan path passing through an A-shaped gateway with visible start and end markers, the clear path emphasized with a restrained gold edge, no warning icons, no competitor branding, calm transparency rather than confrontation, headline space at top-left. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Know your data. Know your time.`
- Support: `AuriX uses clear entitlements—not a confusing “unlimited” promise.`
- CTA: `See the plan`

## 9. Prompt `ST-08` — One key, pooled data

Status: Safe educational content after terms are approved

Visual prompt:

```text
One central luminous access node branching smoothly toward a phone, tablet, and laptop silhouette, all branches drawing from the same finite cyan data reservoir, no device brand logos, no technical dashboard, a small gold ring around the shared source, simple infographic-like 3D composition with clear visual hierarchy and left-side copy space. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `One key. One shared allowance.`
- Support: `Devices using the same key draw from the same data limit.`
- CTA: `Ask AuriX`

Do not call this a physical-device limit.

## 10. Prompt `ST-09` — Receipt review and trust

Status: Safe after the review workflow is staffed

Visual prompt:

A clean abstract payment receipt card under a focused warm review lamp, a human hand visible at the edge of frame comparing it with a second neutral transaction record, cyan data lines remain separate until the review is complete, no bank logos, no readable personal data, no automatic green check mark, professional operations-desk mood, generous text-safe area above.

Overlay copy:

- Headline: `A screenshot is evidence—not proof.`
- Support: `AuriX staff verify payment before activation.`
- CTA: `See how payment works`

## 11. Prompt `ST-10` — Renewal reminder

Status: Safe for customer lifecycle communication

Visual prompt:

```text
A luminous A-shaped gateway completing a calm circular orbit, the cyan signal ribbon approaching the renewal point marked by one small gold light, no alarm clock, no countdown numerals, reassuring continuation rather than urgency, centered-right composition with clean left copy area. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Keep your access ready.`
- Support: `Renew before your current plan ends.`
- CTA: `Renew in Telegram`

## 12. Prompt `ST-11` — Connection quality without an unsupported speed claim

Status: Safe only as mood; do not add “fastest,” Mbps, or latency claims without measurements

Visual prompt:

```text
A clean cyan signal ribbon traveling smoothly through an A-shaped gateway across a quiet dark environment, no speed lines, no racing imagery, no speedometer, stable continuous illumination with no breaks, premium technical calm, wide breathing room for a factual measured result to be added later. [SHARED GENERATION LANGUAGE]
```

Overlay options:

- Pre-measurement: `Built for a clear, simple connection experience.`
- Post-measurement: insert only an approved measured fact with test context.

## 13. Prompt `ST-12` — Signal Check operations update

Status: Safe for transparent maintenance communication

Visual prompt:

```text
An A-shaped gateway in a controlled maintenance state, one cyan signal ribbon paused at an amber service marker while a second diagnostic light traces the gateway edge, no danger imagery, no broken lock, calm operational transparency, dark clean background with large upper-left message zone. [SHARED GENERATION LANGUAGE]
```

Overlay copy template:

- Headline: `Signal Check`
- Status: `[Maintenance / Investigating / Restored]`
- Detail: `[Affected service and verified time window]`
- CTA: `Updates in Telegram`

Never mark an incident “resolved” before verification.

## 14. Prompt `ST-13` — Support story

Status: Safe when based on a real, consented support case

Visual prompt:

```text
A contemporary Myanmar mobile user in a calm home or café workspace, shown from a respectful three-quarter angle, looking relieved after receiving help on a smartphone, an understated A-shaped cyan light reflection in the environment, natural wardrobe, authentic everyday setting, no identifiable message content, no exaggerated celebration, subject on right and testimonial space on left. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: use a real approved customer outcome.
- Quote: use exact consented wording or a clearly labeled paraphrase.
- Footer: `Shared with permission.`

Do not fabricate testimonials, names, usage results, or support times.

## 15. Prompt `ST-14` — FAQ template

Status: Safe evergreen system

Visual prompt:

```text
A single tactile rounded question object approaching a luminous A-shaped gateway, the object becomes a simple clear cyan path on the other side, one visual question-to-answer transformation, no punctuation symbol or text rendered in the image, centered-right composition, spacious left area for the actual question and answer. [SHARED GENERATION LANGUAGE]
```

Overlay structure:

- Series label: `Ask AuriX`
- Question: one customer question.
- Answer: no more than two short lines.
- CTA: `More help in Telegram`

Recommended first FAQs:

1. `How long does a plan last?`
2. `Can I use one key on more than one device?`
3. `What happens when the data allowance is reached?`
4. `Why does AuriX review receipts?`
5. `How do I renew?`

## 16. Prompt `ST-15` — Waitlist / controlled tester recruitment

Status: Recommended before public commercial launch

Visual prompt:

```text
A small group of distinct glowing signal points approaching one A-shaped gateway in an orderly queue, limited and intentional rather than mass-market, one gold invitation light at the entrance, no crowd, no scarcity timer, premium early-access mood, strong negative space for tester criteria. [SHARED GENERATION LANGUAGE]
```

Overlay copy:

- Headline: `Help us test AuriX.`
- Support: `We’re inviting a small Myanmar tester group before public launch.`
- CTA: `Apply in Telegram`
- Terms: state tester eligibility, data allowance, feedback expectations, and end date.

## 17. Prompt `ST-16` — Brand-pattern background

Status: Safe reusable asset

Visual prompt:

```text
Seamless premium abstract background made from sparse A-shaped gateway outlines and crossing cyan-to-violet signal paths on deep midnight navy, tiny restrained gold nodes, low contrast center area for typography, no text, no logo, no icons, no dramatic focal object, elegant subtle brand texture, 4:5 portrait.
```

Use for announcements, FAQ cards, policy posts, and copy-led content.

## 18. Prompt QA scorecard

Score each candidate from 1–5:

| Criterion | Question |
|---|---|
| First-frame clarity | Does the image read instantly on a phone? |
| Brand memory | Does it look specifically like AuriX? |
| Product relevance | Does the visual support the exact post topic? |
| Copy space | Is there clean room for approved typography? |
| Claim safety | Could the image imply an unsupported promise? |
| Cultural fit | Is the Myanmar context respectful and current? |
| Render quality | Are objects, hands, and lighting believable? |

Reject a visual if brand memory, claim safety, or cultural fit scores below 4.

## 19. Final negative prompt

Use when the generation system accepts negative prompts:

```text
text, words, random letters, fake Burmese script, logo imitation, watermark, QR code, bank logo, government symbol, national flag, political symbol, padlock, shield, hacker, balaclava, surveillance eye, Matrix code, neon cyberpunk city, cluttered circuit board, speedometer, racing streaks, globe network cliché, fake app screenshot, impossible phone, extra fingers, distorted hands, stock-photo handshake, exaggerated smile, fear, panic, misinformation, unlimited claim, anonymity claim
```
