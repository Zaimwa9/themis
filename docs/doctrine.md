# The review doctrine: how it works and how to write a good one

The doctrine is the file `.themis/review.md` in the reviewed repository. It is
the main thing you customize: it turns a generic reviewer into one that knows
your codebase, your severity bar, and your house rules.

## How Themis uses it

- On every review, Themis clones the PR head and tells the agent to read
  `.themis/review.md` from that checkout. The file comes from the **PR's own
  branch**, so doctrine changes can be reviewed like any other change and can
  reference the code as it looks in that PR.
- The doctrine calibrates judgment only. The output format (verdict line,
  scoring table, severity sections, inline finding shape) is fixed by Themis's
  own prompt and cannot be changed from the doctrine.
- If the file is missing or unreadable, the review still runs with the
  built-in contract and default judgment. A broken `.themis/` never blocks a
  review.
- `.themis/config.yaml` (same directory) holds behavior knobs: engine, model,
  timeouts, auto-review. Unlike the doctrine, it is read from the repo's
  **default branch**, so a PR cannot flip its own settings (for example
  `web_access`). Full key reference: [`configuration.md`](configuration.md).

Because the doctrine rides on the PR branch, a malicious PR can rewrite its
own review instructions. Themis's guardrails (no GitHub access from the agent,
env allowlist, outbound redaction) hold regardless; see
[`security.md`](security.md).

## Starting point

Copy the starter kit into the target repo and edit from there. This needs a
temporary shallow checkout of Themis:

```bash
starter="$(mktemp -d)"
git clone --depth 1 https://github.com/Zaimwa9/themis.git "$starter"
cp -r "$starter/examples/themis" .themis
```

For a working example, see this repository's own
[`.themis/review.md`](../.themis/review.md): Themis reviews itself with it.

## What each section is for

**Philosophy.** The reviewer's priorities in a few bullets. State what matters
most in your codebase (correctness? security? API stability?) and what the
reviewer should not do (audit unrelated code, praise, restyle). Keep the
bullets that match your taste from the starter kit and add one or two that are
specific to your domain.

**Severity calibration.** This is the highest-leverage section. Define
Blocker/Major/Nit in terms of *your* failure modes, not abstract ones: name
the things that are always Blockers in your codebase (a secret in a log line,
a missing permission check, an unguarded migration) and where the
Major-versus-Nit line sits. Ambiguity here is what produces noisy reviews.

**Codebase map.** One line per directory or module: what lives there and any
rule attached to it ("no business logic in the HTTP layer"). This is what lets
the reviewer open the right files instead of exploring, so reviews get faster
and cheaper. Keep it to the level a new senior hire would need on day one.

**House rules.** Conventions the reviewer should enforce that a linter cannot:
test expectations ("bug fixes ship with a regression test"), dependency
policy, documentation requirements, logging conventions. Make each rule
checkable from a diff; "write clean code" is not enforceable, "new endpoints
need an integration test" is.

**Verification habits.** Optional. Tells the reviewer when to double-check
claims against external sources (API field limits, library behavior) instead
of asserting from memory. Pair it with `web_access: true` in
`.themis/config.yaml` if your doctrine expects live documentation lookups;
without it the agent can still read the pinned dependencies' source in the
checkout.

## Writing rules that work

- **Short beats complete.** The doctrine is read on every review. A focused
  page outperforms an exhaustive style guide; link out or trim rather than
  grow past roughly 100 lines.
- **Concrete beats abstract.** "Flag SQL built by string concatenation" lands;
  "be careful about security" does nothing.
- **Calibrate with real misses.** When the reviewer flags something you don't
  care about, or misses something you do, encode that case in the severity
  section. Treat the doctrine as tuned-over-time configuration, not a
  write-once document.
- **Don't restate the output format.** Verdict shapes, emoji, comment layout
  are fixed; instructions about them are ignored noise.
- **Don't fight the diff scope.** The reviewer reviews the diff. Rules that
  require whole-repo knowledge ("ensure this is consistent everywhere") won't
  be followed reliably.

## What you cannot customize from the doctrine

- The output contract: verdict line, scoring table, severity section names,
  inline comment shape, the nit budget.
- Posting behavior: what gets posted where, thread resolution rules,
  redaction. These are bot-side guardrails (see [`security.md`](security.md)).
- Engine, model, and timeouts: those live in `.themis/config.yaml`, on the
  default branch, out of the PR author's reach.
