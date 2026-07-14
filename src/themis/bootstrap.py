"""One-time GitHub App manifest bootstrap for self-hosted deployments."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx

from themis.engines import ENGINE_NAMES
from themis.github.auth import make_app_jwt

GITHUB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
DEFAULT_IMAGE = "ghcr.io/zaimwa9/themis:latest"


class BootstrapError(ValueError):
    """The bootstrap could not safely complete."""


@dataclass(frozen=True)
class BootstrapOptions:
    repo: str
    output: Path
    organization: str | None
    public_url: str | None
    tunnel: bool
    ngrok_authtoken: str | None
    engine: str
    callback_url: str
    bind_host: str
    bind_port: int
    codex_auth: Path | None
    image: str
    timeout: int
    open_browser: bool


def _validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BootstrapError("--repo must be in owner/name form")
    return repo


def _validate_public_url(url: str | None) -> str | None:
    if url is None:
        return None
    url = url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise BootstrapError("--public-url must be an https origin without query or fragment")
    return url


def _validate_image(image: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", image):
        raise BootstrapError("--image is not a valid container image reference")
    return image


def _public_url_argument(value: str) -> str:
    try:
        return str(_validate_public_url(value))
    except BootstrapError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _image_argument(value: str) -> str:
    try:
        return _validate_image(value)
    except BootstrapError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _app_name(repo: str) -> str:
    owner = re.sub(r"[^a-z0-9-]", "-", repo.split("/", 1)[0].lower()).strip("-")
    return f"themis-{owner[:18]}-{secrets.token_hex(3)}"


def _dotenv(value: object) -> str:
    """Compose .env literal: no interpolation of credential-shaped values."""
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def build_manifest(options: BootstrapOptions, state: str) -> dict[str, object]:
    webhook_url = (
        f"{options.public_url}/webhook"
        if options.public_url
        else "https://example.com/webhook"
    )
    return {
        "name": _app_name(options.repo),
        "url": "https://github.com/Zaimwa9/themis",
        "redirect_url": f"{options.callback_url}/manifest/callback",
        "setup_url": f"{options.callback_url}/install/callback",
        "hook_attributes": {"url": webhook_url, "active": True},
        "public": False,
        "default_permissions": {
            "contents": "read",
            "pull_requests": "write",
            "issues": "write",
        },
        "default_events": [
            "pull_request",
            "issue_comment",
            "pull_request_review_comment",
        ],
    }


def manifest_registration_url(organization: str | None, state: str | None = None) -> str:
    if organization:
        url = f"{GITHUB_URL}/organizations/{quote(organization, safe='')}/settings/apps/new"
    else:
        url = f"{GITHUB_URL}/settings/apps/new"
    return f"{url}?state={quote(state, safe='')}" if state else url


def convert_manifest(code: str, api_url: str = GITHUB_API_URL) -> dict[str, object]:
    response = httpx.post(
        f"{api_url}/app-manifests/{quote(code, safe='')}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    required = ("client_id", "pem", "webhook_secret", "slug")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise BootstrapError(f"GitHub manifest response missing: {', '.join(missing)}")
    return payload


def verify_repo_installation(
    credentials: dict[str, object], repo: str, installation_id: int,
    api_url: str = GITHUB_API_URL,
) -> None:
    app_jwt = make_app_jwt(str(credentials["client_id"]), str(credentials["pem"]))
    response = httpx.get(
        f"{api_url}/repos/{repo}/installation",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if response.status_code == 404:
        raise BootstrapError(
            f"the App was not installed on {repo}; go back and select that repository"
        )
    response.raise_for_status()
    actual_id = int(response.json()["id"])
    if actual_id != installation_id:
        raise BootstrapError("installation callback did not match the requested repository")


def _compose_text(image: str) -> str:
    return f"""services:
  themis:
    image: ${{THEMIS_IMAGE:-{image}}}
    command: [\"python\", \"-m\", \"themis\", \"controller\"]
    environment:
      THEMIS_GH_APP_CLIENT_ID: ${{THEMIS_GH_APP_CLIENT_ID}}
      THEMIS_GH_APP_PRIVATE_KEY: ${{THEMIS_GH_APP_PRIVATE_KEY}}
      THEMIS_GH_WEBHOOK_SECRET: ${{THEMIS_GH_WEBHOOK_SECRET}}
      THEMIS_AGENT_TOKEN: ${{THEMIS_AGENT_TOKEN}}
      THEMIS_AGENT_URL: http://agent:8001
      THEMIS_ENGINE: ${{THEMIS_ENGINE:-codex}}
      THEMIS_PUBLIC_URL: ${{THEMIS_PUBLIC_URL:-}}
      THEMIS_TUNNEL_API: ${{THEMIS_TUNNEL_API:-}}
      THEMIS_WEBHOOK_ENABLED: ${{THEMIS_WEBHOOK_ENABLED:-true}}
      THEMIS_API_TOKEN: ${{THEMIS_API_TOKEN:-}}
    ports:
      - \"8000:8000\"
    volumes:
      - workspaces:/tmp/themis
    depends_on:
      agent:
        condition: service_healthy
    restart: unless-stopped

  agent:
    image: ${{THEMIS_IMAGE:-{image}}}
    command: [\"python\", \"-m\", \"themis\", \"agent\"]
    environment:
      THEMIS_AGENT_TOKEN: ${{THEMIS_AGENT_TOKEN}}
      THEMIS_WORKSPACE_ROOT: /tmp/themis
      THEMIS_CODEX_SANDBOX: ${{THEMIS_CODEX_SANDBOX:-workspace-write}}
      CLAUDE_CODE_OAUTH_TOKEN: ${{CLAUDE_CODE_OAUTH_TOKEN:-}}
      GLM_API_KEY: ${{GLM_API_KEY:-}}
      HTTP_PROXY: ${{HTTP_PROXY:-}}
      HTTPS_PROXY: ${{HTTPS_PROXY:-}}
    volumes:
      - workspaces:/tmp/themis
      - codex-home:/data/codex
    depends_on:
      codex-init:
        condition: service_completed_successfully
    healthcheck:
      test: [\"CMD\", \"curl\", \"-fsS\", \"http://localhost:8001/healthz\"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  codex-init:
    image: ${{THEMIS_IMAGE:-{image}}}
    user: "0:0"
    command:
      - sh
      - -c
      - >-
        if [ -f /seed/auth.json ] && [ ! -f /data/codex/auth.json ]; then
          install -o themis -g themis -m 600 /seed/auth.json /data/codex/auth.json;
        fi
    volumes:
      - ./codex-seed:/seed:ro
      - codex-home:/data/codex
    restart: "no"

  ngrok:
    image: ngrok/ngrok:3
    profiles: [\"tunnel\"]
    command: [\"http\", \"themis:8000\"]
    environment:
      NGROK_AUTHTOKEN: ${{NGROK_AUTHTOKEN:-}}
    restart: unless-stopped

volumes:
  workspaces:
  codex-home:
"""


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    """Create a file once with its final permissions; never expose secret bytes."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def write_deployment(options: BootstrapOptions, credentials: dict[str, object]) -> None:
    output = options.output
    output.mkdir(parents=True, exist_ok=True)
    env_path = output / ".env"
    compose_path = output / "compose.yaml"
    info_path = output / "themis-info.json"
    for path in (env_path, compose_path, info_path):
        if path.exists():
            raise BootstrapError(f"refusing to overwrite existing {path}")

    private_key = base64.b64encode(str(credentials["pem"]).encode()).decode()
    lines = [
        f"THEMIS_GH_APP_CLIENT_ID={_dotenv(credentials['client_id'])}",
        f"THEMIS_GH_APP_PRIVATE_KEY={_dotenv(private_key)}",
        f"THEMIS_GH_WEBHOOK_SECRET={_dotenv(credentials['webhook_secret'])}",
        f"THEMIS_AGENT_TOKEN={_dotenv(secrets.token_hex(32))}",
        f"THEMIS_ENGINE={_dotenv(options.engine)}",
        f"THEMIS_PUBLIC_URL={_dotenv(options.public_url or '')}",
        f"THEMIS_TUNNEL_API={_dotenv('http://ngrok:4040' if options.tunnel else '')}",
        f"NGROK_AUTHTOKEN={_dotenv(options.ngrok_authtoken or '')}",
        "CLAUDE_CODE_OAUTH_TOKEN=''",
        "GLM_API_KEY=''",
    ]
    _write_exclusive(env_path, ("\n".join(lines) + "\n").encode(), 0o600)
    _write_exclusive(compose_path, _compose_text(options.image).encode(), 0o644)
    info = {
        "github_app_slug": str(credentials["slug"]),
        "mention": f"@{credentials['slug']}",
        "repository": options.repo,
    }
    _write_exclusive(
        info_path, (json.dumps(info, indent=2, sort_keys=True) + "\n").encode(), 0o644
    )

    codex_seed = output / "codex-seed"
    codex_seed.mkdir(mode=0o700, exist_ok=True)
    codex_seed.chmod(0o700)
    if options.codex_auth:
        destination = codex_seed / "auth.json"
        _write_exclusive(destination, options.codex_auth.read_bytes(), 0o600)


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px system-ui;max-width:720px;margin:4rem auto;padding:0 1rem}"
        "code{background:#eee;padding:.2rem .4rem}button{font:inherit;padding:.6rem 1rem}</style>"
        f"<h1>{html.escape(title)}</h1>{body}"
    ).encode()


class BootstrapSession:
    def __init__(self, options: BootstrapOptions):
        self.options = options
        self.state = secrets.token_urlsafe(32)
        self.manifest = build_manifest(options, self.state)
        self.credentials: dict[str, object] | None = None
        self.deployment_written = False
        self.done = threading.Event()
        self.error: Exception | None = None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, content: bytes, location: str | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/":
                        self._start()
                    elif parsed.path == "/manifest/callback":
                        self._manifest_callback(parse_qs(parsed.query))
                    elif parsed.path == "/install/callback":
                        self._install_callback(parse_qs(parsed.query))
                    else:
                        self._send(404, _page("Not found", "<p>Unknown bootstrap route.</p>"))
                except Exception as error:
                    session.error = error
                    message = html.escape(str(error))
                    self._send(400, _page("Setup could not continue", f"<p>{message}</p>"))

            def _start(self) -> None:
                action = html.escape(
                    manifest_registration_url(session.options.organization, session.state)
                )
                manifest = html.escape(json.dumps(session.manifest))
                body = (
                    "<p>GitHub will ask you to confirm creation of a private GitHub App.</p>"
                    f'<form method="post" action="{action}">'
                    f'<input type="hidden" name="manifest" value="{manifest}">'
                    '<button type="submit">Continue to GitHub</button></form>'
                )
                self._send(200, _page("Create the Themis GitHub App", body))

            def _manifest_callback(self, query: dict[str, list[str]]) -> None:
                if query.get("state", [None])[0] != session.state:
                    raise BootstrapError("GitHub callback state did not match")
                code = query.get("code", [None])[0]
                if not code:
                    raise BootstrapError("GitHub callback did not include a manifest code")
                if session.credentials is None:
                    session.credentials = convert_manifest(code)
                if not session.deployment_written:
                    write_deployment(session.options, session.credentials)
                    session.deployment_written = True
                slug = quote(str(session.credentials["slug"]), safe="")
                install_url = f"{GITHUB_URL}/apps/{slug}/installations/new?state={session.state}"
                content = _page("App created", "<p>Continuing to repository installation…</p>")
                self._send(303, content, install_url)

            def _install_callback(self, query: dict[str, list[str]]) -> None:
                if query.get("state", [None])[0] != session.state:
                    raise BootstrapError("GitHub installation state did not match")
                if session.credentials is None:
                    raise BootstrapError("App credentials were not received before installation")
                raw_id = query.get("installation_id", [None])[0]
                if not raw_id or not raw_id.isdigit():
                    raise BootstrapError("GitHub callback did not include an installation id")
                verify_repo_installation(session.credentials, session.options.repo, int(raw_id))
                session.done.set()
                body = (
                    f"<p>The App is installed on <code>{html.escape(session.options.repo)}</code>.</p>"
                    f"<p>Your bot is <strong>@{html.escape(str(session.credentials['slug']))}</strong>. "
                    f"Call it with <code>@{html.escape(str(session.credentials['slug']))} review</code>.</p>"
                    "<p>You can close this window and return to the terminal.</p>"
                )
                self._send(200, _page("Themis GitHub setup complete", body))

        return Handler


def run_bootstrap(options: BootstrapOptions) -> None:
    _validate_repo(options.repo)
    _validate_image(options.image)
    _validate_public_url(options.public_url)
    if options.bind_port <= 0 or options.timeout <= 0:
        raise BootstrapError("callback port and timeout must be positive")
    if options.public_url and options.tunnel:
        raise BootstrapError("choose either --public-url or --tunnel")
    if not options.public_url and not options.tunnel:
        raise BootstrapError("one of --public-url or --tunnel is required")
    if options.tunnel and not options.ngrok_authtoken:
        raise BootstrapError("NGROK_AUTHTOKEN is required with --tunnel")
    if options.codex_auth and not options.codex_auth.is_file():
        raise BootstrapError(f"Codex auth file not found: {options.codex_auth}")
    if (options.output / ".env").exists() or (options.output / "compose.yaml").exists():
        raise BootstrapError("output already contains .env or compose.yaml")

    session = BootstrapSession(options)
    server = ThreadingHTTPServer((options.bind_host, options.bind_port), session.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start_url = f"{options.callback_url}/"
    print(f"Open this URL to authorize the GitHub App:\n\n  {start_url}\n", flush=True)
    if options.open_browser:
        webbrowser.open(start_url)
    try:
        if not session.done.wait(options.timeout):
            if session.error:
                raise BootstrapError(f"setup did not complete: {session.error}")
            raise BootstrapError("timed out waiting for GitHub App setup")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if session.credentials is None:  # guarded by session.done, narrows the type
        raise BootstrapError("GitHub App credentials were not received")
    mention = f"@{session.credentials['slug']}"
    profile = " --profile tunnel" if options.tunnel else ""
    print(
        f"\nGitHub bot: {mention}\n"
        f"Request a review with: {mention} review\n"
        f"Deployment details: {options.output / 'themis-info.json'}\n\n"
        f"Deployment written to {options.output}\n"
        f"Start it with: docker compose -f {options.output / 'compose.yaml'}{profile} up -d"
    )


def add_init_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("init", help="create and install a GitHub App from a manifest")
    parser.add_argument("--repo", required=True, help="repository to install on (owner/name)")
    parser.add_argument("--organization", help="organization that should own the App")
    parser.add_argument("--output", type=Path, default=Path.cwd())
    reachability = parser.add_mutually_exclusive_group(required=True)
    reachability.add_argument("--public-url", type=_public_url_argument)
    reachability.add_argument("--tunnel", action="store_true", help="use the bundled ngrok tunnel")
    parser.add_argument("--engine", choices=ENGINE_NAMES, default="codex")
    parser.add_argument("--codex-auth", type=Path, help="auth.json to seed into the agent")
    parser.add_argument("--image", type=_image_argument, default=DEFAULT_IMAGE)
    parser.add_argument("--callback-port", type=_positive_int, default=8976)
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--callback-host", default="127.0.0.1")
    parser.add_argument("--timeout", type=_positive_int, default=3600)
    parser.add_argument("--no-browser", action="store_true")


def options_from_args(args: argparse.Namespace) -> BootstrapOptions:
    callback_url = f"http://{args.callback_host}:{args.callback_port}"
    codex_auth = args.codex_auth
    default_auth = Path.home() / ".codex" / "auth.json"
    if codex_auth is None and args.engine == "codex" and default_auth.is_file():
        codex_auth = default_auth
    return BootstrapOptions(
        repo=args.repo,
        output=args.output.resolve(),
        organization=args.organization,
        public_url=args.public_url,
        tunnel=args.tunnel,
        ngrok_authtoken=os.getenv("NGROK_AUTHTOKEN"),
        engine=args.engine,
        callback_url=callback_url,
        bind_host=args.bind_host,
        bind_port=args.callback_port,
        codex_auth=codex_auth,
        image=args.image,
        timeout=args.timeout,
        open_browser=not args.no_browser,
    )
