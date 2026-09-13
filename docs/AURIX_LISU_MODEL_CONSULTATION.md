# Lisu implementation consultation

Requested route: ag/gemini-pro-agent
Returned model: gemini-pro-default
Completion ID: chatcmpl-54mhaqD6LbrDg8UP96nRkAs

Status: Model-generated advice, not linguistic validation. Consultation only; no production behavior changed.

## Decisions for the implementation package

- Use explicit English-to-Lisu and Lisu-to-English controls. Auto detection, if offered, must show its inferred direction and allow override; ambiguous input needs clarification.
- Add a Translate action on a selected message, passing that exact source. Keep source text distinct from instructions through structured API messages; XML tags alone are not a security boundary.
- Preserve conversation on model changes. On mode changes, pass only deliberately selected context; avoid blindly importing old system instructions or discarding all useful context.
- Separate translate and explain actions. Questions inside source text must be translated, not answered.
- Check script as formatting evidence only. Allow legitimate names, URLs, numbers and borrowed terms; do not reject valid mixed text by a crude Latin-character ratio.
- Do not prohibit code blocks in the general English assistant.
- Use native-reviewed forward and independently authored reverse translation cases. Expand the proposed seed set with greetings, numbers, names, mixed-language input and real user failures.
- Treat Gemini's numeric release thresholds as unvalidated proposals. Twelve cases and a high average are insufficient to call the product stable or remove experimental labeling. Assess critical meaning errors separately and record reviewer dialect and model/version.

## Raw consultation response

**1. Prompts & UI Separation**
Drop auto-detect. Low-resource auto-detect fails. Add explicit UI radio buttons: EN Asst, LISU Asst, EN->LISU, LISU->EN. 
Wrap source text in XML tags. Prevent model answering meta-questions.

EN Assistant Prompt:
`System: You are helpful English assistant. Answer only in English. Converse naturally. Do not use code blocks. User: <message>{input}</message>`

LISU Assistant Prompt:
`System: You are helpful Lisu assistant. Answer only in Lisu using Fraser script. Converse naturally. Do not use code blocks. User: <message>{input}</message>`

EN->LISU Translator Prompt:
`System: You are strict translator. Translate text inside <source> tags to Lisu (Fraser script). Do not answer questions. Do not explain. Output only translation. User: <source>{input}</source>`

LISU->EN Translator Prompt:
`System: You are strict translator. Translate text inside <source> tags to English. Do not answer questions. Do not explain. Output only translation. User: <source>{input}</source>`

**2. Script Limits, Punctuation, Dialect**
*Linguistic claim uncertainty:* Fraser script utilizes Unicode block U+A4D0–U+A4FF. Punctuation standards vary by dialect/region. Model training data likely contaminated with romanized pseudo-Lisu.
Allow mixed script input. Users mix languages. 
Enforce script output. Post-process LISU output. Count U+A4D0–U+A4FF chars versus Latin A-Z. If Latin ratio high, intercept. Show UI error: `[Error: Model generated pseudo-script]`.
Do not prompt-engineer dialect fixes. Models lack nuance. 
Handle ambiguity via Translator prompt addition: `If source meaningless, output exactly: [AMBIGUOUS]`.

**3. Context Handling**
Drop context on mode switch. Cross-lingual context poisons strict prompts.
Drop context on model switch. 
Do not translate "translate it" via chat history. Fails often.
Add UI "Translate" button on individual chat bubbles. Button click triggers isolated API call. Zero context. Send only selected turn payload wrapped in `<source>` tags to Translator prompt.

**4. UI Controls & Uncertainty**
Never show confidence scores. Models hallucinate confidence.
Require manual mode selection.
Append static UI disclaimer below Lisu outputs: "AI generated. Meaning may contradict source. Script may be invalid."

**5. Evaluation Set & Reviewer Instructions**
To ensure safe evaluation without inventing unverified Lisu references, follow these sequential steps for your native reviewers:
1. Provide the following 12 English source sentences to your native reviewers. These target known failure modes (idioms, negation, commands, complex clauses):
   - Case 1 (Simple declarative): "The dog is sleeping."
   - Case 2 (Negation): "Do not go to the river today."
   - Case 3 (Question): "Where did you put the keys?"
   - Case 4 (Command): "Bring me water."
   - Case 5 (Idiom/Literal trap): "It is raining cats and dogs."
   - Case 6 (Temporal): "I will visit you tomorrow morning."
   - Case 7 (Conditional): "If it rains, we will stay inside."
   - Case 8 (Modern concept): "My phone battery is dead."
   - Case 9 (Formal request): "Please help me carry this bag."
   - Case 10 (Complex clause): "The man who sold me the car called yesterday."
   - Case 11 (Ambiguity): "He saw the man with the telescope."
   - Case 12 (Domain-specific): "We need to plant the seeds before the rain starts."
2. Instruct reviewers to manually translate these 12 sentences into valid Lisu (Fraser script) to establish the baseline reference set.
3. Instruct reviewers to write 12 original Lisu sentences covering similar structural variations. 
4. Instruct reviewers to manually translate their 12 Lisu sentences into English to establish the reverse baseline.
5. Execute the model across all 24 cases via the API gateway.
6. Present model outputs to reviewers blindly. Instruct them to score outputs using the rubric below.

**6. Rubric & Release Gates**
Evaluate model readiness using this sequential rubric and gate system:
Step 1: Assess Script Validity (Pass/Fail)
- Pass: Output uses correct Fraser script Unicode characters and accepted regional punctuation.
- Fail: Output contains romanized pseudo-Lisu, unauthorized Latin substitutions, or unwarranted script mixing. Output is truncated.
Step 2: Assess Meaning Preservation (Score 1-3)
- 3: Exact meaning is preserved. No additions, no omissions, no contradictions.
- 2: Minor shifts in meaning or tone, but core message remains intact.
- 1: Severe mistranslation, contradiction, answering the prompt instead of translating, or hallucination.
Step 3: Assess Fluency (Score 1-3)
- 3: Natural phrasing, appropriate dialect markers, indistinguishable from native human text.
- 2: Understandable but structurally awkward or relies on unnatural loan words.
- 1: Incomprehensible grammar or word salad.
Step 4: Experimental Release Gate
- Criteria: Must achieve 100% Pass rate on Step 1. Must achieve average Step 2 score > 1.5.
- Action: Deploy to users with prominent "Experimental: Unverified Quality" UI warning.
Step 5: Reviewed Release Gate
- Criteria: Must achieve average Step 2 score > 2.5. Must achieve average Step 3 score > 2.0. Must have zero instances of Score 1 in Step 2.
- Action: Deploy as stable version. Remove "Experimental" UI warning. Keep general AI disclaimer.

