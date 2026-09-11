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

Total: **100 of 104 graded checks passed on 6 of 6 cases.**

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

Two of eight calls, 25 percent, were discarded by the evidence check and
degraded to the rule fallback. Both recovered on one retry, so the failures
were **run-to-run variation, not a property of those two cases**.

The specific failing quote could not be identified. Failed responses are
not persisted by design, so no offline diagnosis was possible; the only way
anything was learned was by paying for two more calls. This is a capability
gap for an evaluation day, recorded here for a scope ruling.

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

Downstream effect: in Stage 8 a candidate with three years of practical
experience and no statistics degree would be blocked by a gate the posting
itself does not impose.

Second error in the same case: `uncertain_requirements` recorded only
`Entry-level position`, while `At least 3 years of hands-on Python
experience` was promoted to `hard_constraints`. The model noticed that
entry-level was odd but did not record that the two statements contradict
each other. The conflict was read only half way.

**Counting note.** The comparison script reports 4 failed checks for
JD-05, but these are **2 distinct errors**. The single wrong
`hard_constraints` entry matches three separate exclude terms
(`Bachelor`, `degree`, `equivalent`) because exclusions are counted per
term rather than per entry. The raw count overstates the number of
mistakes.

### 4.2 No misclassification or wrong generalisation observed

The other two error types requested for Day 27 were not observed on this
set. Every requirement in the six records sat in a column consistent with
the posting, and no requirement generalised beyond what the posting said.
With six cases this is weak evidence of absence.

### 4.3 Evidence semantics, checked by hand

All 22 evidence quotes across the six records, 14 in JD-01 to JD-04 and
8 in JD-05 and JD-06, were read manually. Every quote is verbatim and every
quote supports the requirement it is attached to. The Day 25 probe error
pattern, a requirement paired with an unrelated but real sentence, did not
appear here.

Every successful quote begins at a sentence start or a bullet item. No
mid-sentence fragment had to be quoted in the successful runs.

### 4.4 Rule baseline errors

The rule parser is a keyword search and cannot express required versus
preferred, negation, gates, or conflicts. Those columns are reported as
not supported rather than scored as wrong. Within its own capability,
skill mention, three identifiable errors remain:

| Case | Rule output | Problem |
|---|---|---|
| JD-02 | `Machine Learning` | comes from a duty sentence, not a required skill |
| JD-04 | `Java` | the posting says Java is **not** required |
| JD-06 | `(empty)` | `Rust` and `Terraform` are outside `KNOWN_SKILLS` |

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

## 5. Fallback behaviour

`parse_job_description_with_fallback` keeps the existing contract and adds
one application level entry point returning either an `llm` outcome with a
validated record, or a `rule_fallback` outcome carrying only the V1 skill
list and a fixed failure category.

Observed in production during this collection: both evidence failures
produced a `rule_fallback`, the collection stopped the remaining cases, and
no automatic second AI request was made.

Nine known failure types map one to one onto existing exception classes; no
error message text is parsed. `AIProviderConfigError`, an empty posting,
and any unexpected error propagate instead of becoming a silent fallback.
Rule skills are never labelled required, and no empty-but-complete
`StructuredJobDescription` is fabricated. Export is not part of the
fallback path, so an export failure cannot trigger a re-parse.

## 6. Cost and latency

| | |
|---|---|
| Requests sent | 8, all user run and separately approved |
| Successful, with usage | 6 - 4,296 input and 922 output tokens |
| Failed, usage unknown | 2 - may still have been billed |
| Estimated cost | **$0.0019656** |

Estimated from reported usage at input $0.20 and output $1.20 per million
tokens, the prices quoted on 2026-09-08. **This is an estimate from usage,
not a verified bill.** Two failed requests are excluded because their usage
is unknown; unknown usage is never recorded as zero.

Observed latency ranged from 1.68 s to 3.76 s across six successful calls.
This is a small-sample observation. No performance claim, percentile, or
SLA follows from it.

## 7. Limits

- Six synthetic cases. Nothing here generalises to real postings.
- JD-05 and JD-06 are second-attempt results.
- The evidence failure cause is unconfirmed; failed responses are not kept.
- Exclusion checks are counted per term, so one bad entry can produce
  several failed checks.
- `required_skills[].text` holds posting phrases such as `Strong SQL
  skills` rather than normalised skill names. Stage 8 matching will need
  normalisation.
- Scalar fields and `responsibilities` still carry no per-item evidence.
- Only successful parses are recorded; there is no failure audit trail.
- Manual semantic review was done by one reader without a second opinion.
