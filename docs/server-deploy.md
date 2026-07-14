# Server deployment

Themis ships as one image run in two roles: a GitHub-facing controller and a
credential-isolated agent. Any Docker host that supports two services and a
shared temporary volume works. The controller receives GitHub credentials;
the agent receives Codex or Claude credentials. Never put both credential
sets in the same service environment.

## Environment variables

The vars needed to boot the quickstart:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `THEMIS_GH_APP_CLIENT_ID` | yes | none | GitHub App client id |
| `THEMIS_GH_APP_PRIVATE_KEY` | yes | none | App private key, PEM text or base64 of it |
| `THEMIS_GH_WEBHOOK_SECRET` | yes, unless `THEMIS_WEBHOOK_ENABLED=false` | none | webhook HMAC secret, shared with the App settings |
| `THEMIS_AGENT_TOKEN` | yes | none | random bearer token shared only by controller and agent |
| `CODEX_HOME` | no | `/data/codex` | codex auth/state directory; mount a persistent volume here |
| `THEMIS_PUBLIC_URL` | no | unset | enables webhook self-registration at `<url>/webhook` |
| `PORT` | no | role default | listen port (`8000` controller, `8001` agent) |

Names and defaults come straight from `src/themis/config.py`, except `PORT`
(read in `__main__.py`) and `CODEX_HOME` (set in the Dockerfile). Full
reference, including sandboxing, repo allowlisting, headless mode, and the
tunnel profile: [`docs/configuration.md`](configuration.md).

## Volume and codex auth

Mount a persistent volume on the **agent** at `/data/codex` (`CODEX_HOME`). Codex stores its
login there and refreshes tokens in place. Without a persistent volume
you'd need to re-authenticate on every restart.

Seed it once, after the container is up, from wherever you already ran
`codex login`:

```bash
# docker compose
docker compose cp ~/.codex/auth.json agent:/data/codex/auth.json

# plain docker
docker cp ~/.codex/auth.json <container>:/data/codex/auth.json

# PaaS with only a remote shell: pipe the file in
<platform-shell-command> sh -c 'cat > /data/codex/auth.json' < ~/.codex/auth.json
```

Any of the three gets the same file onto the volume; use whichever your
platform supports.

## Self-registration

Set `THEMIS_PUBLIC_URL=https://your-host` and Themis calls
`PATCH /app/hook/config` at startup to point the App's webhook at
`${THEMIS_PUBLIC_URL}/webhook`. The App can be created with a throwaway
placeholder webhook URL (as used by the manifest bootstrap's tunnel mode) and
never touched again.

If the call fails, wrong permissions, App not installed yet, Themis logs a
warning and keeps serving; it does not crash on a self-registration
failure. Leave `THEMIS_PUBLIC_URL` unset and paste the webhook URL into the
App settings manually if you'd rather not grant self-registration, or are
running a second instance (staging) that must not steal the webhook from
production.

## Health check

`GET /healthz` returns `{"status": "ok"}` once the process is up. Wire it
into your platform's liveness or readiness probe.

## Upgrades

The shipped `docker-compose.yml` builds the image locally (`build: .`), so
`docker compose pull` does nothing by default. Two paths:

**Building from source:**

```bash
git pull
docker compose up -d --build
```

**Using published images:** images are published to `ghcr.io/<owner>/themis`
on semver tags, plus a `latest` tag for non-prerelease versions (see
`.github/workflows/release.yml`). Swap the `build: .` line in
`docker-compose.yml` for `image: ghcr.io/<owner>/themis:<tag>`, then:

```bash
docker compose pull
docker compose up -d
```

The job queue is in-memory, not durable: jobs that were queued but hadn't
started yet are lost on restart. Recovery is the same as any other lost
job, mention the bot again (`@<app-slug> review`) to re-trigger.
