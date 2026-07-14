# Themis

Themis is a self-hosted GitHub PR review bot that runs on your own Codex or
Claude Max subscription. It reviews pull requests with inline findings and a structured
summary (verdict, scoring table, severity-ordered sections), answers
questions in review threads and PR conversation, and takes its review
doctrine from your own repository, under `.themis/`.

<!-- screenshot: docs/assets/review-example.png -->

## How it works

A GitHub App webhook delivers PR and comment events to Themis. Each event
becomes a job on an in-memory queue, processed one at a time. The worker
shallow-clones the PR head, runs the configured engine (`codex exec`, or
`claude -p` — natively or in API mode for GLM) against your repo's review doctrine, and posts findings and a
summary back to GitHub as the App. One image runs as an isolated controller
and agent; there is still no database, Redis, or message broker.

## Prerequisites

- Docker with the Compose plugin (Docker Desktop, or Docker Engine + `docker compose`)
- An OpenAI account with Codex access, or a Claude Max subscription (pick your engine): the matching CLI installed (`npm install -g @openai/codex` or `npm install -g @anthropic-ai/claude-code`, Node 22+) and `codex login` or `claude setup-token` working on your machine. Claude Pro is not supported because Themis defaults to Opus.
  The glm engine needs no local CLI login: just a `GLM_API_KEY` (Z.ai GLM Coding Plan) in `.env`.
- A GitHub account that can create a GitHub App (personal account or an org)

No clone or build needed: Themis ships as a prebuilt image,
`ghcr.io/zaimwa9/themis`.

## Choose a setup path

