Doc Type: rubric (normative)
Status: SIGNED by operator 2026-07-03 (three edits applied; R7 dev relabels confirmed; R8 added 2026-07-03, operator-worded — judge version ej7)
Audience: the engagement judge (quotes this verbatim) + labelers (humans use the same standard)
Environment: cross-environment
Scope: llm-kb engagement measurement

# Engagement Rubric — the operator's definition of "engaged"

_This document IS the ground truth standard. It was extracted from 83 operator-labeled rows
(48-row dev fixture + 35-row gate set) and the five boundary cases named by the 2026-07-03
gate fail (judge coherent but stricter than the operator = definition gap, not capability).
**Implementation contract:** the judge embeds a verbatim copy of §The-question + §Rules as a
constant, test-pinned against this file; any edit here requires a `judge_version` bump and a
fresh gate set. Signed rubric = operator-grounded open-endedness made literal._

## The question being judged

Per (session, moment, exactly ONE topic slug): **did the assistant genuinely ENGAGE the
topic's SUBJECT?**

- **HIGH rows** (facts were injected): engaged = the assistant **used the injected facts OR
  genuinely worked their subject**.
- **MODERATE rows** (nothing injected — control arm): engaged = the topic's subject
  **genuinely came up / was worked on its own**.

The topic's **name** (the de-slugged slug) names the subject. The identity lines shown with
it (injected facts / topic-page excerpts) are a **pointer to the subject, not its
definition** — they are often narrower, staler, or one old session's rendering.

**Scope: the MOMENT, not the whole session.** The judged object is the moment — the
triggering prompt, the assistant's reply, and the work that flows from that reply. The same
slug can be engaged at one moment and not-engaged at another within a single session. Later
excerpts corroborate the moment's work; they are NOT independent evidence of engagement
elsewhere in the session.
> Worked: `acme-widgets`, one session, two moments — the adapter-design moment:
> **ENGAGED** (dev r6); the honesty-exchange moment about a product link: **NOT ENGAGED**
> (dev r2).

## Rules

**R1 — Judge at the slug's subject grain, not the identity text's grain.**
If the session works the subject the *name* points at, it is engaged even when the
identity's specifics never appear.
> Worked: `config-api-migration` — the session migrated a client onto the config-driven
> system; the injected facts were doc-status meta-facts. **ENGAGED.** (gate r1)
> Worked: `time-estimate-mechanics` — the session designed estimate-vs-actual tooling; the
> facts were ms-conversion mechanics. **ENGAGED.** (gate r9)

**R2 — Concrete instances of the subject COUNT.**
Doing the kind of work the topic is about is engagement; the topic's name need never be
spoken.
> Worked: `product-regeneration` — a live forced-config regen of one product IS product
> regeneration. **ENGAGED.** (dev r13)
> Worked: `supply-chain-subtask` — creating a supply-chain subtask under the go-live parent
> is this subject being worked, even though it was a different ticket than the identity's.
> **ENGAGED.** (gate r5, r35)

**R3 — Correct APPLICATION of the content counts — including applying it to rule it out.**
If the assistant uses the topic's content to reason, scope, or disambiguate — even
concluding "this doesn't apply here" — that is engagement: the injection did its job.
> Worked: `ready-for-review-exclusion` — "the RFR-excluded idea is for schedulable-hours
> math; this completion metric counts RFR." The fact was applied correctly to separate two
> metrics. **ENGAGED.** (gate r15)
> Contrast: merely counting RFR tickets in a dashboard tally, never touching the exclusion
> idea, is NOT engagement with this topic. (gate r13, r14, r16)

**R4 — Responsiveness is not engagement.**
Answering the user's request well is orthogonal. An excellent reply on a different subject
is NOT engaged.
> Worked: `acme-widgets` facts injected during an honesty exchange about a product link —
> good reply, subject unworked. **NOT ENGAGED.** (dev r2)

**R5 — Word overlap, shared vocabulary, and merely adjacent areas do NOT count.**
Same infra words (gate/log/config) or neighboring work is not the subject.
> Worked: `admission-gate-design` (music-pipeline enqueue gate) injected on LLM-judge-gate
> design prompts — vocabulary collision. **NOT ENGAGED.** (gate r17, r19)
The R5↔R2/R3 boundary: the test is whether the subject is **WORKED**, not whether it is
named or near. Designing regen-automation safety rails inside a broader framework IS working
`api-automation` (gate r24); a passing mention of a component inside unrelated analysis is
not.

**R6 — An explicit dismissal of the injection does not decide the verdict; the session's
subject does.**
"Ignoring the injected memory" with a session whose own subject IS the topic → engaged
(dev r4: the correction-harvesting-workflow session). Dismissing an off-topic injection →
not engaged (dev r6-class misfires).

**R7 — Per-slug discipline.**
Each judgment is about exactly one slug. A sibling or co-advertised topic engaging does not
transfer.
> Worked: `iam` is NOT engaged by embedding-model work, even though sibling `llm-models`
> would be. (dev-era finding, rows 42/46 class)

**R8 — Project-name and catch-all slugs: ubiquity is not engagement.**
When a slug names an entire project, repo, or workspace (e.g. `acme-portal-api`), merely
working WITHIN that project does not engage the topic — otherwise every session engages it
by definition and the verdict carries no information. Engagement requires the session to
work the topic AS A SUBJECT — its architecture, configuration, or behavior as a system —
or to use the injected facts.
> Worked: merging WIP inside the portal-api clone — **NOT ENGAGED** (dev r3).
(Same logic as the retrieval catch-all blocklist: an always-true signal is informationless
— same topic class, same cure.)
R8 applies ONLY when the slug names an entire project, repo, or workspace. It does NOT
extend to systems, features, migrations, or subjects WITHIN a project (e.g.
`config-api-migration` is a subject, not a catch-all — R1 governs it, not R8).

## "Corrected" (unchanged definition)

`corrected` = the TOPIC's content was wrong or contested by the operator that session — a
verdict on the fact, never on the agent's behavior, **and never on the agent's USE of a
correct fact** (fact right, application wrong = NOT corrected). Rare by construction.

## Notes for labelers and judge alike

- MODERATE control rows use the same rules; the only difference is nothing was placed in
  context, so R3's "application of content" reads as "the subject was worked organically."
- When the identity is stale or mismatched to the slug (pre-flag it per §2.8 of the judge
  spec), judge the slug's subject anyway — the identity never overrides the name (R1).
- **R1 makes the SLUG NAME load-bearing:** the name is now part of the measurement chain,
  and a vague or wrong name misleads the judge WITH rubric backing. The recompile/curation
  work must treat "does the name actually name the subject" as a first-class check,
  alongside "does the page match the name."
- **Null-bias tiebreak, strengthened:** R1–R3 must be honestly attempted BEFORE any
  tiebreak — a session that works the subject under any of them is engaged. "Genuinely
  torn" means torn after applying R1–R7, not uncertain on first read. The tiebreak is a
  last resort, never an exit from hard R2/R5 calls (the prior judge failed by being
  systematically stricter than the operator; a tiebreak reached early re-creates that
  failure under rubric cover).
