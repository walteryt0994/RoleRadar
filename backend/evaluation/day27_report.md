# Day 27 - Rule baseline versus LLM parser

Date: 2026-09-11
Case set: `evaluation/jd_cases.py`, version 1, frozen 2026-09-10
Versions under test: prompt 1, parser 1, schema 1, record 1
Model: `gpt-5.6-luna`, `reasoning_effort` none, `max_output_tokens` 900,
`max_retries` 0, `store` false

Expectations were written before any model output was collected. The six
postings are fictional. No resume, profile, or personal data was sent.

## 1. How to read this report

Three sources are kept apart and never mixed:

- **Manual expectation** - the answer a careful reader should produce from
  the posting plus the rules already written in `PARSER_INSTRUCTIONS`.
- **Rule baseline** - the unchanged V1 `extract_skills`, a `KNOWN_SKILLS`
  word-boundary search. `KNOWN_SKILLS` was not edited for this experiment.
- **LLM observation** - six real API responses, replayed offline from
  saved records in the ignored `backend/records/` directory.

Mock providers appear only in tests. No mocked output is reported here as
model behaviour.

## 2. Per-case results

| Case | Focus | Rule baseline | LLM graded checks |
|---|---|---|---|
| JD-01 | required vs preferred | `Python, SQL, AWS, Tableau` | 17 / 17 |
| JD-02 | degree and authorization gates | `Python, Docker, Machine Learning` | 19 / 19 |
| JD-03 | unstated fields stay null | `SQL, Java, Kubernetes` | 15 / 15 |
| JD-04 | negated skill | `Python, SQL, Java, Docker` | 15 / 15 |
| JD-05 | alternative branch and conflict | `Python, Excel, Data Analysis` | 14 / 18 |
| JD-06 | unknown tech, duties, injection | `(empty)` | 20 / 20 |

Two counts describe this run and both are reported:

- **First attempt: 4 of 6 cases produced a usable record.** JD-05 and JD-06
  failed the evidence check.
- **Final state: 6 of 6 cases have a stored successful record**, because
  JD-05 and JD-06 were retried once each under a separate approval.

Against the stored six records, **100 of 104 graded checks passed**. The
four failed checks correspond to **two** expectation deviations; see the
counting note in 4.1.

These counts cover this fixed six-case set only. They are not an accuracy
rate and must not be extrapolated. No precision or recall is claimed; the
comparison object would differ per column and the denominators are tiny.

The rule baseline output matched its frozen expectation in all six cases.

## 3. Attempts and reproducibility

JD-05 and JD-06 each failed the evidence back-reference check on the first
attempt and succeeded on a second attempt with identical input, versions,
and call options. **Their rows above are second-attempt results.**

Eight requests were sent in total: six in the approved batch and two in a
follow-up diagnostic batch, each separately approved.

| Case | Attempt 1 | Attempt 2 |
|---|---|---|
| JD-01 to JD-04 | success | not needed |
| JD-05 | `evidence` failure | success |
| JD-06 | `evidence` failure | success |

Two of eight calls in this run were discarded by the evidence check and
degraded to the rule fallback. Both succeeded on one retry, which shows the
failures are **not deterministic for these two cases**. It does **not**
show the cause is random: the input, the prompt, the exact-match rule and
the varying output could still interact systematically. **The failure cause
is unconfirmed.**

The 2 of 8 figure is an observation from this run, which included selective
retries of the two failing cases. It is not a stable failure rate.

The specific failing quote could not be identified because failed responses
are not persisted by design. Paying for two more calls produced a different
outcome; it did not diagnose the original failures, and it is not the only
possible way to investigate them. Retaining a minimal failure record is one
option for a future batch, recorded here for a scope ruling.

One verified property of the current code: `verify_evidence` compares
whitespace-normalised strings **case sensitively**. `mentor junior
engineers` passes and `Mentor junior engineers` does not. This is a
plausible contributor to the two failures but was **not confirmed**.

## 4. Error analysis

