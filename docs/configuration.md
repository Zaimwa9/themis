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
| `THEMIS_ENGINE` | no | `codex` | instance default review engine; `codex`, `claude`, `glm`, `kimi`, or `openrouter` |
| `THEMIS_CONCURRENCY` | no | `1` | parallel jobs, `1`–`8`; out-of-range or non-integer values warn and fall back to `1`. Sizes the queue consumers and the engine-run slots, and must be set on both the controller and agent containers (the compose templates pass it to both). The practical limit is the operator's engine subscription quota, so keep it small |
| `THEMIS_DEFAULT_REPO_CONFIG` | no | unset | `.themis/config.yaml` content (raw yaml or base64 of it) used for repos that have no `.themis/config.yaml`; see below |
| `CODEX_HOME` | no | `/data/codex` | codex auth/state directory; its `auth.json` must be a login chain no other install uses (single-use rotating refresh tokens) |
| `THEMIS_CODEX_SANDBOX` | no | `workspace-write` | codex sandbox mode; `danger-full-access` for runtimes without Landlock |
| `CLAUDE_CODE_OAUTH_TOKEN` | agent only | unset | Claude Max token from `claude setup-token`; never set it on the controller |
| `GLM_API_KEY` | agent only | unset | Z.ai GLM Coding Plan key for the glm engine; never set it on the controller |
| `KIMI_API_KEY` | agent only | unset | Moonshot pay-as-you-go platform key for the kimi engine; never set it on the controller. Deliberately not the Kimi Code subscription key — that plan's guidelines restrict it to personal interactive use, which excludes review bots |
| `OPENROUTER_API_KEY` | agent only | unset | OpenRouter key for the openrouter engine (prepaid credits, pay-per-token — not a subscription); never set it on the controller |
| `THEMIS_PUBLIC_URL` | no | unset | enables webhook self-registration at `<url>/webhook` |
| `THEMIS_TUNNEL_API` | no | unset | ngrok agent API URL for tunnel discovery |
| `THEMIS_WEBHOOK_ENABLED` | no | `true` | set `false` for headless mode |
| `THEMIS_API_TOKEN` | no | unset | enables `/api/review` and `/api/discuss` |
| `THEMIS_WORKSPACE_ROOT` | no | `/tmp/themis` | scratch root for PR clones |
| `THEMIS_ROLE` | no | `controller` | role when `python -m themis` gets no argument; `controller` or `agent` |
| `PORT` | no | role default | listen port (`8000` controller, `8001` agent) |
| `THEMIS_DATA_ROOT` | no | `~/.themis` | durable store for pending learnings (compose mounts a volume at `/data/themis`) |
| `NGROK_AUTHTOKEN` | only with the `tunnel` compose profile | none | used only by the compose tunnel profile's ngrok sidecar |

When an engine's credentials die (expired setup-token, invalidated codex
refresh chain), Themis classifies the failure as `EngineAuthError`: the
review is not retried, a courtesy comment on the PR names the engine and the
credential to fix, and the worker logs `themis_engine_auth_failed`. Because
the diagnostics are matched against agent-visible output — which a hostile
PR could steer the agent into echoing — the agent service first confirms
the death out of band: it re-runs the engine with a fixed trusted prompt in
an empty scratch workspace (logged as `themis_auth_probe`). Only a probe
that also fails with an auth diagnostic triggers the terminal path;
otherwise the failure stays a plain retryable engine error.

### GitHub Action mode

In [GitHub Action mode](github-action.md) (`python -m themis action`) the
App, webhook, agent-service, and queue variables above do not apply. The
action reads: `THEMIS_GITHUB_TOKEN_FILE` (path to the posting + clone
token, staged as a file by `action.yml` so the value never sits in a live
process environment; a `GITHUB_TOKEN` env fallback exists for direct
invocation and is popped immediately), `THEMIS_ENGINE`
(default `claude` here — the env-credential engines fit workflows best),
`THEMIS_MENTION` (trigger keyword, default `@themis`), `THEMIS_BOT_LOGIN`
(default `github-actions[bot]`), `THEMIS_CODEX_AUTH_JSON` (codex
`auth.json` content, written to a private `CODEX_HOME` on the runner),
plus `THEMIS_CODEX_SANDBOX`, `THEMIS_DEFAULT_REPO_CONFIG`,
`THEMIS_WORKSPACE_ROOT` (default `$RUNNER_TEMP/themis`), and the engine
credential vars, all with server-mode semantics. The `action.yml` inputs
map onto these one-for-one, and `.themis/config.yaml` in the target repo
applies unchanged.

