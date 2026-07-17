# Agent Evaluation Platform

Internal platform for evaluating and ranking third-party Agent APIs.

## Prerequisites

- Python 3.12
- uv
- Node.js 24
- pnpm 11
- Docker Compose

## Local Development

Install dependencies:

```bash
uv sync
pnpm install
```

Generate the two shared password hashes and place them in `.env`:

```bash
uv run python -m agent_eval.maintenance.main hash-password
```

Use the generated values for `AGENT_EVAL_READ_ONLY_PASSWORD_HASH` and
`AGENT_EVAL_OPERATOR_PASSWORD_HASH`. Sessions last 7 days for read-only users and 8 hours for
operators. When writing Argon2 hashes to `.env`, wrap each value in single quotes so its `$`
characters are not interpreted by Compose.

Generate the Agent Token encryption key:

```bash
uv run python -m agent_eval.maintenance.main generate-encryption-key
```

Store it as `AGENT_EVAL_ENCRYPTION_KEY`. Losing this key makes saved Bearer Tokens impossible to
decrypt.

Run the API:

```bash
uv run alembic upgrade head
uv run uvicorn agent_eval.api.main:app --reload
```

Run the Worker in another terminal:

```bash
uv run python -m agent_eval.worker.main
```

On PostgreSQL, the configured Agent, judge, citation-fetch, and citation-domain concurrency values
are deployment-wide across all API and worker replicas. SQLite keeps process-local limits. Request
leases heartbeat during long calls and expired leases make crashed holders reclaimable; see
`deploy/runbook.md` for lease timing and database-pool sizing.

Artifact storage is selected with `AGENT_EVAL_ARTIFACT_BACKEND`; `local` remains the default.
`AGENT_EVAL_ARTIFACT_LOCAL_DIR` defaults to `<AGENT_EVAL_DATA_DIR>/artifacts`. The `s3` backend uses
`AGENT_EVAL_ARTIFACT_S3_BUCKET` plus optional `PREFIX`, `ENDPOINT_URL`, `REGION`, and
`FORCE_PATH_STYLE` settings. Credentials come only from boto3's standard AWS credential chain, such
as environment variables, container credentials, or instance roles. Stored database keys remain
relative POSIX paths; the configured S3 prefix is applied internally and does not change
`artifact_index.relative_path`.

Production PostgreSQL plus S3/MinIO backups are created with `deploy/backup-production.sh`. The
workflow writes a verified custom-format PostgreSQL dump and a verified indexed artifact archive,
then atomically publishes a backup-set manifest as its completion marker. Configure
`AGENT_EVAL_BACKUP_PATH` and see `deploy/runbook.md` for scheduling, retention, consistency, and
isolated restore rehearsal procedures. A local backup directory protects against logical failures,
not host loss; replicate complete sets to independent off-host storage.

Run the Web app:

```bash
pnpm dev:web
```

The Vite server is available at `http://localhost:5173` and proxies API calls to `http://localhost:8000`.

## Demo Dataset

Build the fixed 100-question demo dataset from pinned official sources, then import it:

```bash
uv run python -m agent_eval.maintenance.main build-demo-dataset
uv run alembic upgrade head
uv run python -m agent_eval.maintenance.main import-dataset data/demo-dataset --require-demo-distribution
```

The build contains C-Eval 30, MMLU-Pro 10, BrowseComp-ZH 20, BrowseComp 10,
BizFinBench 20, and FinQA 10. It enforces 70 Chinese and 30 English questions, the three
ability totals, and all three scoring question types. Source revisions, sampling seed, and file
hashes are recorded in `data/demo-dataset/manifest.json`.

`data/demo-dataset/` is intentionally ignored. BrowseComp's authors request that decrypted
examples not be published because doing so leaks the benchmark. Review each dataset's license
note before any commercial or public use; C-Eval and BizFinBench include noncommercial terms.

Running the import command again reuses versions with identical canonical content hashes. A
changed question set creates the next frozen version instead of mutating prior data.

Audit a source bundle before importing it. The `balanced-v2` policy targets five dimensions with
30 questions each, balanced equally across Chinese and English and using canonical
`easy`/`medium`/`hard` difficulty labels:

```bash
uv run python -m agent_eval.maintenance.main audit-dataset data/demo-dataset \
  --policy balanced-v2
```

Add `--enforce` in CI when the bundle is expected to satisfy the policy. Audit output is stable
JSON and includes logical content hashes, cross-dimension distributions, metadata coverage,
prompt-length buckets, and machine-readable violations.

Build the local-only 150-question candidate cohort from reviewed anchors and deterministic
original tasks:

