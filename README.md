# Themis

Themis is a self-hosted GitHub PR review bot that runs on your own Codex or
Claude subscription. It reviews pull requests with inline findings and a structured
summary (verdict, scoring table, severity-ordered sections), answers
questions in review threads and PR conversation, and takes its review
doctrine from your own repository, under `.themis/`.

<!-- screenshot: docs/assets/review-example.png -->

## How it works

A GitHub App webhook delivers PR and comment events to Themis. Each event
becomes a job on an in-memory queue, processed one at a time. The worker
shallow-clones the PR head, runs the configured engine (`codex exec` or
`claude -p`) against your repo's review doctrine, and posts findings and a
summary back to GitHub as the App. One container, no external services: no
database, no Redis, no message broker.

## Prerequisites

- Docker with the Compose plugin (Docker Desktop, or Docker Engine + `docker compose`)
- An OpenAI account with Codex access, or a Claude Pro/Max subscription (pick your engine): the matching CLI installed (`npm install -g @openai/codex` or `npm install -g @anthropic-ai/claude-code`, Node 22+) and `codex login` or `claude setup-token` working on your machine
- A GitHub account that can create a GitHub App (personal account or an org)

No clone or build needed: Themis ships as a prebuilt multi-arch image,
`ghcr.io/zaimwa9/themis`.

## Quickstart

Setting this up is mostly mechanical; feel free to hand this README to the
coding agent you already run (Claude Code, Codex, ...) on the machine that
will host Themis, and only do the GitHub App clicks yourself.

### 1. Create the GitHub App

Go to `github.com/settings/apps` (or your org's Settings > Developer
settings > GitHub Apps) and click **New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | Your choice, e.g. `my-reviewer`. This becomes the bot's mention: `@my-reviewer`. |
| Homepage URL | Anything, e.g. this repo's URL |
| Webhook URL | A placeholder (`https://example.com/webhook`) if you'll self-register or use the tunnel profile; otherwise your real `https://<host>/webhook` |
| Webhook secret | Generate one: `openssl rand -hex 20` |

Permissions:

| Permission | Access |
|---|---|
| Contents | Read-only |
| Pull requests | Read and write |
| Issues | Read and write |

Subscribe to events: `pull_request`, `issue_comment`, `pull_request_review_comment`.

Then, on the App's page: **Generate a private key** (downloads a `.pem`
file), and **Install App** on the repositories you want reviewed.

### 2. Log in to Codex

Using the claude engine instead? Skip this step and the auth.json seeding in
step 4: run `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN` plus
`THEMIS_ENGINE=claude` in `.env` during step 3. Details in
[Engines](#engines).

Install the CLI if you haven't already (Node 22+):

```bash
npm install -g @openai/codex
```

```bash
codex login
```

This writes credentials to `~/.codex/auth.json`. Themis reuses this file
(step 4).

### 3. Configure

Create a directory for the deployment with two files. `docker-compose.yml`:

```yaml
services:
  themis:
    image: ghcr.io/zaimwa9/themis:latest
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - codex-home:/data/codex
    restart: unless-stopped

  # Optional: local tunnel for hosts without a public URL (step 5).
  ngrok:
    image: ngrok/ngrok:3
    profiles: ["tunnel"]
    command: ["http", "themis:8000"]
    environment:
      NGROK_AUTHTOKEN: ${NGROK_AUTHTOKEN:-}
    restart: unless-stopped

volumes:
  codex-home:
```

And `.env` with the three required variables (full reference:
[`docs/configuration.md`](docs/configuration.md)):

```bash
THEMIS_GH_APP_CLIENT_ID=
THEMIS_GH_APP_PRIVATE_KEY=
THEMIS_GH_WEBHOOK_SECRET=
```

- `THEMIS_GH_APP_CLIENT_ID`: the App's Client ID, from the App's settings page
- `THEMIS_GH_APP_PRIVATE_KEY`: the private key from step 1, base64-encoded,
  paste the output:
  - macOS: `base64 -i key.pem | tr -d '\n'`
  - Linux: `base64 -w0 key.pem`
- `THEMIS_GH_WEBHOOK_SECRET`: the same secret you generated in step 1

### 4. Run it

Server (VPS, always-on host, PaaS):

```bash
docker compose up -d
cat ~/.codex/auth.json | docker compose exec -T themis sh -c 'cat > /data/codex/auth.json'
```

The second command seeds Codex's login into the container's volume once;
codex refreshes its own tokens in place after that.

Local machine, reusing your existing login instead of seeding a copy:
create `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  themis:
    volumes:
      - ~/.codex:/data/codex
```

Compose merges override files automatically; no seeding step needed.

### 5. Wire up the webhook

Pick one:

- Set `THEMIS_PUBLIC_URL=https://your-host` in `.env`. Themis registers
  `<url>/webhook` as the App's webhook URL at startup, so the placeholder
  from step 1 is never touched again.
- Or paste `https://<your-host>/webhook` into the App's webhook settings
  manually.

Self-registration only runs at startup: after editing `.env`, run `docker
compose up -d` again to pick it up (Compose recreates the container when
`.env` changes).

Running locally with no public host yet, use the bundled tunnel instead:

```bash
docker compose --profile tunnel up -d
```

with `THEMIS_TUNNEL_API=http://ngrok:4040` and `NGROK_AUTHTOKEN=<your token>`
set in `.env`. `NGROK_AUTHTOKEN` needs a free ngrok account; get yours from
https://dashboard.ngrok.com/get-started/your-authtoken. Themis discovers the
ngrok URL and self-registers the webhook. Details:
[`docs/local-tunnel.md`](docs/local-tunnel.md).

### 6. Verify

```bash
curl localhost:8000/healthz
# {"status":"ok"}
```

Check `docker compose logs themis` for the startup line reporting the
resolved App slug. Open a test PR against a repo the App is installed on:
expect a 👀 reaction on the trigger, then a 🚀 reaction on the PR once the
review starts, then the review itself.

## Customize reviews

Copy the starter kit into the target repo:

```bash
cp -r examples/themis .themis
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
| `web_access` | `false` | `true` gives the agent network access; enable for doctrines that verify external API contracts |
| `model.name` | engine default | `gpt-5.4` (codex) or `claude-opus-4-6[1m]` (claude) |
| `model.reasoning_effort` | `high` | `low` \| `medium` \| `high` (codex only) |
| `limits.timeout_seconds` | `1200` | per agent attempt |
| `limits.max_attempts` | `2` | attempts before posting a failure comment |
| `limits.clone_depth` | `50` | shallow clone depth |
| `triggers.auto_review` | `true` | `false` = mention-only, no auto-review when a PR opens or is marked ready for review |

Talk to the bot in a PR: `@<app-slug> review` re-reviews on demand,
`@<app-slug> <question>` asks a question, and replies inside a thread the
bot already posted in are answered automatically, no mention needed.

## Engines

Themis runs reviews through one of two agent CLIs, both on your own subscription:

| Engine | Auth | Setup |
|---|---|---|
| `codex` (default) | `auth.json` volume (`CODEX_HOME`) | `codex login` locally (quickstart step 2), copy `auth.json` into the volume (quickstart step 4) |
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
| Auth that worked starts failing months later | Re-seed `auth.json` (`codex login` locally, then repeat the seeding step from Quickstart step 4). |
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
