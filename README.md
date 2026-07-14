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
shallow-clones the PR head, runs the configured engine (`codex exec` or
`claude -p`) against your repo's review doctrine, and posts findings and a
summary back to GitHub as the App. One image runs as an isolated controller
and agent; there is still no database, Redis, or message broker.

## Prerequisites

- Docker with the Compose plugin (Docker Desktop, or Docker Engine + `docker compose`)
- An OpenAI account with Codex access, or a Claude Max subscription (pick your engine): the matching CLI installed (`npm install -g @openai/codex` or `npm install -g @anthropic-ai/claude-code`, Node 22+) and `codex login` or `claude setup-token` working on your machine. Claude Pro is not supported because Themis defaults to Opus.
- A GitHub account that can create a GitHub App (personal account or an org)

No clone or build needed: Themis ships as a prebuilt image,
`ghcr.io/zaimwa9/themis`.

## Quickstart

The bootstrap uses GitHub's App Manifest flow to create the App, generate its
private key and webhook secret, install it on the requested repository, and
write a ready-to-run deployment. There are no GitHub settings to copy. GitHub
still asks the account owner to approve App creation and repository access.

### 1. Log in to Codex

Using the Claude engine instead? Run `claude setup-token`, pass
`--engine claude` to the bootstrap, and put the resulting token in
`CLAUDE_CODE_OAUTH_TOKEN` in the generated `.env`. Details in
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

Detailed options, the tunnel command, recovery, and the manual fallback are in
[`docs/bootstrap.md`](docs/bootstrap.md).

Creating the GitHub App manually instead? Configure these repository
permissions:

| Permission | Access |
|---|---|
| Checks | Read-only |
| Contents | Read-only |
| Issues | Read and write |
| Pull requests | Read and write |
| Commit statuses | Read-only |

Subscribe to `pull_request`, `issue_comment`, and
`pull_request_review_comment`. Actions permission is not required. Existing
Apps must also be updated with the Checks and Commit statuses permissions before
Themis can include CI context in reviews.

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
| `engine` | instance `THEMIS_ENGINE` | `codex` or `claude`, overrides the instance's default engine for this repo |
| `web_access` | `false` | toggles engine web tooling; Claude's Bash may still egress unless the deployment enforces an external network policy |
| `model.name` | engine default | `gpt-5.4` (codex) or `claude-opus-4-6[1m]` (claude) |
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

Themis runs reviews through one of two agent CLIs, using your Codex or Claude Max subscription:

| Engine | Auth | Setup |
|---|---|---|
| `codex` (default) | `auth.json` volume (`CODEX_HOME`) | `codex login` locally; bootstrap copies `auth.json` into the generated volume |
| `claude` | one env var | run `claude setup-token` locally, set `CLAUDE_CODE_OAUTH_TOKEN` in `.env` |

Pick the instance default with `THEMIS_ENGINE` in `.env`. A repo can override it
in `.themis/config.yaml` with `engine: claude` or `engine: codex`; if that engine
has no credentials on the instance, Themis posts a comment saying so instead of
failing silently. The claude path needs no volume: token in `.env`, done.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Crashes at startup naming an env var | Set that variable; Themis fails fast on missing or invalid required config. |
| Crashes at startup on a `GET /app` call | Wrong `THEMIS_GH_APP_CLIENT_ID` or malformed `THEMIS_GH_APP_PRIVATE_KEY`. |
| Codex sandbox errors | Set `THEMIS_CODEX_SANDBOX=danger-full-access`; the container is the sandbox boundary on runtimes without Landlock. |
| PR comment says the usage limit was reached | Your Codex or Claude subscription (whichever engine ran the job) has hit its usage window. Mention the bot again once it resets. |
| Auth that worked starts failing months later | Run `codex login` locally, then refresh the persistent agent credential using the command in [Automated setup: Refreshing Codex authentication](docs/bootstrap.md#refreshing-codex-authentication). |
| Review comment says engine credentials missing | Set `CLAUDE_CODE_OAUTH_TOKEN` (claude) or seed the codex auth volume (codex), or change `THEMIS_ENGINE` / the repo's `engine:` key. |
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
