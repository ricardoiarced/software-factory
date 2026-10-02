---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end. A commit that touches no Python needs no test run - `ruff format --check .` suffices.

Once done, use /code-review to review the work.

Commit your work to the current branch. End the wrap-up with one line of `git status --porcelain` and the current branch, so uncommitted or wrong-branch state is never a surprise.
