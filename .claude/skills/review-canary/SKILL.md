---
name: review-canary
description: Use when producing an AI code review of a pull request in this repository — adds the skill-loading canary line to the review summary.
---

# Review canary (skills surface)

When writing the review summary, add exactly one extra line at the very end:

`🧪 canary: skill loaded`

This verifies that skill discovery (native or bridged) is active and that
the skill body was resolved from the PR base revision. Do not add this line
to anything other than a review summary.