### 4.1 LLM error, JD-05 - hard constraint over-inference

The model placed this entry in `hard_constraints`:

```text
Bachelor's degree in Statistics, or equivalent practical experience
```

`PARSER_INSTRUCTIONS` states: *Never treat a preferred, optional,
alternative, vague, or contradictory statement as a hard constraint.* A
degree requirement carrying an explicit alternative is not an
unconditional gate, so this violates an instruction the prompt already
gives.

Scope of this finding: the failure is against the frozen v1 expectation,
which asks for an alternative branch to stay out of `hard_constraints`. The
saved entry does keep the full `degree OR equivalent practical experience`
wording and does not split the OR into two independent gates. The risk is
therefore that a later matching implementation reads a hard constraint as a
gate; there is no matching code yet, so no candidate is blocked today.

Second deviation in the same case: `uncertain_requirements` recorded only
`Entry-level position`, while `At least 3 years of hands-on Python
experience` was promoted to `hard_constraints`. The frozen expectation for
this case asks the tension between the two statements to be surfaced as
uncertain. That is a business assumption of this experiment, not a universal
logical contradiction; a posting may legitimately use both phrasings. The
failure count follows the frozen standard and the assumption is stated here
so the count can be judged.

**Counting note.** The comparison script reports 4 failed checks for
JD-05, but these are **2 distinct errors**. The single wrong
`hard_constraints` entry matches three separate exclude terms
(`Bachelor`, `degree`, `equivalent`) because exclusions are counted per
term rather than per entry. The raw count overstates the number of
mistakes.

### 4.2 No further misclassification or wrong generalisation observed

Apart from the JD-05 hard constraint deviation in 4.1, which is itself a
column choice, no other requirement sat in a column inconsistent with its
posting, and no requirement generalised beyond what the posting said. This
is "no other case observed", not "no misclassification". With six cases it
is weak evidence of absence.

### 4.3 Evidence semantics, checked by hand

All 22 evidence quotes across the six records, 14 in JD-01 to JD-04 and
8 in JD-05 and JD-06, were read manually. Every quote is verbatim and every
quote supports the requirement it is attached to. The Day 25 probe error
pattern, a requirement paired with an unrelated but real sentence, did not
appear here.

Every successful quote begins at a sentence start or a bullet item. No
mid-sentence fragment had to be quoted in the successful runs.

### 4.4 Rule baseline limits

The rule parser detects whether a known skill term is **mentioned**. It
cannot express required versus preferred, negation, gates, or conflicts,
so those columns are reported as not supported rather than scored as wrong.

Under its own contract the JD-02 and JD-04 hits are correct: both terms do
appear in the posting. They are listed here as limits that mislead when a
mention list is consumed as a list of required skills, not as wrong hits.
The JD-06 result is a separate kind of limit, vocabulary coverage. The
three are not one error rate:

| Case | Rule output | Limit |
|---|---|---|
| JD-02 | `Machine Learning` | the term comes from a duty sentence, so reading it as a required skill is a consumer error |
| JD-04 | `Java` | the posting says Java is **not** required, and a mention list cannot carry that |
| JD-06 | `(empty)` | `Rust` and `Terraform` are outside `KNOWN_SKILLS`, a coverage limit |

An empty rule result means the vocabulary found nothing. It does not mean
the posting has no requirements. JD-06 in fact requires two technologies.

### 4.5 Prompt ambiguity, with an observation

The prompt states that a mandatory education or authorization requirement
also belongs in `hard_constraints`, but it does not say whether a plain
required skill belongs there. Those cells are marked not graded.

Observation: across all six records the model never placed a plain
required skill in `hard_constraints`. It did promote an experience
requirement in JD-05. This is an observation about six responses, not a
decision; resolving the ambiguity is a prompt change and therefore a
version bump.

### 4.6 Prompt injection

JD-06 ends with an instruction addressed to automated tools, telling them
to mark every applicant as a perfect match. The returned record contains
no trace of it: `required_skills` holds only `Rust` and `Terraform`,
`hard_constraints` and `uncertain_requirements` are empty, and no field
mentions a match verdict. One observation on one posting is not a
security assessment.

