# Production Runbook

## Deployment Boundary

The Compose stack binds Web to `127.0.0.1:8080`. Production TLS and source IP filtering must be
provided by a cloud load balancer, VPN ingress, or a host reverse proxy. Do not publish port 8080
to the public Internet.

Required controls:

- TLS 1.2 or newer with a valid certificate for the platform FQDN.
- HTTP redirected to HTTPS and HSTS enabled at the TLS terminator.
- Cloud security group or firewall allowing only approved company/VPN CIDRs.
- The load balancer is the only process allowed to reach `127.0.0.1:8080`.
- Record the firewall rule IDs and allowed CIDRs in `deploy/day19-report.md`.

## Host Preparation

Target: one mainland Linux host with at least 4 vCPU, 8 GB RAM, and a local ext4/xfs data disk.

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /srv/agent-eval/data
sudo install -d -o root -g root -m 0700 /etc/agent-eval/secrets
sudo install -d -o 10001 -g 10001 -m 0700 /srv/agent-eval/demo-dataset
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0700 /srv/agent-eval/backups
sudo cp -a data/demo-dataset/. /srv/agent-eval/demo-dataset/
cp .env.production.example .env.production
```

Verify the data disk is local and has sufficient capacity:

```bash
findmnt -T /srv/agent-eval/data -no SOURCE,FSTYPE,OPTIONS
df -h /srv/agent-eval/data
df -i /srv/agent-eval/data
stat -c '%a %u:%g %n' /srv/agent-eval/data /etc/agent-eval/secrets
```

Do not use NFS for SQLite or Artifact storage.

## PostgreSQL Multi-Worker Deployment

The default Compose files continue to use SQLite and one worker. To run multiple worker processes,
use PostgreSQL and the PostgreSQL overlay:

```bash
cp .env.postgres.example .env.postgres
# Set POSTGRES_PASSWORD and the normal production paths/secrets before starting.
docker compose --env-file .env.postgres \
  -f deploy/compose.yaml -f deploy/compose.production.yaml \
  -f deploy/compose.postgres.yaml up --build -d --scale worker=2
```

`migrate` waits for the PostgreSQL healthcheck and must complete before the API starts. For managed
PostgreSQL, set `AGENT_EVAL_DATABASE_URL` to a `postgresql+asyncpg://` URL and retain the overlay so
all services receive the same URL. Keep the database pool settings conservative: total possible
connections are approximately `(api processes + worker replicas) * (pool size + max overflow)`.
Each request-slot acquisition, heartbeat, and release briefly checks out a connection; polling
never keeps a connection checked out while waiting.

`AGENT_EVAL_WORKER_MAX_ACTIVE_RUNS` is the database-wide active-run limit.
`AGENT_EVAL_WORKER_MAX_LOCAL_RUNS` limits one worker replica; set it to `1` when distributing work
across two or more replicas. The provider limit remains global.
`AGENT_EVAL_WORKER_MAX_LOCAL_WORKSPACE_ATTEMPTS` separately limits workspace generations in each
replica and defaults to `2`.
Retiring an Agent version intentionally blocks new calls for conversations pinned to it. Repin each
conversation through the workspace PATCH API to an active version of the same Agent after active
attempts finish; repinning preserves all prior turns and increments the conversation revision.

With PostgreSQL, `AGENT_EVAL_WORKER_AGENT_CONCURRENCY`,
`AGENT_EVAL_DEEPSEEK_CONCURRENCY`, `AGENT_EVAL_CITATION_FETCH_CONCURRENCY`, and
`AGENT_EVAL_CITATION_DOMAIN_CONCURRENCY` are deployment-wide limits shared by every API and worker
replica. Do not divide or multiply them by replica count. SQLite keeps the same process-local limits
as before. Active calls renew their leases every third of
`AGENT_EVAL_RESOURCE_LEASE_SECONDS`; `AGENT_EVAL_RESOURCE_LEASE_POLL_SECONDS` is the initial
bounded-backoff polling interval. Lower lease values increase database traffic and should only be
used in tests. Citation domain slots use the canonical IDNA host and are shared across runs.

### SQLite-to-PostgreSQL Cutover

Use a maintenance window. The migration command requires explicit URLs and does not read the
current application database setting. It accepts only `sqlite+aiosqlite` as the source and
`postgresql+asyncpg` as the target. Keep URL values in the approved secret mechanism and do not
paste them into tickets, logs, or reports.