```bash
uv run python -m agent_eval.maintenance.main build-balanced-v2
uv run python -m agent_eval.maintenance.main audit-dataset data/balanced-v2-candidate \
  --policy balanced-v2 --enforce
uv run python -m agent_eval.maintenance.main import-balanced-v2 data/balanced-v2-candidate
```

The candidate output is intentionally ignored because it includes restricted benchmark anchors.
Its manifest records source decisions, redistribution restrictions, deterministic answer checks,
and the canonical bundle hash. Generic imports remain restricted to released abilities in the
`legacy-v1` cohort. `import-balanced-v2` is the only candidate import path: it reruns the enforced
150-question audit, stores the isolated `balanced-v2` cohort, and creates or reuses the
20/20/20/20/20 score configuration. Deployment and bootstrap do not run this import automatically.

### Unified V3 Profiles

Once both frozen cohorts are complete, the API exposes `unified-v3` as the default ranking profile.
It is an immutable composition, not another dataset import: all 6 latest frozen `legacy-v1`
versions (100 questions) are combined with only the 7 language-specific dataset versions from the
5 balanced-v2 original benchmark families (75 questions). The 75 balanced anchors are not selected again, so the profile contains 175
distinct prompts: reasoning 45, retrieval 35, data reasoning 35, instruction following 30, and
long context 30.

The comprehensive profile and `unified-v3-<ability>` specialist profiles all use the
20/20/20/20/20 score configuration and the `unified-v3` protocol epoch. A comprehensive ability
result and its corresponding specialist result therefore share a metric protocol hash, while run
protocol hashes remain profile-specific. Legacy and balanced profiles remain available for
archival runs, but global leaderboards and run-status summaries only use ranking runs from the
unified-v3 profile family. Historical snapshots remain available through leaderboard history.

At cutover, archive any pre-unified current snapshot without deleting it:

```bash
uv run python -m agent_eval.maintenance.main archive-current-leaderboard
```

The command is idempotent. The next published `unified-v3` comprehensive run becomes current.
Ability scores are question-weighted means; benchmark-level breakdowns remain available.

Ranking publication validates each Agent result before it can enter a global leaderboard.
Authentication failures always invalidate a result, as do response-schema and client-configuration
failures affecting at least 20% of execution items. Timeouts, input limits, and other capability
failures remain valid zero-score evidence. Revalidate existing unified-v3 results after upgrading:

```bash
uv run python -m agent_eval.maintenance.main revalidate-ranking-results
```

The repair is idempotent, preserves snapshots, removes invalid global result rows, and selects the
newest comprehensive snapshot that still contains a valid result when current state needs repair.

Agent responses may report OpenAI-style `prompt_tokens`/`completion_tokens` or DashScope-style
`input_tokens`/`output_tokens` under `usage`. Valid nonnegative counts are persisted per HTTP
attempt and summed in published run and global leaderboard metrics; historical or malformed usage
remains unknown rather than being estimated. `token_usage_available_count` counts attempts with a
complete input/output/total tuple, while `token_usage_missing_count` counts terminal attempts with
partial or absent usage. Published latency p50 and p95 values use the deterministic nearest-rank
method over terminal attempts that have a latency sample. Monetary cost is not estimated.

For citation-required questions, configured citation JSONPath values remain preferred. If that
mapping yields no valid URLs, up to three distinct HTTP/HTTPS URLs embedded in the extracted answer
are used as a fallback. Ordinary answer text is never treated as a citation.

## Verification

```bash
uv run ruff check .
uv run mypy
uv run pytest
pnpm lint:web
pnpm test:web
pnpm build:web
```

## Docker Compose

```bash
docker compose -f deploy/compose.yaml up --build
```

An optional private MinIO overlay is available for S3-compatible deployments. It is not part of the
default or current production stack and publishes no MinIO ports:

```bash
cp .env.minio.example .env.minio
# Replace all credential placeholders first.
docker compose --env-file .env.minio \
  -f deploy/compose.yaml -f deploy/compose.minio.yaml up --build
```

Open `http://localhost:8080`. Run the maintenance check with:

```bash
docker compose -f deploy/compose.yaml --profile tools run --rm maintenance
```

Bootstrap the fixed dataset and default judge/score configuration with:

```bash
docker compose -f deploy/compose.yaml --profile tools run --build --rm maintenance bootstrap --dataset /datasets/demo
```

The base Compose file is for local development and binds plain HTTP. Production uses the separate
fail-closed overlay, read-only Secret files, a host data path, resource/log limits, and loopback-only
Web binding. See `deploy/runbook.md` and use `deploy/manage.sh`; do not expose the base Compose stack
to the Internet or commit `.env`, dataset contents, passwords, Agent Tokens, or API keys.
