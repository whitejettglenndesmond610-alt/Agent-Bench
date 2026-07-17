# 10-Minute Demo Script

## 0:00-1:00 Access And Roles

- Open the HTTPS platform from an allowlisted network.
- Show operator login and explain the separate read-only password.
- Confirm production metadata and readiness without displaying Secret values.

## 1:00-3:00 Agent Integration

- Open Agent management.
- Show the two configured Agent identities and immutable versions.
- Demonstrate request template, answer JSONPath, citation JSONPath, timeout, and concurrency.
- Run one connection test. Never expose the Bearer Token or raw Authorization header.

## 3:00-5:30 Evaluation Run

- Create a run with the two Agent versions and fixed profile.
- Show queue state, per-Agent totals, successes, failures, retries, and cancellation control.
- Use the prepared 20-question smoke run if live third-party latency is uncertain.

## 5:30-7:30 Latest Leaderboard

- Open the latest officially published snapshot.
- Switch between overall and three ability rankings.
- Explain answer quality, citation quality, success rate, anomaly rate, and HTTP calls.
- State clearly that HTTP calls are displayed but never affect rank.

## 7:30-9:00 Agent Comparison

- Select both Agents and open the same-snapshot comparison.
- Show the fixed 0-100 radar chart, Chinese/English scores, Benchmark results, and versions.
- Explain why cross-snapshot comparison is rejected.

## 9:00-10:00 Recovery And Read-Only View

- Show the prepared worker-restart recovery evidence and backup verification output.
- Log in with the read-only role and confirm Agent/run operations are inaccessible.
- If an external API is unavailable, open the current fallback snapshot and finish the result demo.

## Before The Meeting

- Run `deploy/manage.sh preflight --require-snapshot`.
- Verify both Agent connection tests.
- Keep the fallback snapshot ID and report available.
- Confirm TLS certificate expiry and the presenter source IP.
- Do not rotate Agent Tokens, encryption key, or passwords during the demo window.