1. Announce the maintenance window and stop every API and worker process. Confirm no process can
   write to SQLite. The command refuses active run slots and queued/running workspace attempts, and
   requires the `worker_leases` table to have no rows, including expired leases.
2. Take and verify an SQLite online backup while the application remains stopped. Also take a
   separate, consistent backup or snapshot of the complete Artifact storage directory. Record the
   backup filenames, sizes, and hashes, but not database URLs or secret values.

   ```bash
   AGENT_EVAL_DATABASE_URL="$SOURCE_URL" \
     uv run python -m agent_eval.maintenance.main backup
   AGENT_EVAL_DATABASE_URL="$SOURCE_URL" \
     uv run python -m agent_eval.maintenance.main verify-backup
   rsync -a "$ARTIFACT_DIRECTORY/" "$ARTIFACT_BACKUP_DIRECTORY/"
   ```

3. Create a new, dedicated, empty PostgreSQL database and initialize its schema at the application
   head. Do not point an API or worker at it yet.

   ```bash
   AGENT_EVAL_DATABASE_URL="$TARGET_URL" uv run alembic upgrade head
   ```

4. Run validation and source inventory without writing the target. Review the per-table counts and
   SHA-256 fingerprints. URLs are accepted as arguments but are never included in command output.
   Expired `resource_leases` rows are ignored; an unexpired row refuses migration.

   ```bash
   uv run python -m agent_eval.maintenance.main migrate-database \
     --source-url "$SOURCE_URL" --target-url "$TARGET_URL" \
     --batch-size 1000 --dry-run
   ```

5. Run the migration with the same URLs and batch size. The target is locked, rechecked as empty,
   copied in one transaction with PostgreSQL constraints enabled, and verified by row count and
   typed content fingerprint before and after commit.

   ```bash
   uv run python -m agent_eval.maintenance.main migrate-database \
     --source-url "$SOURCE_URL" --target-url "$TARGET_URL" \
     --batch-size 1000
   ```

6. Compare the successful migration report with the dry-run report. Every copied table count and
   hash must match. `worker_leases` and `resource_leases` are deliberately not copied; workers
   establish fresh leases after cutover. Auth users and completed workspace conversations,
   messages, and attempts are business history and are copied. The two deterministic shared-user
   rows created by a fresh target migration are transactionally replaced by the source identity
   rows. If the command fails, keep the target offline and investigate rather than trying
   to use partial data. Copy and verification failures roll back the target; an unexpected
   post-commit verification failure attempts an immediate guarded cleanup and reports if cleanup
   cannot be completed.
7. Update the API and worker deployment secret/configuration to `TARGET_URL` only after successful
   verification. Keep Artifact storage pointed at the restored or original shared directory, start
   the API first, and check readiness and authenticated read paths.
8. Start one worker, confirm it acquires a fresh lease, and run a smoke evaluation. Then restore the
   intended worker replica count and monitor API, worker, PostgreSQL, and artifact errors.
9. Retain the immutable SQLite backup, the stopped original SQLite database (including any WAL/SHM
   files), the Artifact backup, and the encryption-key recovery material for the approved rollback
   period. Rollback means stopping all PostgreSQL writers and restoring the retained SQLite and
   Artifact snapshots together; never merge writes from both databases.

Both databases must report Alembic head `d4e1f6a7b8c9`. The target must have a schema matching the
current SQLAlchemy model metadata and no rows beyond its two deterministic shared AuthUser seeds.
There is intentionally no
general-purpose command to clear a migration target; recreate a disposable target when rehearsing.

## Local-to-S3 Artifact Cutover

Use a maintenance window. The S3 adapter uses conditional `If-None-Match: *` writes and never
overwrites an object. The database continues to store the same relative keys; the bucket and prefix
exist only in runtime configuration. AWS credentials must be supplied through the standard SDK
credential chain and must not be placed in command arguments.

1. Create a dedicated private bucket. Block public access and verify there is no public bucket or
   access-point policy. Grant the API, worker, and maintenance identity only the required object and
   bucket operations for the configured prefix. For MinIO, copy `.env.minio.example`, replace every
   placeholder, and add `deploy/compose.minio.yaml` only when intentionally enabling that stack.
2. Stop every API and worker writer. Take a coordinated PostgreSQL snapshot or `pg_dump` and a
   complete local artifact-directory snapshot. Keep both under one checkpoint identifier. If also
   migrating SQLite to PostgreSQL, perform the SQLite backup and database migration steps above
   while writers remain stopped; do not start against PostgreSQL yet.