## 5. Batch contract and the sealed live entry

A record only counts for this batch when the posting fingerprint, the
prompt, parser and schema versions, the provider, the requested model, the
requested output limit and the requested reasoning effort all match
`evaluation/batch_contract.py`. The comparison and the collection tool read
the same definition, so a record collected under other settings is reported
as a named skip and the case stays unmeasured rather than being silently
counted or silently skipped as already collected. The returned model is
recorded as observed and is deliberately not part of the contract.

The collection tool still parses its arguments strictly: a missing `--only`
value, an unknown flag or an unknown case id exits non-zero. Its live entry
point has since been **sealed**: the module no longer imports a provider,
never loads an API key, and has no collection loop, so `--send` is refused
with a non-zero exit and there is no send path left to call directly.

While the batch was running, the tool checked a per-request cost estimate
before sending and stopped on a failure or an unknown usage. That estimate
was a character-ratio heuristic, not a proven upper bound, and its running
balance lived only in one process: a failed request saved no record, so a
restart did not know it had happened. It is therefore described here as a
heuristic that was in force during the batch, never as a guaranteed cost
ceiling. The tool now reports only the subtotal of the usage the stored
records report, and derives no remaining balance.

## 6. Fallback behaviour

`parse_job_description_with_fallback` keeps the existing contract and adds
one application level entry point returning either an `llm` outcome with a
validated record, or a `rule_fallback` outcome carrying only the V1 skill
list and a fixed failure category.

Observed during this approved synthetic collection: both evidence failures
produced a `rule_fallback`, the collection stopped the remaining cases, and
no automatic second AI request was made.

Nine known failure types map one to one onto existing exception classes; no
error message text is parsed. `AIProviderConfigError`, an empty posting,
and any unexpected error propagate instead of becoming a silent fallback.
Rule skills are never labelled required, and no empty-but-complete
`StructuredJobDescription` is fabricated. Export is not part of the
fallback path, so an export failure cannot trigger a re-parse.

## 7. Cost and latency

| | |
|---|---|
| Requests sent | 8, all user run under two separately approved batches |
| Successful, with usage | 6 - 4,296 input and 922 output tokens |
| Failed, usage unknown | 2 - may still have been billed |
| Estimated cost of the known usage | **$0.0019656, plus an unknown amount for two failed requests** |

These eight requests are approved synthetic verification against fixed
fictional postings, not production traffic.

Estimated from reported usage at input $0.20 and output $1.20 per million
tokens, the prices published on the official model page on 2026-09-11.
**This is an estimate from usage, not a verified bill.** Two failed requests
are excluded because their usage is unknown; unknown usage is never recorded
as zero. The project total is likewise "an estimate of $0.0029488 for the
known usage, plus an unknown amount", not a complete cost.

Observed latency ranged from 1.68 s to 3.76 s across six successful calls.
This is a small-sample observation. No performance claim, percentile, or
SLA follows from it.

## 8. Limits

- Six synthetic cases. Nothing here generalises to real postings.
- JD-05 and JD-06 are second-attempt results, so the final 6 of 6 is not a
  first-attempt figure.
- The evidence failure cause is unconfirmed; failed responses are not kept,
  and the retries did not diagnose the original failures.
- The cost figures cover the known usage only; two requests have unknown
  usage and are excluded rather than counted as zero.
- Exclusion checks are counted per term, so one bad entry can produce
  several failed checks.
- `required_skills[].text` holds posting phrases such as `Strong SQL
  skills` rather than normalised skill names. Stage 8 matching will need
  normalisation.
- Scalar fields and `responsibilities` still carry no per-item evidence.
- Only successful parses are recorded; there is no failure audit trail.
- Manual semantic review was done by one reader without a second opinion.
- The live collection entry point is sealed, so this batch cannot be
  re-run or extended from this code; a further batch needs a new approval
  and a live entry point built for it.
