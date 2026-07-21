# Headless mode

For teams that already have GitHub webhook infrastructure. Themis exposes
the same enqueue path the bundled webhook handler uses through two HTTP
routes, so the full review and discussion feature set works without
Themis's own `/webhook` route.

The GitHub App still has to exist and be installed on the target repos
(create it with [`bootstrap.md`](bootstrap.md) or its manual fallback). Themis
still talks outbound to GitHub itself: cloning the PR, posting comments and reviews, reading
`.themis/config.yaml`. What headless mode removes is Themis's *inbound*
webhook route; your existing handler owns receiving and verifying GitHub's
deliveries, and calls these two routes instead.

## Enable it

```
THEMIS_WEBHOOK_ENABLED=false
THEMIS_API_TOKEN=<a long random token>
```

`THEMIS_WEBHOOK_ENABLED=false` removes the `/webhook` route entirely; no
`THEMIS_GH_WEBHOOK_SECRET` is needed in that mode. `THEMIS_API_TOKEN` must
be set, or Themis refuses to start (no entrypoint would be configured
otherwise).

Both routes require `Authorization: Bearer $THEMIS_API_TOKEN`
(constant-time compare), and return `404` if `THEMIS_API_TOKEN` is unset,
i.e. when the trigger API is disabled.

## POST /api/review

Enqueues a full review, same dedup id as the webhook path
(`review:{repo}#{pr_number}`).

Calls to this route count as explicit requests: a draft PR is reviewed
(only closed PRs are skipped), unlike the automatic webhook triggers,
which skip drafts. If you forward `opened`/`ready_for_review` events here
and want drafts skipped, filter on `payload["pull_request"]["draft"]` in
your handler.

Body:

```json
{"repo": "owner/name", "pr_number": 123}
```

```bash
curl -X POST https://your-themis-host/api/review \
  -H "Authorization: Bearer $THEMIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo": "owner/name", "pr_number": 123}'
```

## POST /api/discuss

Body:

```json
{
  "repo": "owner/name",
  "pr_number": 123,
  "comment_id": 456789,
  "body": "the question text",
  "kind": "thread",
  "in_reply_to_id": null,
  "mentions_bot": true,
  "author_association": "MEMBER",
  "author_login": "dev"
}
```

- `kind`: `"conversation"` for a PR-level comment, the answer posts as a new
  issue comment, or `"thread"` for an inline review thread, `comment_id` /
  `in_reply_to_id` locate the thread and the answer posts as a thread
  reply.
- `mentions_bot`: `false` preserves webhook semantics for a forwarded,
  unmentioned thread reply, Themis answers only if it already authored part
  of that thread. Pass `true` for anything the caller already knows is an
  explicit mention.
- `author_association` / `author_login` (optional): forward the commenter's
  GitHub values if you want the discussion to be able to create
  [learnings](learnings.md). Omitted, the caller is treated as untrusted
  (`"NONE"`) and capture stays off — replies still work.

```bash
curl -X POST https://your-themis-host/api/discuss \
  -H "Authorization: Bearer $THEMIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "owner/name",
    "pr_number": 123,
    "comment_id": 456789,
    "body": "why did you flag this?",
    "kind": "thread",
    "in_reply_to_id": null,
    "mentions_bot": true
  }'
```

## Responses

| Status | Meaning |
|---|---|
| `202 {"status": "queued"}` | enqueued |
| `202 {"status": "duplicate"}` | a job with the same id is already queued or running |
| `401` | missing or wrong bearer token |
| `403` | the App isn't installed on the repository |
| `404` | the trigger API is disabled (`THEMIS_API_TOKEN` unset) |

## Forwarding from an existing handler

Themis resolves the GitHub installation id itself, so your handler doesn't
need to know anything about App internals, just forward the parsed event:

```python
# inside your existing, already-signature-verified webhook handler
if event == "pull_request" and payload["action"] in ("opened", "ready_for_review"):
    await http_client.post(
        f"{THEMIS_URL}/api/review",
        json={"repo": payload["repository"]["full_name"], "pr_number": payload["number"]},
        headers={"Authorization": f"Bearer {THEMIS_API_TOKEN}"},
    )
```