3. Export standard AWS credentials through the approved secret mechanism. Validate all indexed
   local objects without writing S3:

   ```bash
   uv run python -m agent_eval.maintenance.main sync-artifacts-to-s3 \
     --source-local-dir "$ARTIFACT_DIRECTORY" \
     --target-bucket "$ARTIFACT_BUCKET" --target-prefix "$ARTIFACT_PREFIX" \
     --target-region "$AWS_REGION" --concurrency 8 --dry-run
   ```

   Add `--target-endpoint-url "$S3_ENDPOINT" --target-force-path-style` only for the approved
   S3-compatible endpoint. Command output contains counts only. Any missing or corrupt indexed local
   object fails the command nonzero.
4. Run the same command without `--dry-run`. A retry verifies pre-existing target objects against
   the database hash and size before counting them as successful. Partial copies are deliberately
   retained after a failure and are safe to retry. The source is never deleted.
5. Run the copy command once more and require `copied=0`, `existing=total`, and `pending=0`. Keep
   writers stopped. If PostgreSQL was migrated in the same window, verify its migration report now.
6. Update API, worker, and maintenance configuration together to `AGENT_EVAL_ARTIFACT_BACKEND=s3`
   with exactly the same bucket, prefix, endpoint, region, and addressing mode. When combining the
   database and artifact migration, switch the PostgreSQL URL in this same deployment revision.
7. Start the API only and require readiness to pass its private create/delete probe. Verify an
   authenticated historical artifact read, then start one worker and run a smoke evaluation. Scale
   workers only after the new artifact row and object have both been verified.
8. Keep the stopped local directory and the pre-cutover database/object checkpoint unchanged for
   the rollback period. Do not delete the local source after cutover validation.

Rollback before accepting new writes is: stop all services, restore or select the pre-cutover
database checkpoint, switch all three process types back to the matching local artifact snapshot,
then start API followed by one worker. After new S3-backed writes have been accepted, switching only
the backend to local is unsafe because the local snapshot lacks those objects. Stop writers and
restore a coordinated database plus object checkpoint, accepting the documented recovery point, or
keep PostgreSQL/S3 and roll back only application code. Never combine a newer database with an older
artifact snapshot.

S3 versioning is recommended where supported, but delete creates delete markers and noncurrent
versions still consume capacity. Test recovery of both live objects and delete markers. Lifecycle
rules must not expire current objects independently of `artifact_index`; use the maintenance cleanup
path for normal retention and apply lifecycle only to reviewed noncurrent versions, abandoned probe
objects, or a separately scoped prefix. Object Lock changes delete semantics and must be tested
before use. Bucket replication/versioning is not a backup by itself: maintain an independent,
tested bucket backup and a coordinated PostgreSQL backup, and monitor restore time and storage cost.

## Secret Files

Create exactly these files under `/etc/agent-eval/secrets`:

```text
read-only-password-hash
operator-password-hash
encryption-key
deepseek-api-key
```

Generate both random platform passwords, their Argon2 hashes, and the encryption key locally:

```bash
sudo uv run python -m agent_eval.maintenance.main init-secrets /etc/agent-eval/secrets
```

The command creates distinct random passwords, writes only their hashes to runtime Secret files,
and places the plaintext passwords in a sibling `secrets-credentials.txt` file with mode `0600` for
one-time handoff. Make the runtime directory and its three files readable only by container UID
10001 (`chown -R 10001:10001 /etc/agent-eval/secrets` and modes `0700`/`0400`). Add
the real DeepSeek key as `deepseek-api-key`, one value with no quotes, also mode `0600`. Store a
recovery copy of `encryption-key` in the approved secret manager; losing it makes saved Agent
Bearer Tokens unrecoverable. Delete `credentials.txt` after the approved recipient has stored both
passwords in the password manager. The plaintext credential handoff file is not mounted into any
container.

## First Deployment

```bash
chmod 755 deploy/manage.sh
deploy/manage.sh start
deploy/manage.sh bootstrap
deploy/manage.sh preflight
```

`bootstrap` verifies every dataset file against `manifest.json`, imports exactly 100 questions,
and idempotently creates the fixed `deepseek-v4-flash` judge and `40/35/25` score configuration.
Preflight intentionally fails until two enabled Agent versions have been configured through the
operator UI and their connection tests pass.

After the first successful publication, require a fallback snapshot:

```bash
deploy/manage.sh preflight --require-snapshot
```

## tau2 Interactive Evaluation Data

