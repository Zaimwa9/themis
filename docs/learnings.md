# Learnings

Themis remembers review conventions per repository. When a trusted human
corrects the bot in a PR discussion or states a preference — or explicitly
asks `@themis remember <rule>` — Themis distills it into a one-line learning
and applies it to every future review of that repo.

## How a learning is born

1. You reply in a PR discussion (inline thread or conversation) — for
   example: *"we prefer reusing the manager method here"* or
   `@themis remember: never introduce raw SQL in handlers`.
2. If your reply states a durable, generalizable rule, Themis writes it to a
   **pending buffer** on the Themis host and marks the reply thread with a
   🧠 footer. Facts about the current PR, or anything a linter already
   enforces, are deliberately not captured.
3. Once `digest_threshold` learnings are pending, Themis opens (or updates)
   **one digest PR** from the `themis/learnings` branch that appends them to
   `.themis/learnings.jsonl`. Review it like any PR: edit lines, delete bad
   ones, then merge. What you merge is what future reviews read.

Publishing the digest PR needs the App's **Contents: Read and write**
permission (bootstrap-created Apps request it; older Apps must upgrade —
see [bootstrap.md](bootstrap.md)). Until the permission is granted the
digest write fails with a logged warning and learnings simply stay pending.

The `themis/learnings` branch is never force-pushed. If a branch by that
name already exists with its own commits (yours, or a digest PR you closed
without merging), Themis leaves it alone, logs
`themis_digest_branch_conflict`, and keeps the learnings pending; delete or
rename that branch to let the digest flow resume. After a digest PR merges,
Themis deletes its own branch automatically.

## Trust model

- Only comments from authors with `OWNER`, `MEMBER`, or `COLLABORATOR`
  association can create learnings. Drive-by "remember this" comments from
  strangers are dropped server-side, whatever the reply says.
- Learnings enter prompts as data with explicit no-override framing: they
  can refine focus and style expectations but can never suppress findings,
  change severities, or override the review doctrine. A learning that tries
  is ignored and called out in the review summary.
- The merged file is the single source of truth. To delete or edit a
  learning, edit `.themis/learnings.jsonl` in a normal PR (or directly in
  the digest PR before merging).

## The file

`.themis/learnings.jsonl`, one JSON object per line:

```json
{"id": "lrn-a3f9c2d1", "text": "Prefer FeatureState.objects.get_live_feature_states(...) over duplicating the live filter.", "paths": ["api/features/models.py"], "learnt_from": "dev", "pr": 42, "created_at": "2026-07-13T09:00:00+00:00"}
```

`paths` scopes the rule ([] = repo-wide); `supersedes` (optional) points at
a learning this one replaces. Malformed lines are skipped with a warning —
a broken file never blocks reviews.

## Opting out

```yaml
# .themis/config.yaml
learnings:
  enabled: false
```

disables capture, injection, and the digest PR for that repo. The pending
buffer lives under `THEMIS_DATA_ROOT` on the Themis host; deleting a repo's
`learnings/<owner>__<repo>/` directory there forgets its unmerged learnings.

## Headless note

`/api/discuss` callers default to `author_association: "NONE"` (untrusted);
pass the real association if you want API-driven discussions to create
learnings.
