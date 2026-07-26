# GitHub Action mode

Run Themis as a GitHub Action: the same engine + prompt pipeline as the
service, with zero infrastructure. No server, tunnel, GitHub App, or
webhook — a workflow runs the review on GitHub's runners and posts it with
the workflow's own `GITHUB_TOKEN`.

## Setup

1. Copy [`examples/github-actions/themis-review.yml`](../examples/github-actions/themis-review.yml)
   to `.github/workflows/themis-review.yml` in the repository you want
   reviewed.
2. Add the engine credential as a repository (or organization) secret:

   | Engine | Secret | Where it comes from |
   |---|---|---|
   | `claude` (default) | `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` (Claude Max) |
   | `codex` | pass via `codex-auth-json` input | content of a dedicated `~/.codex/auth.json` |
   | `glm` | `GLM_API_KEY` | Z.ai GLM Coding Plan |
   | `kimi` | `KIMI_API_KEY` | Moonshot platform (pay-as-you-go) |
   | `openrouter` | `OPENROUTER_API_KEY` | OpenRouter credits |

That's it. New non-draft PRs get a review; commenting
`@themis review` re-reviews (works on drafts too, optionally with steering
context after the command: `@themis review focus on the auth changes`);
`@themis <question>` on the PR or a reply inside a Themis finding thread
gets a discussion answer. The mention keyword is plain trigger text, not a
real GitHub login — nobody gets pinged, and it is configurable via the
`mention` input.

Reviews are posted by `github-actions[bot]`. To post under a dedicated
account instead, pass a PAT as `github-token` and set `bot-login` to that
account's login.

## How it maps to server mode

The action wraps `python -m themis action`: it parses the triggering event
from `GITHUB_EVENT_PATH` with the same trigger rules as the webhook route
(drafts skip automatic reviews, bot senders are ignored, mention commands
work identically), then runs the same pipeline — shallow PR clone,
trusted-context masking, engine run, output parsing, outbound redaction,
summary + inline findings.

Differences, all inherent to the platform:

- **One job per workflow run.** There is no queue; GitHub's own workflow
  concurrency is the scheduler. Runner minutes are the cost model.
- **Engines run in-process.** Server mode isolates engine credentials in a
  separate agent container; here the ephemeral runner plays that role.
  Engine subprocesses still receive only the allowlisted environment.
- **No learnings.** Per-repo memory needs storage that outlives a runner
  and a digest branch; the action skips it entirely.
- **`.themis/config.yaml` still applies** — read from the repository's
  default branch (never the PR head), exactly like server mode. Both CLI
  families are preinstalled, so an `engine:` override in the repo config
  works whenever the matching credential env is present in the workflow
  (without it, the run posts the standard "no credentials" comment). The
  `default-config` input plays the role of `THEMIS_DEFAULT_REPO_CONFIG`.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `engine` | `claude` | `claude`, `codex`, `glm`, `kimi`, or `openrouter` |
| `github-token` | workflow token | clone + posting token; PAT for a custom identity |
| `mention` | `@themis` | trigger keyword in PR comments |
| `bot-login` | `github-actions[bot]` | login the posts appear under (set with a PAT) |
| `default-config` | unset | fallback `.themis/config.yaml` text (raw or base64) |
| `codex-auth-json` | unset | codex `auth.json` content (store as a secret) |
| `codex-sandbox` | `workspace-write` | codex sandbox mode |
| `engine-cli-version` | `latest` | npm version applied to both engine CLIs |

Engine credentials are passed as env vars on the action step (see the
example workflow), not as inputs — the engine adapters read them from the
environment, and the values never reach the workflow command line.

## Permissions

The example workflow requests exactly what the pipeline uses:

```yaml
permissions:
  contents: read        # clone the PR, read .themis/config.yaml
  pull-requests: write  # inline findings, summary, thread replies
  issues: write         # reactions and courtesy comments
  checks: read          # CI snapshot in the review context
  statuses: read
```

## Security model

Server mode isolates engine credentials and the GitHub-facing token in
separate containers. Action mode collapses that split onto one runner, so
the boundary moves to the process level:

- **The GitHub token never enters a live process environment.** An
  exec-time environment is readable through `/proc/<pid>/environ` by any
  same-UID process — and the engine child runs as the runner user on
  attacker-influenced PR content. `action.yml` therefore stages the token
  in a `0600` file from a step whose shell exits before any engine starts;
  the entrypoint reads the file, deletes it, and keeps the token in
  process memory only (cross-process memory reads are blocked by Yama
  ptrace restrictions on GitHub-hosted Ubuntu runners). Its value is also
  registered for outbound redaction.
- **Engine subprocesses get the allowlisted environment only**, exactly as
  in server mode: the engine credential it needs, never the GitHub token.
- **Engine credentials remain readable by the engine** — necessarily, as
  in server mode's agent container. The blast radius of a hostile PR is
  the engine credential plus whatever the repo makes clonable, not the
  write-capable GitHub token.
- On **self-hosted runners**, keep the default Yama setting
  (`kernel.yama.ptrace_scope=1`) and per-job workdir cleanup; a runner
  shared across jobs weakens the ephemerality this model leans on.

## Limits and caveats

- **Fork PRs are refused, fail closed.** `pull_request` runs from a fork
  get no secrets anyway, but comment-triggered workflows (`issue_comment`,
  `pull_request_review_comment`) run in the base-repo context *with*
  secrets even for fork PRs — so the entrypoint checks the PR head's
  repository via the API before any clone or engine start and refuses
  foreign (or deleted-fork) heads with an explanatory comment. Themis does
  not recommend `pull_request_target` (it runs workflow config from the
  base branch against attacker-controlled code — safe only with care that
  defeats the point of a drop-in action). For public repos with heavy
  fork traffic, the [service deployment](../README.md) is the right shape.
- **Codex on runners:** GitHub-hosted Ubuntu runners support Landlock, so
  the default `workspace-write` sandbox works. A `codex-auth-json` chain
  must be dedicated to the action (single-use rotating refresh tokens:
  sharing the chain with your workstation login kills one of them). Note
  that codex refresh-token rotation may rewrite `auth.json` locally; the
  runner's copy is discarded after the run, so a refreshed chain is *not*
  persisted — API-key `auth.json` content avoids that class of trouble
  entirely.
- **Self-hosted runners** need Node 22+, `uv`-installable Python ≥ 3.12,
  and `git` on the PATH.
- **Timeouts:** budget the job for the whole retry ladder, not one
  attempt: `.themis/config.yaml` allows `limits.max_attempts` engine runs
  of `limits.timeout_seconds` each (default 2 × 20 min), plus setup,
  clone, and posting. The example workflow's `timeout-minutes: 60` covers
  the defaults; raise it if you raise the limits, or the runner kills the
  job mid-retry and the pipeline's failure comment never posts.
