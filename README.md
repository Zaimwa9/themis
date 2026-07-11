# Themis

Themis is a self-hosted GitHub PR review bot that runs on your own Codex
subscription. It reviews pull requests with inline findings and a structured
summary (verdict, scoring table, severity-ordered sections), answers
questions in review threads and PR conversation, and takes its review
doctrine from your own repository, under `.themis/`.

<!-- screenshot: docs/assets/review-example.png -->

## How it works

A GitHub App webhook delivers PR and comment events to Themis. Each event
becomes a job on an in-memory queue, processed one at a time. The worker
shallow-clones the PR head, runs `codex exec` against your repo's review
doctrine, and posts findings and a summary back to GitHub as the App. One
container, no external services: no database, no Redis, no message broker.

## Prerequisites

- Docker with the Compose plugin (Docker Desktop, or Docker Engine + `docker compose`)
- A Codex subscription, with the CLI installed (`npm install -g @openai/codex`, Node 22+) and `codex login` working on your machine
- A GitHub account that can create a GitHub App (personal account or an org)

## Quickstart

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

Install the CLI if you haven't already (Node 22+):

```bash
npm install -g @openai/codex
```

```bash
codex login
```

This writes credentials to `~/.codex/auth.json`. Themis reuses this file
(step 4).

### 3. Clone and configure

```bash
git clone <this-repo-url> && cd themis
cp .env.example .env
```

Fill in the three required variables in `.env`:

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

| Key | Default | Meaning |
|---|---|---|
| `model.name` | `gpt-5.4` | codex model |
| `model.reasoning_effort` | `high` | `low` \| `medium` \| `high` |
| `limits.timeout_seconds` | `1200` | per codex attempt |
| `limits.max_attempts` | `2` | attempts before posting a failure comment |
| `limits.clone_depth` | `50` | shallow clone depth |
| `triggers.auto_review` | `true` | `false` = mention-only, no auto-review when a PR opens or is marked ready for review |

Talk to the bot in a PR: `@<app-slug> review` re-reviews on demand,
`@<app-slug> <question>` asks a question, and replies inside a thread the
bot already posted in are answered automatically, no mention needed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Crashes at startup naming an env var | Set that variable; Themis fails fast on missing or invalid required config. |
| Crashes at startup on a `GET /app` call | Wrong `THEMIS_GH_APP_CLIENT_ID` or malformed `THEMIS_GH_APP_PRIVATE_KEY`. |
| Codex sandbox errors | Set `THEMIS_CODEX_SANDBOX=danger-full-access`; the container is the sandbox boundary on runtimes without Landlock. |
| PR comment says the usage limit was reached | Your Codex subscription's usage window is exhausted. Mention the bot again once it resets. |
| Auth that worked starts failing months later | Re-seed `auth.json` (`codex login` locally, then repeat the seeding step from Quickstart step 4). |
| Webhook deliveries show 401 in the App's settings | `THEMIS_GH_WEBHOOK_SECRET` doesn't match the App's webhook secret. |
| Where are the logs | `docker compose logs -f themis` |
| A job queued right before a restart never ran | The in-memory queue doesn't survive restarts; mention the bot again to re-trigger. |
| Opened a PR, nothing happened | Check in order: PR is a draft (skipped until marked ready); PR author is a bot account (ignored); `auto_review: false` in `.themis/config.yaml`; the GitHub App isn't installed on that repo; webhook deliveries are failing (App settings > Advanced > Recent Deliveries). |

## Documentation

- [`docs/server-deploy.md`](docs/server-deploy.md): deploying to any Docker host or PaaS, the full env var reference, upgrades.
- [`docs/local-tunnel.md`](docs/local-tunnel.md): the ngrok tunnel profile in depth.
- [`docs/headless.md`](docs/headless.md): bring your own webhook handler, the `/api/review` and `/api/discuss` contracts.
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