Names and defaults come straight from `../src/themis/config.py`, except
`PORT` and `THEMIS_ROLE` (read in `__main__.py`), `CODEX_HOME` (set in the Dockerfile), and
`CLAUDE_CODE_OAUTH_TOKEN`, `GLM_API_KEY`, `KIMI_API_KEY`, and
`OPENROUTER_API_KEY` (read directly by
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
# engine: codex            # codex | claude | glm | kimi | openrouter; unset = instance default (THEMIS_ENGINE)
# web_access: false        # toggles engine web tools; see the table below
model:
  # name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2, kimi: kimi-k3, openrouter: openrouter/auto)
  reasoning_effort: high   # low | medium | high (codex only; claude-harness engines ignore it)
limits:
  timeout_seconds: 1200
  max_attempts: 2
  clone_depth: 50
triggers:
  auto_review: true
  delta_review: false      # opt-in: re-review pushed commits as a delta once a review exists
  # skip_titles:            # wildcard patterns; a matching PR title skips the auto-review
  #   - 'ci: *'
  #   - 'chore: *'
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
| `engine` | unset (instance `THEMIS_ENGINE`) | `codex`, `claude`, `glm`, `kimi`, or `openrouter`; an invalid value warns and falls back to the instance default |
| `web_access` | `false` | toggles engine web tooling: codex enables sandbox network access; claude enables `WebFetch`/`WebSearch`; glm/kimi/openrouter behave like claude (`WebFetch`/`WebSearch`). Claude's unsandboxed Bash may still egress unless the deployment enforces an external network policy. Only the repo's default branch controls this |
| `model.name` | unset (engine default) | `gpt-5.4` for codex, `claude-opus-4-6[1m]` for claude, `glm-5.2` for glm, `kimi-k3` for kimi, `openrouter/auto` for openrouter — any OpenRouter model slug (e.g. `moonshotai/kimi-k3`) can be set, but OpenRouter's Claude Code integration only guarantees Anthropic first-party models; other providers' slugs may not work reliably as review agents |
| `model.reasoning_effort` | `high` | `low`, `medium`, or `high`; codex only, ignored by the claude-harness engines (claude/glm/kimi/openrouter) |
| `limits.timeout_seconds` | `1200` | wall-clock budget per agent attempt, in seconds |
| `limits.max_attempts` | `2` | attempts before Themis gives up and posts a failure comment |
| `limits.clone_depth` | `50` | git fetch depth for the shallow PR clone |
| `triggers.auto_review` | `true` | `false` = mention-only, no automatic review on PR open or ready-for-review |
| `triggers.delta_review` | `false` | opt-in: on push to an already-reviewed PR, re-review only the commits since the last themis review; off by default because each non-coalesced push may cost an engine run; see below |
| `triggers.skip_titles` | `[]` | case-insensitive wildcard patterns (`*`, `?`); a PR whose title matches any of them gets no automatic review (mention/API reviews still run); see below |
| `learnings.enabled` | `true` | per-repo learnings memory; see [docs/learnings.md](learnings.md) |
| `learnings.digest_threshold` | `10` | pending learnings needed before Themis opens/updates the digest PR (min 1) |
| `review.modules.<name>` | per-module profile | tri-state presence per optional review section: `always`, `auto`, or `off`; see below |
| `agent.context` | `false` | the review agent natively discovers instruction files (`CLAUDE.md`, `AGENTS.md`) — resolved from the PR base revision, never the PR head; see below |
| `agent.skills` | `false` | the review agent uses `.claude/skills` packages — same base-revision rule; native discovery on claude/glm/kimi/openrouter, a synthesized index (skills bridge) on codex |

A partial file overlays the defaults key by key, so you only need to set the
fields you want to change. Unknown fields are ignored. An invalid field warns
and falls back to that field's built-in default without discarding valid
sibling fields.

### Delta re-reviews (`triggers.delta_review`)

Opt-in — off by default, because it turns pushes to a reviewed PR into
automatic engine runs (rapid pushes coalesce into one, and a push whose head
the last review already covered exits before any engine starts). Enable it
with `delta_review: true` on repos where the iterate-on-findings loop is
worth that cost.

Independent of `auto_review`, which governs first reviews only: a
mention-only repo (`auto_review: false`) can enable `delta_review` and get
"review once by mention, then every push re-checks itself" — the prior review
is the consent, so no further mention is needed.

Once enabled and a themis review exists on a PR, pushing new commits triggers a scoped
re-review of just what changed since the last reviewed commit — the review
prompt narrows to `git diff <last-reviewed-sha>..HEAD`, checks each open
finding thread against the new code (resolving verified fixes, replying with
what is still missing otherwise), and reports issues the fix commits
introduced as regular tracked findings.

Mechanics and bounds:

- The last reviewed commit is read from a checkpoint themis writes at the
  start of its own summary comments, carrying a signature bound to the
  repository, PR and commit; marker text anywhere else — comments by others,
  bot replies quoting untrusted text, or the review prose itself — is
  ignored, and an unsigned or unverifiable one is refused rather than trusted
  (see [`security.md`](security.md#control-markers-and-the-delta-checkpoint)). The scan walks the conversation from the newest comment
  backwards and stops at the latest checkpoint, so later discussion volume
  does not bury it; in the extreme case where the scan's safety bound
  (thousands of comments) runs out first, the push gets a full review
  rather than a silent skip — which also posts a fresh checkpoint at the
  conversation tail. A PR that has never been reviewed gets nothing on push — the
  first review still comes from PR open / ready-for-review, a mention, or
  `/api/review`.
- Thread follow-through is best-effort: the delta prompt requires resolving
  each verified-fixed finding thread and replying to each still-open one,
  and any open finding thread the run failed to re-check is listed in the
  summary and stays open for the next review.
- Rapid pushes collapse: while a review for the PR is queued or running,
  further push events coalesce into a single follow-up check that runs once
  the active review finishes — it re-reads the PR head and either covers
  everything pushed since the last review in one delta or exits without
  cost when that review already reached the head.
- After a force-push (or when the shallow clone no longer reaches the last
  reviewed commit) there is no trustworthy delta, so the push is reviewed as
  a full review instead.
- Server mode only: [GitHub Action mode](github-action.md) has no App key to
  sign checkpoints with, so this setting has no effect there.
- Delta re-reviews are automatic triggers, but neither `auto_review` nor draft
  status gates them: they answer a review that already exists, so
  `delta_review` alone enables them. Drafts included — a draft carrying a
  themis review was already asked about, and iterating on findings before
  marking ready is the normal way to work. A push to a draft that has never
  been reviewed still gets nothing. `triggers.skip_titles` matches do skip
  deltas — retitling a PR to a filtered pattern stops further ones. An
  explicit `@mention review` always runs a full review.

### Title filters (`triggers.skip_titles`)

Each entry is a wildcard pattern — `*` matches any run of characters, `?`
matches exactly one, everything else is literal — compared case-insensitively
against the *whole* PR title. A match skips the automatic review on PR open /
ready-for-review and leaves a short comment naming the rule (at most one
per PR across draft/ready cycles, best effort); an explicit `@mention` or
`/api/review` call still reviews the PR — the same escape hatch as
`auto_review: false`.

```yaml
triggers:
  skip_titles:
    - 'ci: *'              # titles starting with "ci: "
    - 'chore: *'           # one pattern per prefix; a list is the "or"
    - 'WIP*'               # "WIP", "WIP:", "WIP anything…"
    - '*[skip review]*'    # opt-out marker anywhere in the title
```

Because a pattern covers the whole title, `ci: *` does not fire on
`PCI: rotate keys` or `revert ci: bump runner`; wrap a keyword in stars
(`*keyword*`) to match it anywhere. Patterns are not regular expressions —
`(`, `|`, `$` and friends are literal characters. Surrounding whitespace is
trimmed; an entry that is blank, not a string, or longer than 200 characters
is dropped with a warning while the remaining entries keep filtering (at
most the first 50 valid patterns are used). Matching case-folds both sides,
so the rare character that expands under case-folding (`ß` → `ss`) counts
as its folded length for `?`; use `*` around such characters.

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
  discovered natively (claude/glm/kimi/openrouter engines). Engines without native skill
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

### Linked issue and PR context

When the PR title or description references other issues or pull requests —
`Fixes #12`, `GH-12`, `owner/repo#34`, or a full `github.com/.../issues/N` /
`.../pull/N` URL — Themis resolves them with its installation token before
the review and writes them to `.review-input/linked_issues.json` (title,
state, author, body), so the agent can judge the change against what it
claims to address. This is automatic and needs no configuration, and it
requires no new App permissions or installation approval: the lookups are
covered by the Issues and Pull requests permissions Themis already
requests, plus the Metadata read access every GitHub App has.

Scope and bounds, in the same spirit as the rest of the trust model:

- Same-owner references only. A reference to a repository under another
  owner is ignored; Themis never fetches third-party content on behalf of
  a PR description.
- A cross-repository reference resolves only when the referenced
  repository's visibility is explicitly **public**, fail closed — private
  and Enterprise `internal` siblings never resolve: the installation token
  may reach them, but a PR description must never move non-public content
  into a review the reviewed repo's readers could not already see.
- At most 5 references per review, bodies clamped to 4000 characters
  (`body_truncated: true` marks a clipped body).
- Best effort: a reference the token cannot see (deleted, private,
  uninstalled repo) is skipped with a log line and never blocks the review,
  and the whole resolution runs under one 20-second deadline — on expiry
  the review proceeds with whatever resolved in time.
- The fetched content is handed to the agent as **data, not instructions**,
  with the same prompt guardrails as requester-supplied extra context: it
  cannot suppress findings, change severities, or alter the output contract.

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
