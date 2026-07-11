# Security and trust model

## The trust model, stated openly

`.themis/review.md` (the review doctrine) is read from the PR's own branch,
not the default branch. This is deliberate: it lets the doctrine reference
the actual code the PR touches. But it means a malicious PR can rewrite its
own review instructions. Themis does not try to sandbox the doctrine's
content; instead, the guardrails below hold even against a fully
adversarial doctrine.

## Bot-side guardrails

These hold regardless of what `review.md` says, because they sit outside
the agent's reach:

- **No GitHub access from codex.** The `codex exec` subprocess never sees a
  GitHub token. Cloning and posting happen in Themis's own process, before
  and after the codex run.
- **Env allowlist.** codex runs with an explicit allowlist of environment
  variables (`PATH`, `HOME`, `CODEX_HOME`, locale and proxy variables);
  none of Themis's own secrets, GitHub App key, webhook secret, API token,
  are in its environment. See `../src/themis/codex.py`.
- **Findings filtered to the diff.** Inline findings that land outside the
  PR's actual changed files are dropped before posting, and folded into the
  summary as unposted instead of silently discarded.
- **Thread resolution restricted to bot-authored threads.** Codex can ask
  to resolve a review thread; Themis only honors that for threads it
  authored itself, never a human's.
- **Clone URLs are token-scrubbed.** The installation token lives only in
  the fetch URL argv, never written to `.git/config`; `FETCH_HEAD` and
  `.git/logs` (which can retain it) are deleted right after clone, and any
  token substring reaching command output or logs is scrubbed.
- **Workspaces don't persist.** Each job's clone is deleted when the job
  ends, success or failure, and a stale-workspace sweep runs at the start
  of every job as a crash safety net.

## Webhook verification

`POST /webhook` requires a valid `X-Hub-Signature-256` HMAC-SHA256 over the
raw request body, keyed with `THEMIS_GH_WEBHOOK_SECRET`, checked with a
constant-time comparison. Deliveries that fail verification get `401` and
are never parsed or enqueued.

## Trigger API authentication

`/api/review` and `/api/discuss` require `Authorization: Bearer
$THEMIS_API_TOKEN`, also compared constant-time. Missing or wrong token:
`401`. The endpoints answer `404` unless `THEMIS_API_TOKEN` is set. See
[`docs/headless.md`](headless.md).

## Sandbox modes

`codex exec` runs under `--sandbox $THEMIS_CODEX_SANDBOX`:

- `workspace-write` (default): codex can write inside the workspace only,
  enforced by the kernel (Landlock or equivalent namespace support).
- `danger-full-access`: no kernel-level sandboxing from codex itself. Use
  this on container runtimes that don't support Landlock (some managed
  PaaS). In this mode the container is the isolation boundary instead of
  codex, so run Themis in its own container with nothing sensitive mounted
  alongside it.

## Single-tenant by design

One `CODEX_HOME` volume holds one `auth.json`, which is one Codex
subscription. Themis is built for one instance per person or team: its own
GitHub App, its own subscription. Running multiple unrelated teams against
a shared instance isn't supported: usage quota and credential blast radius
aren't isolated between them. Run one instance per subscription.
