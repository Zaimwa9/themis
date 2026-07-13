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

- **No GitHub credentials in the agent container.** The controller clones and
  posts. The separate agent container receives only the shared temporary
  workspace, prompt, and engine credential, and has its own PID namespace.
- **Env allowlist.** Each engine runs with an explicit allowlist of
  environment variables (`PATH`, `HOME`, locale and proxy variables, plus
  `CODEX_HOME` for codex, `CLAUDE_CODE_OAUTH_TOKEN` for claude, or the
  provider key for glm/qwen (crossing over only as `ANTHROPIC_AUTH_TOKEN`,
  with the endpoint baked into the adapter so no env or repo config can
  redirect it)); none of
  Themis's own secrets, GitHub App key, webhook secret, API token, are in
  its environment. See `../src/themis/engines/base.py` and "Engine secret
  reachability and outbound redaction" below.
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

## Engine secret reachability and outbound redaction

The agent subprocess runs on untrusted PR content inside the agent container. Its environment is
allowlisted per engine: codex sees only `CODEX_HOME` beyond the base set and
runs with `--ignore-user-config --ignore-rules`, so it authenticates from
`auth.json` without loading worker config or repo `.rules` files; claude sees
`CLAUDE_CODE_OAUTH_TOKEN` plus non-secret hygiene flags; glm and qwen see
their provider key as `ANTHROPIC_AUTH_TOKEN` plus the same hygiene flags.
The agent container
never receives the GitHub App key, webhook secret, or API token. It receives
only `THEMIS_AGENT_TOKEN`, which grants execution access but no GitHub access.

A hostile PR can still instruct the agent to print secrets it legitimately
holds (its own subscription credential) into the review output. Every body
Themis posts to GitHub (findings, summaries, replies, status comments)
passes through an outbound redaction step that removes exact values of
instance secrets and credential-shaped strings (`sk-ant-*`,
`gho_/ghp_/ghs_/ghu_*`, `github_pat_*`, JWTs) before leaving the instance.
Agent output tails are redacted at source: the diagnostic tail logged when
the agent's result files can't be parsed, and the tails embedded in engine
error messages (failed attempts, job-failure tracebacks), all pass through
the same redaction before they can reach a log line. Treat worker logs as
sensitive anyway; redaction is a backstop, not a license to ship logs.

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

## Claude-harness engine sandbox posture (claude, glm, qwen)

The codex engine runs under codex's own kernel sandbox (`workspace-write`
by default, network denied). The claude, glm, and qwen engines have no kernel
sandbox: they run with permissions skipped, and their dedicated container is
the isolation boundary (non-root user, allowlisted env, scrubbed clone). All
three run in safe mode, with filesystem setting sources disabled, a strict
empty MCP configuration, auto-memory off, and an isolated config directory,
so repo-controlled `CLAUDE.md`, hooks, plugins, skills, agents, and MCP
servers are not loaded. By default their
`WebFetch`/`WebSearch` tools are also disabled, but Bash remains available,
so a prompt-injected job could still exfiltrate over the network.
The only secret in reach is the engine's own key (your Claude token, or the
glm/qwen provider key); it's on the
outbound-redaction list above so it never reaches a GitHub-facing body, and
the claude token is rotatable with `claude setup-token`. Repos opt into Claude's built-in
web tools per repo with `web_access: true` (default-branch controlled);
deployments with strict requirements should route the agent through an egress
proxy allowlisting Anthropic's required endpoints. Container separation
protects GitHub credentials but does not itself restrict outbound networking.

## Single-tenant by design

One `CODEX_HOME` volume holds one `auth.json`, and one
`CLAUDE_CODE_OAUTH_TOKEN` value is one Claude token, and one
`GLM_API_KEY`/`QWEN_API_KEY` is one coding-plan subscription: each engine ties the
instance to one subscription. Themis is built for one instance per person
or team: its own GitHub App, its own subscription. Running multiple
unrelated teams against a shared instance isn't supported: usage quota and
credential blast radius aren't isolated between them. Run one instance per
subscription.