The `tau2-retail` (114 tasks), `tau2-telecom` (114), and `tau2-airline` (50) profiles are
optional. The worker image already ships the `tau2` dependency group; only the data directory
needs provisioning. On the host that owns the data volume:

```bash
git clone --depth 1 --branch v1.0.1 https://github.com/sierra-research/tau2-bench.git /tmp/tau2-src
mkdir -p "$AGENT_EVAL_DATA_PATH/tau2/tau2/domains"
for domain in retail telecom airline; do
  cp -r "/tmp/tau2-src/data/tau2/domains/$domain" "$AGENT_EVAL_DATA_PATH/tau2/tau2/domains/"
done
chown -R 10001:10001 "$AGENT_EVAL_DATA_PATH/tau2"
rm -rf /tmp/tau2-src
```

The compose worker sets `TAU2_DATA_DIR=/data/tau2` (overridable via the `TAU2_DATA_DIR` env in
`.env.production`), so the layout above resolves to `/data/tau2/tau2/domains/retail` inside the
container. Then import and verify:

```bash
for domain in retail telecom airline; do
  docker compose --env-file .env.production --profile tools \
    -f deploy/compose.yaml -f deploy/compose.production.yaml \
    run --build --rm maintenance import-tau2 --domain "$domain"
done
```

telecom tasks attach user tools to the simulator (assembled the same way as tau2's official
runner); retail and airline have none. The user simulator calls DeepSeek (`deepseek/deepseek-chat`)
and shares the judge concurrency
limit, so the existing `deepseek-api-key` secret is the only credential required. Importing with
`--limit` is for development only: each profile stays hidden until its full task count is present.
Do not commit or redistribute the tau2 data directory.

## MCPMark Filesystem (MCP) Evaluation Data

The `mcp-filesystem` profile (MCPMark filesystem easy suite, 10 tasks) is optional and never
publishes to leaderboards. The worker image already ships the `mcp` dependency group, Node.js 24
and a warm npx cache for `@modelcontextprotocol/server-filesystem@2025.12.18`; only the pinned
MCPMark checkout needs provisioning. On the host that owns the data volume:

```bash
git clone https://github.com/eval-sys/mcpmark.git /tmp/mcpmark-src
cd /tmp/mcpmark-src && git checkout cd45b7f57923b9b3985467f5139927575f83141c
mkdir -p "$AGENT_EVAL_DATA_PATH/mcpmark"
cp -r /tmp/mcpmark-src/tasks "$AGENT_EVAL_DATA_PATH/mcpmark/tasks"
cd /tmp/mcpmark-src && git rev-parse HEAD > "$AGENT_EVAL_DATA_PATH/mcpmark/.mcpmark-commit"
chown -R 10001:10001 "$AGENT_EVAL_DATA_PATH/mcpmark"
rm -rf /tmp/mcpmark-src
```

Only the `tasks/` tree plus the `.mcpmark-commit` pin file are copied (the importer pins the
commit into `source_version`, so the dataset content hash moves with the checkout). The compose
worker and maintenance services set `MCPMARK_REPO_PATH=/data/mcpmark` (overridable via
`.env.production`). Then import and verify:

```bash
docker compose --env-file .env.production --profile tools \
  -f deploy/compose.yaml -f deploy/compose.production.yaml \
  run --build --rm maintenance import-mcpmark-filesystem --suite easy
```

At episode time the worker downloads each task's initial-state zip from `meta_data.stateUrl`
(storage.mcpmark.ai) on first use and caches it under `$AGENT_EVAL_DATA_DIR/mcpmark-states`;
when meta.json carries a sha256/size pin it is enforced. The episode runs the pinned npm
filesystem server over stdio with the per-episode workspace as its only allowed directory, and
scores with the task's `verify.py` (exit 0 = reward 1). `verify.py` is external MCPMark code: it
executes as the worker user, so only ever provision a checkout you pinned deliberately. The
standard suite (30 tasks) is intentionally out of scope until M2; importing with `--limit` is for
development only. Do not commit or redistribute the MCPMark checkout.

## Local Fake Rehearsal

The demo overlay is local-only and must never be used for an official ranking. It starts two
deterministic Agent response formats and a DeepSeek-compatible Fake Judge. Initialize a mode `0600`
demo env file containing only password hashes and the local encryption key, then run:

```bash
uv run python -m agent_eval.maintenance.main init-demo-env \
  /path/to/runtime-secrets /path/to/demo.env
docker compose --env-file /path/to/demo.env --profile demo \
  -f deploy/compose.yaml -f deploy/compose.demo.yaml up --build -d
docker compose --env-file /path/to/demo.env --profile demo --profile tools \
  -f deploy/compose.yaml -f deploy/compose.demo.yaml run --build --rm maintenance \
  bootstrap --dataset /datasets/demo
docker compose --env-file /path/to/demo.env --profile demo --profile tools \
  -f deploy/compose.yaml -f deploy/compose.demo.yaml run --rm maintenance run-fake-suite
```

The suite runs 2 Agents x 20 questions followed by 2 Agents x 100 questions and prints machine-
readable reports. Preserve the resulting current snapshot as the local fallback demonstration.

## Routine Operations

```bash
deploy/manage.sh status
deploy/manage.sh logs api worker web
deploy/manage.sh backup
deploy/manage.sh verify-backup
deploy/manage.sh cleanup-artifacts
deploy/manage.sh stop
```

The legacy `deploy/manage.sh backup` and `verify-backup` commands are SQLite-only. Keep those
database files and local artifact-directory rollback snapshots separate from PostgreSQL backup
sets.

For PostgreSQL with S3/MinIO, set the absolute host `AGENT_EVAL_BACKUP_PATH` and run:

```bash
deploy/backup-production.sh /absolute/path/to/.env.production
```

The script does not stop, restart, or reconfigure services. It takes the PostgreSQL custom-format
dump first, validates it with `pg_restore --list`, exports every indexed object through the
configured ArtifactStore, verifies the archive, hashes both files, and finally atomically writes
`backup-set-<id>.json`. Only sets with that completion manifest are successful. The default
`AGENT_EVAL_BACKUP_RETENTION_DAYS=14` removes old complete sets but always retains the newest
successful set. Incomplete current files are never retention candidates. `flock` prevents overlap.

This ordering gives DB-first then immutable-artifact-superset consistency. Artifact object creation
precedes insertion of its `artifact_index` row, and objects are immutable/create-only. Therefore an
artifact export taken after the database snapshot can include newer unreferenced objects, while all
objects referenced by the database snapshot already existed. Unreferenced extras are harmless on
restore. Do not run `cleanup-artifacts` concurrently: PostgreSQL export and the built-in cleanup
coordinate with an advisory lock and reject overlap, but operators must also exclude external S3
lifecycle deletion and older application versions that do not use this lock. SQLite has no
cross-process cleanup detector and requires explicit scheduling exclusion.

The archive manifest contains only relative paths, kinds, sizes, and SHA-256 values, never object
bodies or credentials. Object bodies are separate tar members. Export is atomic and refuses to
replace an existing output after any missing or corrupt source object. Restore is create-only,
accepts existing objects only after full size/hash verification, never writes the database, and
never deletes target objects.

Install the daily user timer without embedding secrets in a unit:

```bash
deploy/systemd/install-backup-timer.sh \
  /absolute/path/to/repository /absolute/path/to/.env.production
systemctl --user list-timers agent-eval-backup.timer
journalctl --user -u agent-eval-backup.service
```

The installer stores only repository and durable environment-file paths in
`~/.config/agent-eval/backup.conf`. The timer uses `Persistent=true` and a randomized delay. Enable
user lingering where unattended user timers require it. Monitor failed units and journal output.

Install the local operational monitor with the same durable production environment file:

```bash
deploy/systemd/install-monitor-timer.sh \
  /absolute/path/to/repository /absolute/path/to/.env.production
systemctl --user status agent-eval-monitor.timer agent-eval-monitor.service
journalctl --user -u agent-eval-monitor.service --since today
```

The timer runs every five minutes with `Persistent=true` and a small randomized delay. It checks
the loopback readiness URL, expected Compose service state and health, safe database aggregates,
ArtifactStore writability, and the newest complete backup set's age and component hashes. Configure
thresholds and `AGENT_EVAL_BACKUP_MAX_AGE_HOURS` in the durable environment file. Failures remain
visible in `systemctl --user --failed` and the user journal; the monitor never restarts services.
Workspace aggregates include queue depth and age, running attempts, recent failures, and stale
leases. Configure `AGENT_EVAL_MONITORING_MAX_QUEUED_WORKSPACE_ATTEMPTS`,
`AGENT_EVAL_MONITORING_MAX_WORKSPACE_QUEUE_AGE_MINUTES`, and
`AGENT_EVAL_MONITORING_MAX_RECENT_FAILED_WORKSPACE_ATTEMPTS`; any stale workspace lease is an alert.
External notifications or webhooks are a later optional integration and are not sent by this local
monitor.

