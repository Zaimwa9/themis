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
| `THEMIS_CONCURRENCY` | no | `1` | parallel jobs, `1`–`8`; out-of-range or non-integer values warn and fall back to `1`. The practical limit is the operator's engine subscription quota, so keep it small |
| `THEMIS_DEFAULT_REPO_CONFIG` | no | unset | `.themis/config.yaml` content (raw yaml or base64 of it) used for repos that have no `.themis/config.yaml`; see below |
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
| `THEMIS_DATA_ROOT` | no | `~/.themis` | durable store for pending learnings (compose mounts a volume at `/data/themis`) |
| `NGROK_AUTHTOKEN` | only with the `tunnel` compose profile | none | used only by the compose tunnel profile's ngrok sidecar |

Names and defaults come straight from `../src/themis/config.py`, except
`PORT` and `THEMIS_ROLE` (read in `__main__.py`), `CODEX_HOME` (set in the Dockerfile), and
`CLAUDE_CODE_OAUTH_TOKEN` and `GLM_API_KEY` (read directly by
the engine adapters in `../src/themis/engines/`, not part of `Settings`). Model, limit, and trigger configuration
lives in `.themis/config.yaml` (or its `THEMIS_DEFAULT_REPO_CONFIG`
fallback, below), not in dedicated environment variables. There is no
mention configuration at all: the
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
learnings:
  enabled: true            # false = no capture, no injection, no digest PR
  digest_threshold: 10
agent:
  context: false           # true = load CLAUDE.md/AGENTS.md natively, from the PR base
  skills: false            # true = load .claude/skills natively, from the PR base
review:
  modules:                 # always | auto | off (booleans accepted: true = auto, false = off)
    scorecard: always
    walkthrough: always
    product_impact: always
    verification_steps: always
    assumptions: always
    sign_off: always
    ci_context: auto
    inline_findings: auto
    code_suggestions: auto
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
| `learnings.enabled` | `true` | per-repo learnings memory; see [docs/learnings.md](learnings.md) |
| `learnings.digest_threshold` | `10` | pending learnings needed before Themis opens/updates the digest PR (min 1) |
| `review.modules.<name>` | per-module profile | tri-state presence per optional review section: `always`, `auto`, or `off`; see below |
| `agent.context` | `false` | the review agent natively discovers instruction files (`CLAUDE.md`, `AGENTS.md`) — resolved from the PR base revision, never the PR head; see below |
| `agent.skills` | `false` | the review agent uses `.claude/skills` packages — same base-revision rule; native discovery on claude/glm, a synthesized index (skills bridge) on codex |

A partial file overlays the defaults key by key, so you only need to set the
fields you want to change. Unknown fields are ignored. An invalid field warns
and falls back to that field's built-in default without discarding valid
sibling fields.

### Review modules (`review.modules`)

The optional parts of a review are modules, each with a tri-state value.
Presentation categories (`scorecard`, `walkthrough`, `product_impact`,
`verification_steps`, `assumptions`, `sign_off`) have explicit presence:

- `always` — the category appears on every review.
- `auto` — retained as a compatibility alias for enabled presentation
  categories.
- `off` — the category is omitted completely.

When an enabled category has nothing material to add, it stays visible with a
short empty-state message. Blockers, Majors, and Nits are different: they are
finding groups rather than presentation categories, and an empty group is
always omitted.

For `ci_context`, `auto` remains adaptive (failed checks are mentioned while
neutral/passing states may be omitted), `always` reports every snapshot state,
and `off` suppresses CI commentary.

`big_picture` is adaptive too, by design: under `auto` (the default) the
`Big picture:` note appears only when the change provides concrete structural
evidence — there is no empty-state filler when it doesn't. `always` pins the
note on every review (`Big picture: Fits the existing boundaries.` when clean),
and `off` suppresses the note. The underlying step-back structural analysis
runs regardless: a design that already causes a concrete problem is a normal
calibrated finding, so `off` can never hide a defect.

Booleans are accepted as lenient aliases (`true` → `auto`, `false` → `off`),
and yaml's bare `off` parses as `false`, which lands on the same state. An
invalid value warns and behaves as unset, retaining that module's default.

For the two delivery modules (`inline_findings`, `code_suggestions`),
`always` is equivalent to `auto`: delivery is already mandatory whenever it
applies (every anchorable finding is posted inline; a suggestion block is
attached whenever the exact fix is small and certain), so there is nothing
extra for `always` to force. Their meaningful settings are `auto` and `off`.

