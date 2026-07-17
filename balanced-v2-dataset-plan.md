# Balanced V2 Dataset Plan

## Goal

`balanced-v2` is a new 150-question evaluation cohort. It does not replace or mutate the current
100-question dataset. Historical runs remain bound to their exact frozen dataset versions and
protocol hashes.

## Blueprint

| Dimension | Questions | Chinese | English | Easy | Medium | Hard |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Reasoning | 30 | 15 | 15 | 6 | 18 | 6 |
| Retrieval | 30 | 15 | 15 | 6 | 18 | 6 |
| Data reasoning | 30 | 15 | 15 | 6 | 18 | 6 |
| Instruction following | 30 | 15 | 15 | 6 | 18 | 6 |
| Long context | 30 | 15 | 15 | 6 | 18 | 6 |
| Total | 150 | 75 | 75 | 30 | 90 | 30 |

Difficulty values are restricted to `easy`, `medium`, and `hard`. Every question must also carry
a domain, a source version, a sampling seed, and a reviewed license note.

## Current Gap

The current source bundle has 100 questions: reasoning 40, retrieval 30, data reasoning 30,
Chinese 70, and English 30. It has no instruction-following dimension and no explicit
long-context dimension. Difficulty metadata is absent on 90 questions; the remaining ten use
noncanonical `one-step` and `multi-step` labels. Domain metadata is complete but uses 63 labels
that require taxonomy normalization.

The target cohort should retain a reviewed 75-question anchor set and add 75 questions:

| Work item | Retain | Add | Final |
| --- | ---: | ---: | ---: |
| Reasoning | 25 | 5 English | 30 |
| Retrieval | 25 | 5 English | 30 |
| Data reasoning | 25 | 5 English | 30 |
| Instruction following | 0 | 15 Chinese + 15 English | 30 |
| Long context | 0 | 15 Chinese + 15 English | 30 |

Retaining a question is not automatic. The anchor review must verify the reference answer,
difficulty, domain taxonomy, prompt integrity, and licensing. The five additional English
questions in each existing dimension correct the current language imbalance.

## Source Acceptance

New questions must come from a pinned, reproducible source with an explicit redistribution and
commercial-use decision. Candidate public benchmarks must not be imported merely because their
repository code has a permissive license; the dataset license and any upstream document licenses
must be reviewed separately. Decrypted or non-redistributable benchmark material must not be
committed or emitted by audit reports.

IFEval was reviewed but not selected because its prompts require dedicated rule verifiers rather
than ordinary reference-answer judging. LongBench v2 was reviewed but not selected because the
repository license does not establish uniform redistribution rights for every upstream context.
The two new dimensions therefore use original deterministic synthetic tasks containing no
third-party prose. The candidate remains internal-only because its 75 anchors retain the original
benchmark restrictions, including noncommercial and no-redistribution terms.

The candidate builder produces all 150 questions and passes automated schema, answer,
prompt-uniqueness, metadata, language, difficulty, and long-context checks. After human release
review, the controlled maintenance import reruns the enforced audit and activates an isolated
`balanced-v2` profile only when the complete frozen cohort and five-dimension score configuration
are present.

## Unified V3 Lifecycle

`balanced-v2` is an archival source cohort after release. It is not copied into a third stored
dataset. When the 6 latest frozen `legacy-v1` versions and the 7 language-specific versions from
all 5 balanced original benchmark families are present, the backend validates and exposes these
immutable profile compositions:

| Profile | Selection | Questions |
| --- | --- | ---: |
| `unified-v3` | 100 legacy + 75 balanced originals | 175 |
| `unified-v3-reasoning` | exact reasoning subset | 45 |
| `unified-v3-retrieval` | exact retrieval subset | 35 |
| `unified-v3-data_reasoning` | exact data-reasoning subset | 35 |
| `unified-v3-instruction_following` | exact instruction subset | 30 |
| `unified-v3-long_context` | exact long-context subset | 30 |

Activation is all-or-nothing. Validation requires exact dataset and question totals, the expected
ability distribution, and no repeated whitespace-normalized case-insensitive prompts. Arbitrary
mixed cohorts and partial unified selections are rejected. The balanced 75-question anchor subset
is deliberately excluded because those questions already exist in the selected legacy versions.

All unified profiles use 20/20/20/20/20 scoring and protocol epoch `unified-v3`. Metric protocols
depend only on the relevant ability datasets, epoch, judge, score configuration, and aggregation
version, allowing comprehensive and specialist results to coexist on one ability leaderboard.
Global leaderboards and active/failed status summaries ignore pre-unified ranking runs, while old
snapshots remain queryable as history. Run
`archive-current-leaderboard` at cutover; it only clears the current flag and is safe to repeat.

## Release Gates

1. `audit-dataset --policy balanced-v2 --enforce` exits successfully.
2. Two reviewers verify every new reference answer and difficulty label.
3. Pilot runs cover at least four materially different Agent families.
4. Questions with no discrimination or widespread ambiguous failures are replaced.
5. Judge agreement is checked on all open-ended questions.
6. Exact dataset content hashes are pinned before profile publication.
7. Smoke, standard, and specialist subsets are derived deterministically from the frozen cohort.

## Commands

```bash
uv run python -m agent_eval.maintenance.main audit-dataset data/demo-dataset \
  --policy balanced-v2

uv run python -m agent_eval.maintenance.main audit-dataset /path/to/balanced-v2 \
  --policy balanced-v2 --enforce

uv run python -m agent_eval.maintenance.main import-balanced-v2 /path/to/balanced-v2
```
