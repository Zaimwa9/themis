# Local development with a tunnel

Run Themis on your laptop and let GitHub reach it through ngrok, no public
host needed.

## Start it

```bash
docker compose --profile tunnel up -d
```

This starts the `themis` service and an `ngrok` sidecar (`ngrok/ngrok:3`,
the `tunnel` profile in `../docker-compose.yml`) that tunnels to
`themis:8000`.

Set in `.env`:

```
THEMIS_TUNNEL_API=http://ngrok:4040
NGROK_AUTHTOKEN=<your ngrok authtoken>
```

`NGROK_AUTHTOKEN` needs a free ngrok account; get yours from
https://dashboard.ngrok.com/get-started/your-authtoken.

## How discovery works

At startup, when `THEMIS_PUBLIC_URL` is unset and `THEMIS_TUNNEL_API` is
set, Themis polls the ngrok agent's local API at
`${THEMIS_TUNNEL_API}/api/tunnels` for the tunnel's public `https` URL, then
calls `PATCH /app/hook/config` with the App JWT to point the GitHub App's
webhook at `<tunnel-url>/webhook`. No manual copy-pasting of the ngrok URL
into the App settings.

Free ngrok URLs change on every restart; Themis re-registers on every
startup, so this stays hands-off. If you have an ngrok static domain (the
free tier includes one), the URL is stable across restarts too, useful if
you want to avoid a webhook redelivery gap right after a restart.

## Reusing your laptop's Codex login

Mount your existing `~/.codex` directly instead of seeding a copy, via
`docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  themis:
    volumes:
      - ~/.codex:/data/codex
```

Compose merges override files automatically. Token refreshes land back in
your real `~/.codex`, so `codex` on the host and Themis in the container
share one login.

## THEMIS_PUBLIC_URL wins

If both `THEMIS_PUBLIC_URL` and `THEMIS_TUNNEL_API` are set,
`THEMIS_PUBLIC_URL` wins and the tunnel API is never queried. Unset
`THEMIS_PUBLIC_URL` to use tunnel discovery.

## Checking the tunnel

`../docker-compose.yml` doesn't publish the ngrok agent's dashboard port to
the host, only `themis` can reach `http://ngrok:4040` on the compose
network. To inspect the tunnel from your laptop:

```bash
docker compose logs ngrok
```

or add a port mapping in `docker-compose.override.yml`:

```yaml
services:
  ngrok:
    ports:
      - "4040:4040"
```

then browse `http://localhost:4040`.