| Module | Default | Controls |
|---|---|---|
| `scorecard` | `always` | the canonical four-row numeric `/5` Correctness / Test coverage / Code quality / Product impact table |
| `walkthrough` | `always` | the logical-area walkthrough in a collapsed GitHub details block |
| `product_impact` | `always` | the standalone `Product take:` narrative |
| `big_picture` | `auto` | the `Big picture:` architecture/maintainability trajectory note. The step-back structural pass itself always runs; `off` suppresses only the note, and a structural defect with concrete consequences still surfaces as a calibrated finding |
| `verification_steps` | `always` | the `🧪 How to verify` details block |
| `assumptions` | `always` | the `🧭 Assumptions & unverified claims` details block |
| `sign_off` | `always` | the italic, good-natured PR-specific sign-off with the reviewed-at SHA |
| `ci_context` | `auto` | CI commentary in the review body (CI is still collected as evidence) |
| `inline_findings` | `auto` | posting findings as inline review comments; `off` folds every finding into the summary — every path/line pointer is kept, and bodies keep as much context as fits GitHub's comment cap — enforced at posting time, not just in the prompt |
| `code_suggestions` | `auto` | GitHub ```suggestion blocks inside inline findings; `off` keeps the finding and states the fix as prose, enforced by stripping at posting time |

The core output — verdict line, TL;DR/assessment, and the severity sections —
is not a module and can never be turned off: configuration must not be able
to silently hide defects.

### Default presentation and packaged doctrine

The presentation profile is independent of doctrine selection. Every repo
defaults all six presentation categories to `always`; `ci_context`,
`inline_findings`, and `code_suggestions` remain `auto`. Each explicit valid
value in `review.modules` overlays its own default, whether or not the repo has
a committed doctrine. Presentation `auto` values resolve as enabled for
backward compatibility, so `off` is the only suppression mechanism.

Presence is configurable, rendering is canonical: the scorecard uses integer
`/5` scores, walkthrough/verification/assumptions use collapsed details blocks,
and the sign-off remains one italic, PR-specific line. Categories with no
material content use their documented empty-state message.

Separately, when the PR checkout has no `.themis/review.md`, Themis applies a
built-in default doctrine (the repo-agnostic philosophy, severity calibration,
and verification habits from `examples/themis/review.md`) instead of reviewing
doctrine-less. A committed doctrine replaces that free-text guidance wholesale;
it does not change the presentation defaults.

### Trusted agent context (`agent`)

By default the review agent loads **nothing** from the repository: no
`CLAUDE.md`/`AGENTS.md`, no settings, no hooks, no skills, no MCP servers. A
PR could otherwise rewrite the reviewer's instructions and steer its own
review. The `agent` keys opt back into the useful part of that surface
without the injection risk:

- `agent.context: true` — instruction files (`CLAUDE.md`, `AGENTS.md`,
  including nested ones) are discovered natively by the engine, plus the
  files they `@`-reference.
- `agent.skills: true` — skill packages under `.claude/skills/` are
  discovered natively (claude/glm engines). Engines without native skill
  discovery (codex) get the **skills bridge** instead: Themis synthesizes
  `.review-input/skills-index.md` from the base-revision `SKILL.md`
  frontmatter (name and description, capped at 50 entries and 200
  characters per description) and one static prompt sentence tells the
  agent to read a skill's file when its description matches the code under
  review — the same progressive disclosure the claude harness does
  natively. Author skills once, in the claude format, and every engine
  uses them.

Both are independent and off by default, and they are repository behavior:
they can only be set in `.themis/config.yaml` (read from the default
branch), never through environment variables.

Whether or not a repo opts in, every job starts with a workspace mask:
PR-head instruction files, `.claude/`, and executable configuration
(`.claude/settings.json`, hooks, plugins, agents, commands, `.mcp.json`)
are removed from the working tree — codex discovers `AGENTS.md` natively
and has no CLI flag against it, so the mask is what isolates the agent from
PR-controlled instructions. Opting in then rebuilds those namespaces from
the **PR base revision** before the agent starts: base versions are
materialized at their canonical paths for native discovery to read. The
workspace is intentionally synthetic — application code from the PR head,
agent inputs from the trusted base — and the review diff still shows
changes to those files; they just don't influence the review that examines
them.

Everything fails closed per capability: a base instruction file referencing
a path that only the PR head provides, oversized content (1 MiB per file,
10 MiB and 200 files per capability), or a path that would escape the
workspace disables that capability for the run and leaves its namespace
empty — exactly the no-opt-in behavior. Reviews only; discussion jobs keep
the fully-disabled baseline.

### Instance-level default (`THEMIS_DEFAULT_REPO_CONFIG`)

When you can't (or don't want to) commit `.themis/config.yaml` to a target
repo — trying Themis on a repo you can't push to yet — set
`THEMIS_DEFAULT_REPO_CONFIG` on the controller to the config content, raw
yaml or base64-encoded. A shell assignment is not seen by a later
`docker compose up`; put the encoded value in the deployment's `.env`
(single line, like the private key):

```bash
printf 'triggers:\n  auto_review: false\n' | base64 | tr -d '\n'
# then in .env next to the compose file:
# THEMIS_DEFAULT_REPO_CONFIG=dHJpZ2dlcnM6CiAgYXV0b19yZXZpZXc6IGZhbHNlCg==
```

Resolution order per repo: `.themis/config.yaml` in the repo if present,
else `THEMIS_DEFAULT_REPO_CONFIG`, else built-in defaults. A repo file
replaces the instance default wholesale — the two are never merged key by
key. A value that isn't valid yaml (or isn't a mapping) fails startup;
what's inside is handled leniently like a repo file: unknown keys are
ignored and invalid values degrade to defaults with a warning.

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
config can hold a queue consumer past that ceiling.
