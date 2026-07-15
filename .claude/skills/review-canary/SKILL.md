---
name: review-canary
description: Use when producing an AI code review of a pull request in this repository — adds the skill-loading canary line to the review summary.
---

# Review canary (skills surface)

When writing the review summary, add exactly one extra line after the
context canary (or alone if there is none):

`🧪 canary: skill loaded`

This verifies that native skill discovery is active for the engine running
this review and that the skill body was resolved from the repository's base
revision. Do not add this line to anything other than a review summary.