| Path | GitHub App | Best for |
|---|---|---|
| [Quick start](#quick-start-manifest-bootstrap--testing-not-long-term) | auto-created via manifest, random name | trying Themis in minutes, throwaway deployments |
| [Production setup](#production-setup-manual-github-app) | created by you once, stable name | long-term installs that survive redeployments |
| [Headless mode](#headless-mode-bring-your-own-webhook) | created by you once | teams with existing webhook infrastructure |

All three run the same prebuilt image and support every engine.

## Quick start (manifest bootstrap — testing, not long-term)

The bootstrap uses GitHub's App Manifest flow to create the App, generate its
private key and webhook secret, install it on the requested repository, and
write a ready-to-run deployment. There are no GitHub settings to copy. GitHub
still asks the account owner to approve App creation and repository access.

> **Why not long-term?** The manifest flow generates a random App name and must
> be re-run on every fresh deployment. For a permanent install, use the
> [Production setup](#production-setup-manual-github-app) below.

### Using a coding agent

Copy the prompt below into Claude Code, Codex, or any agent that can run
shell commands. It handles the entire bootstrap autonomously:

```text
Set up Themis for this repository using the automated GitHub App Manifest bootstrap:

  https://github.com/Zaimwa9/themis/blob/main/docs/bootstrap.md

  Use the Claude engine and the bundled ngrok tunnel.

  Do not manually create or configure a GitHub App. Run `python -m themis init` with
  `--engine claude --tunnel`.

  Handle everything autonomously. Only pause when GitHub requires my approval, or when
  I need to provide an ngrok auth token or complete Claude authentication.

  After setup, start the deployment, verify it works, and tell me the bot's @mention
  and how to trigger a review.
```

### 1. Log in to Codex

Using the Claude engine instead? Run `claude setup-token`, pass
`--engine claude` to the bootstrap, and put the resulting token in
`CLAUDE_CODE_OAUTH_TOKEN` in the generated `.env`. Using glm? No CLI
login needed: pass `--engine glm` to the bootstrap and put your Z.ai GLM
Coding Plan key in `GLM_API_KEY` in the generated `.env`. Details in
[Engines](#engines).

Install the CLI if you haven't already (Node 22+):

```bash
npm install -g @openai/codex
```

```bash
codex login
```

This writes credentials to `~/.codex/auth.json`. The bootstrap copies it into
the generated deployment with mode `0600`.

### 2. Bootstrap

For an instance with an existing public HTTPS URL:

```bash
mkdir themis-deploy
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:8976:8976 \
  -v "$PWD/themis-deploy:/output" \
  -v "$HOME/.codex:/host-codex:ro" \
  ghcr.io/zaimwa9/themis:latest \
  python -m themis init \
  --repo OWNER/REPO \
  --public-url https://themis.example.com \
  --output /output \
  --codex-auth /host-codex/auth.json \
  --bind-host 0.0.0.0 \
  --no-browser
```

For a local machine without a public URL, export an ngrok token and replace
`--public-url ...` with `--tunnel`:

```bash
export NGROK_AUTHTOKEN=<your-token>
# Add `-e NGROK_AUTHTOKEN` to docker run, then pass `--tunnel` to themis init.
```

If the target is owned by an organization, also pass `--organization OWNER`.
The App must be created under that organization because the generated App is
private. The command prints a localhost URL. Open it, approve the pre-filled
App, and select `OWNER/REPO` on the installation screen. The private key never
leaves the bootstrap process and the generated `.env`. The success screen and
terminal show the bot's generated `@mention`; it is also saved in
`themis-info.json` for later reference.

The same command can run from a source checkout without Docker:

```bash
uv run python -m themis init \
  --repo OWNER/REPO \
  --public-url https://themis.example.com \
  --output ./themis-deploy
```

Detailed options, the tunnel command, and recovery are in
[`docs/bootstrap.md`](docs/bootstrap.md).

### 3. Run and verify

```bash
cd themis-deploy
docker compose up -d
```

Use `docker compose --profile tunnel up -d` when the bootstrap used `--tunnel`.
Themis discovers the tunnel URL and updates the App webhook automatically.

```bash
curl localhost:8000/healthz
# {"status":"ok"}
```

Check `docker compose logs themis` for the startup line reporting the
resolved App slug. Open a test PR against a repo the App is installed on:
expect a 👀 reaction on the trigger, then a 🚀 reaction on the PR once the
review starts, then the review itself.

## Production setup (manual GitHub App)

Create the App yourself, once: it gets a stable name and `@mention`, and any
number of deployments can point at it without ever re-running a bootstrap.

### 1. Create the GitHub App

Under **Settings > Developer settings > GitHub Apps > New GitHub App**, on the
organization that owns the target repos or on your personal account:

| Setting | Value |
|---|---|
| GitHub App name | your choice, must be unique on GitHub; its slug becomes the bot's `@mention` |
| Homepage URL | any URL; `https://github.com/Zaimwa9/themis` works |
| Webhook URL | `https://HOST/webhook`; any placeholder works if you set `THEMIS_PUBLIC_URL` later, Themis re-registers it at startup |
| Webhook secret | a long random string, goes in `THEMIS_GH_WEBHOOK_SECRET` |
| Checks permission | Read-only |
| Contents permission | Read-only |
| Issues permission | Read and write |
| Pull requests permission | Read and write |
| Commit statuses permission | Read-only |
| Events | `pull_request`, `issue_comment`, `pull_request_review_comment` |

Actions permission is not required. Existing Apps must be updated with the
Checks and Commit statuses permissions before Themis can include CI context
in reviews.

Then generate a private key (App settings > Private keys) and install the App
on the target repositories (App settings > Install App).

### 2. Configure and deploy

Grab the Compose file and point it at the published image:

```bash
mkdir themis-deploy && cd themis-deploy
curl -fsSLO https://raw.githubusercontent.com/Zaimwa9/themis/main/docker-compose.yml
# edit: replace the two `build: .` lines with `image: ghcr.io/zaimwa9/themis:latest`
```

Create `.env` next to it ([`.env.example`](.env.example) documents every key):

```text
THEMIS_GH_APP_CLIENT_ID=<App client id>
THEMIS_GH_APP_PRIVATE_KEY=<PEM, or base64 of it>
THEMIS_GH_WEBHOOK_SECRET=<the webhook secret>
THEMIS_AGENT_TOKEN=<any long random string>
THEMIS_PUBLIC_URL=https://your-host    # optional: webhook self-registration
THEMIS_ENGINE=codex                    # or claude / glm
CLAUDE_CODE_OAUTH_TOKEN=<token>        # claude engine only
GLM_API_KEY=<key>                      # glm engine only
```

```bash
docker compose up -d
```

For the codex engine, seed the auth volume once the agent is up. The pipe
runs as the container's unprivileged `themis` user, so ownership and `0600`
mode come out right (`docker compose cp` would leave the file root-owned and
unreadable to the agent):

```bash
docker compose exec -T agent sh -c 'umask 077; cat > /data/codex/auth.json' \
  < ~/.codex/auth.json
```

PaaS deployment, upgrades, and the full env reference:
[`docs/server-deploy.md`](docs/server-deploy.md) and
[`docs/configuration.md`](docs/configuration.md).

### 3. Verify

Same checks as the quick start: `curl localhost:8000/healthz`, look for the
App slug in `docker compose logs themis`, open a test PR.

## Headless mode (bring your own webhook)

Already have webhook infrastructure? Set `THEMIS_WEBHOOK_ENABLED=false` to
remove Themis's inbound webhook route and drive it from your own handler
through two authenticated HTTP routes, `POST /api/review` and
`POST /api/discuss`. The GitHub App still has to exist and be installed,
created as in the [Production setup](#production-setup-manual-github-app).
Contracts and examples: [`docs/headless.md`](docs/headless.md).

## Customize reviews

Copy the starter kit into the target repo. This needs a temporary shallow
checkout of Themis (the deployment itself does not):

```bash
starter="$(mktemp -d)"
git clone --depth 1 https://github.com/Zaimwa9/themis.git "$starter"
cp -r "$starter/examples/themis" .themis
```

- `.themis/review.md`: the review doctrine, philosophy, severity
  calibration, a map of your codebase, house rules. Edit this; it is read
  straight from the PR branch on every review.
- `.themis/config.yaml`: behavior knobs, every key optional.

How the doctrine is consumed and how to write one that works:
[`docs/doctrine.md`](docs/doctrine.md). This repo reviews itself with its own
[`.themis/review.md`](.themis/review.md).

| Key | Default | Meaning |
|---|---|---|
| `engine` | instance `THEMIS_ENGINE` | `codex`, `claude`, or `glm`, overrides the instance's default engine for this repo |
| `web_access` | `false` | toggles engine web tooling (`WebFetch`/`WebSearch`); glm behaves like claude here, and Claude's Bash may still egress unless the deployment enforces an external network policy — this caveat applies to all claude-harness engines |
| `model.name` | engine default | engine default: `gpt-5.4` (codex), `claude-opus-4-6[1m]` (claude), `glm-5.2` (glm) |
| `model.reasoning_effort` | `high` | `low` \| `medium` \| `high` (codex only) |
| `limits.timeout_seconds` | `1200` | per agent attempt |
| `limits.max_attempts` | `2` | attempts before posting a failure comment |
| `limits.clone_depth` | `50` | shallow clone depth |
| `triggers.auto_review` | `true` | `false` = mention-only, no auto-review when a PR opens or is marked ready for review |

Talk to the bot in a PR: `@<app-slug> review` re-reviews on demand,
`@<app-slug> review <focus>` steers the review toward a given area (the
focus text is honored only from repo owners, org members, and
collaborators — anyone else gets a plain review), `@<app-slug> <question>`
asks a question, and replies inside a thread the bot already posted in are
answered automatically, no mention needed.

## Engines

Themis runs reviews through an agent CLI, using your Codex, Claude Max, or GLM Coding Plan subscription:

| Engine | Auth | Setup |
|---|---|---|
| `codex` (default) | `auth.json` volume (`CODEX_HOME`) | `codex login` locally; bootstrap copies `auth.json` into the generated volume |
| `claude` | one env var | run `claude setup-token` locally, set `CLAUDE_CODE_OAUTH_TOKEN` in `.env` |
| `glm` | one env var | set `GLM_API_KEY` in `.env` (Z.ai GLM Coding Plan key); reviews run through the claude CLI against Z.ai's Anthropic-compatible endpoint |

Pick the instance default with `THEMIS_ENGINE` in `.env`. A repo can override it
in `.themis/config.yaml` with `engine:` set to any of them; if that engine
has no credentials on the instance, Themis posts a comment saying so instead of
failing silently. The claude and glm paths need no volume: key in
`.env`, done.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Crashes at startup naming an env var | Set that variable; Themis fails fast on missing or invalid required config. |
| Crashes at startup on a `GET /app` call | Wrong `THEMIS_GH_APP_CLIENT_ID` or malformed `THEMIS_GH_APP_PRIVATE_KEY`. |
| Codex sandbox errors | Set `THEMIS_CODEX_SANDBOX=danger-full-access`; the container is the sandbox boundary on runtimes without Landlock. |
| PR comment says the usage limit was reached | The subscription of whichever engine ran the job has hit its usage window. Mention the bot again once it resets. |
| Auth that worked starts failing months later | Run `codex login` locally, then refresh the persistent agent credential using the command in [Automated setup: Refreshing Codex authentication](docs/bootstrap.md#refreshing-codex-authentication). |
| Review comment says engine credentials missing | Set `CLAUDE_CODE_OAUTH_TOKEN` (claude), `GLM_API_KEY` (glm), or seed the codex auth volume (codex), or change `THEMIS_ENGINE` / the repo's `engine:` key. |
| Webhook deliveries show 401 in the App's settings | `THEMIS_GH_WEBHOOK_SECRET` doesn't match the App's webhook secret. |
| Where are the logs | `docker compose logs -f themis` |
| A job queued right before a restart never ran | The in-memory queue doesn't survive restarts; mention the bot again to re-trigger. |
| Opened a PR, nothing happened | Check in order: PR is a draft (skipped until marked ready); PR author is a bot account (ignored); `auto_review: false` in `.themis/config.yaml`; the GitHub App isn't installed on that repo; webhook deliveries are failing (App settings > Advanced > Recent Deliveries). |

## Documentation

- [`docs/server-deploy.md`](docs/server-deploy.md): deploying to any Docker host or PaaS, upgrades.
- [`docs/local-tunnel.md`](docs/local-tunnel.md): the ngrok tunnel profile in depth.
- [`docs/headless.md`](docs/headless.md): bring your own webhook handler, the `/api/review` and `/api/discuss` contracts.
- [`docs/doctrine.md`](docs/doctrine.md): the review doctrine, how it works and how to write a good one.
- [`docs/configuration.md`](docs/configuration.md): the full env and `.themis/config.yaml` reference.
- [`docs/security.md`](docs/security.md): the trust model and bot-side guardrails.
- [`docs/contributing-engines.md`](docs/contributing-engines.md): adding a new engine / model provider.

## Developing

Working on Themis itself needs [uv](https://docs.astral.sh/uv/) and Python 3.12, no Docker:

```bash
uv sync --locked        # install deps into .venv
uv run pytest           # run the test suite
uv run ruff check .     # lint
uv run python -m themis # run the server locally (reads THEMIS_* from the environment)
```

CI runs the same pytest and ruff commands on every push and pull request.

## License

MIT. See [`LICENSE`](LICENSE).