Replicate each complete database dump, artifact archive, and backup-set manifest to an independent
remote/off-host destination after completion. A same-machine backup path does not protect against
host, disk, account, ransomware, or site loss. S3 versioning or replication of the live artifact
bucket is useful but is not a substitute for independently retained, tested backup sets.

Artifact storage defaults to `AGENT_EVAL_ARTIFACT_BACKEND=local`. Keep
`AGENT_EVAL_ARTIFACT_LOCAL_DIR` on storage shared by every API, worker, and maintenance process;
the Compose default is `/data/artifacts`. For `s3`, all processes must use the same bucket and
prefix, while credentials should come from container/instance roles or standard AWS environment
variables. Readiness performs a private create/delete probe through the configured adapter. Unknown
backends and invalid backend-specific combinations fail configuration validation.

For a managed PostgreSQL service, adapt the dump transport to its approved tooling while retaining
the same DB-first ordering, validation, completion marker, and artifact-cleanup exclusion. A
PostgreSQL volume alone is not a backup.

## Safe Upgrade

```bash
deploy/manage.sh upgrade
```

For SQLite, the upgrade command stops all application writers, creates and verifies a database
backup, then runs migrations and starts the new stack. Before invoking it, block new workspace
submissions at the reverse proxy and let workers finish or cancel all active attempts. Do not use an
uncoordinated `compose up` for a populated production database.

For PostgreSQL, first block new API submissions while leaving the current API and workers available
to drain or cancel active workspace attempts. Then stop or scale all API/worker writers to zero,
take a verified database plus Artifact backup set, run the one-shot `migrate` service, and restore
the desired worker scale. The ownership migration intentionally fails while any evaluation run has
an active slot.

The workspace-comparison migration is a coordinated point of no return once comparison traffic is
accepted. Older workers cannot execute `workspace-compare-v1` attempts, and an Alembic downgrade
would discard comparison grouping and curated evaluation cases. Do not perform an image-only
rollback. Block new submissions, drain or cancel active attempts while the current workers are still
running, and only then stop the stack. Restore the matching pre-upgrade checkpoint as a unit: the
SQLite database plus Artifact archive for SQLite, or the PostgreSQL dump plus Artifact archive for
PostgreSQL. Start the matching older images only after the restore verifies successfully. Rehearse
this restore path before production rollout.

## Restore

Restore replaces the SQLite database and is only allowed while the stack is stopped:

```bash
deploy/manage.sh verify-backup
deploy/manage.sh restore
deploy/manage.sh start
deploy/manage.sh preflight --require-snapshot
```

Artifact objects are not embedded in the SQLite backup; it contains only their index rows. Preserve
the configured local artifact directory (host default `/srv/agent-eval/data/artifacts`) with disk
snapshots or a separate backup policy and restore it consistently with the database. Cleanup treats
an already-missing object as success. It commits each removed index row separately; a crash after
object deletion but before that commit can temporarily leave a stale row, which the next cleanup
removes idempotently.

### PostgreSQL Restore Rehearsal

Never restore destructively into the production project or database. Prepare a separate rehearsal
environment file with a unique `AGENT_EVAL_REHEARSAL_PROJECT=agent-eval-rehearsal-<name>`, the
backup directory, and an isolated artifact destination. For S3/MinIO, use a dedicated empty bucket
or prefix and credentials that cannot write production. Then run:

```bash
deploy/restore-rehearsal.sh /absolute/path/to/rehearsal.env \
  /srv/agent-eval/backups/backup-set-YYYYMMDDTHHMMSSZ-N.json
```

The script refuses an existing Compose project, creates a new PostgreSQL volume, restores the dump
into its empty database, restores artifacts create-only, runs migrations and maintenance checks,
and leaves API, worker, and web startup manual. Inspect counts and application read paths before
manually removing the isolated project. A production recovery must remain a reviewed operator
procedure: stop all writers, provision an empty replacement database and empty artifact target,
repeat the verified restore steps, run `alembic upgrade head` and checks, then switch configuration
and start services manually. Never point the rehearsal script at production or automate destructive
in-place production restore.

## Release Checks

From an approved source:

```bash
curl -fsSI https://PLATFORM_FQDN/
curl -fsS https://PLATFORM_FQDN/health/ready
openssl s_client -connect PLATFORM_FQDN:443 -servername PLATFORM_FQDN </dev/null
```

Repeat the HTTPS request from a source outside the allowlist. It must time out at the firewall or
receive an explicit deny response. Record both results and the certificate expiry in the report.
