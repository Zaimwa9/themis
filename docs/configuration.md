# Configuration

Two planes:

- **Environment variables**: identity and infrastructure. Who Themis is on
  GitHub, where it stores state, how it's reachable. Set once per
  deployment.
- **`.themis/` in the target repo**: behavior. Review philosophy, model,
  limits, trigger rules. Set per repo.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `THEMIS_GH_APP_CLIENT_ID` | yes | none | GitHub App client id |
| `THEMIS_GH_APP_PRIVATE_KEY` | yes | none | App private key, PEM text or base64 of it |
| `THEMIS_GH_WEBHOOK_SECRET` | yes, unless `THEMIS_WEBHOOK_ENABLED=false` | none | webhook HMAC secret, shared with the App settings |
| `THEMIS_AGENT_TOKEN` | yes | none | controller-to-agent bearer token; use the same random value in both containers |
| `THEMIS_AGENT_URL` | no | `http://agent:8001` | internal URL of the isolated agent service |
| `THEMIS_ENGINE` | no | `codex` | instance default review engine; `codex`, `claude`, or `glm` |
| `CODEX_HOME` | no | `/data/codex` | codex auth/state directory |
| `THEMIS_CODEX_SANDBOX` | no | `workspace-write` | codex sandbox mode; `danger-full-access` for runtimes without Landlock |
| `CLAUDE_CODE_OAUTH_TOKEN` | agent only | unset | Claude Max token from `claude setup-token`; never set it on the controller |
| `GLM_API_KEY` | agent only | unset | Z.ai GLM Coding Plan key for the glm engine; never set it on the controller |
| `THEMIS_PUBLIC_URL` | no | unset | enables webhook self-registration at `<url>/webhook` |
| `THEMIS_TUNNEL_API` | no | unset | ngrok agent API URL for tunnel discovery |
| `THEMIS_WEBHOOK_ENABLED` | no | `true` | set `false` for headless mode |
| `THEMIS_API_TOKEN` | no | unset | enables `/api/review` and `/api/discuss` |
| `THEMIS_WORKSPACE_ROOT` | no | `/tmp/themis` | scratch root for PR clones |
| `THEMIS_ROLE` | no | `controller` | role when `python -m themis` gets no argument; `controller` or `agent` |
| `PORT` | no | role default | listen port (`8000` controller, `8001` agent) |
| `NGROK_AUTHTOKEN` | only with the `tunnel` compose profile | none | used only by the compose tunnel profile's ngrok sidecar |

Names and defaults come straight from `../src/themis/config.py`, except
`PORT` and `THEMIS_ROLE` (read in `__main__.py`), `CODEX_HOME` (set in the Dockerfile), and
`CLAUDE_CODE_OAUTH_TOKEN` and `GLM_API_KEY` (read directly by
the engine adapters in `../src/themis/engines/`, not part of `Settings`). There is no model, limit, or mention configuration
in the environment; the
mention handle is derived at startup from `GET /app` (the App's slug), so it
can never drift from the App's actual name.

## `.themis/config.yaml`

Lives in the target repository. Start from `../examples/themis/` (copy it
in as `.themis/`, see the README's Quickstart). Every key is optional; a
repo with no `.themis/` directory at all gets full defaults.

```yaml
# engine: codex            # codex | claude | glm; unset = instance default (THEMIS_ENGINE)
# web_access: false        # toggles engine web tools; see the table below
model:
  # name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2)
  reasoning_effort: high   # low | medium | high (codex only; claude-harness engines ignore it)
limits:
  timeout_seconds: 1200
  max_attempts: 2
  clone_depth: 50
triggers:
  auto_review: true
```

| Key | Default | Meaning |
|---|---|---|
| `engine` | unset (instance `THEMIS_ENGINE`) | `codex`, `claude`, or `glm`; an invalid value warns and falls back to the instance default |
| `web_access` | `false` | toggles engine web tooling: codex enables sandbox network access; claude enables `WebFetch`/`WebSearch`; glm behaves like claude (`WebFetch`/`WebSearch`). Claude's unsandboxed Bash may still egress unless the deployment enforces an external network policy. Only the repo's default branch controls this |
| `model.name` | unset (engine default) | `gpt-5.4` for codex, `claude-opus-4-6[1m]` for claude, `glm-5.2` for glm |
| `model.reasoning_effort` | `high` | `low`, `medium`, or `high`; codex only, ignored by claude/glm |
| `limits.timeout_seconds` | `1200` | wall-clock budget per agent attempt, in seconds |
| `limits.max_attempts` | `2` | attempts before Themis gives up and posts a failure comment |
| `limits.clone_depth` | `50` | git fetch depth for the shallow PR clone |
| `triggers.auto_review` | `true` | `false` = mention-only, no automatic review on PR open or ready-for-review |

A partial file deep-merges over the defaults, key by key, so you only need
to set the fields you want to change.

A malformed `.themis/config.yaml`, invalid YAML, wrong types, not a
mapping, logs a warning and Themis proceeds on full defaults. A broken
config file in a target repo must never block reviews.

## Why config is fetched from the default branch

Themis reads `.themis/config.yaml` via the GitHub Contents API from the
repository's **default branch**, once per job, not from the PR branch. Two
reasons: the values (`clone_depth`, `auto_review`) are needed before the PR
is even cloned, and reading from the default branch means a PR cannot
change the bot's own behavior for its own review, such as disabling review
or switching to a costlier model to burn quota.

The review doctrine, `.themis/review.md`, is different: it's read from the
PR checkout on purpose, so it can reference the code the PR touches. See
[`docs/security.md`](security.md) for the trust model that follows from
that choice.

## Fixed job ceiling

Every job (review or discussion) runs under a fixed timeout of 2700
seconds, 2 times the default `timeout_seconds` plus 300s of headroom for
cloning and posting, enforced by the job queue rather than the per-repo
config. A repo raising `limits.timeout_seconds` past what fits under that
ceiling is still capped, because the repo config is only fetched inside the
job, after the queue has already committed to running it. No single repo's
config can hold the shared, single-concurrency queue past that ceiling.
