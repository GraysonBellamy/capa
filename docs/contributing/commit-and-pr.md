# Commits and PRs

**Audience:** contributors preparing changes to land on `main`.
**Scope:** subject-line style, when to bundle vs. split, PR body content, the (incomplete) CI gating story today, and review etiquette.

CAPA is pre-alpha (see the [project README](https://github.com/GraysonBellamy/capa/blob/main/README.md)) with a small contributor base. The conventions on this page are deliberately light. They will get heavier when the project leaves pre-alpha; flag anything here that has drifted from current practice.

---

## Commit subjects

Short, imperative, no enforced prefix. Looking at recent `git log --oneline` on `main`:

```text
Update heat flux tuning
Add heat flux tuning
Revamp nidaq channel handling
GUI enhancements
Update device API
Code cleanup
Major UI improvements and overhaul. Setup tab added with full capabilities
Add additional watlow adapter setpoint test
New shutdown manager and webcam bug fixes
```

The convention:

- Imperative voice (`Add`, `Update`, `Revamp`, `Fix`, `Remove`), not past tense.
- Subject under ~70 characters where reasonable.
- No mandatory conventional-commits prefix (`feat:`, `fix:`, etc.). Some sibling projects (e.g. alicatlib) document those prefixes; capa doesn't enforce them. Using them is fine if you want to, but don't gate review on their presence.
- A second sentence after a period is acceptable when one short subject can't carry the meaning (`Major UI improvements and overhaul. Setup tab added with full capabilities`).

What to avoid:

- Useless subjects (`wip`, `stuff`, `final`).
- Subject lines that only restate the diff (`Change foo.py`) — say what the change *does* (`Switch foo to per-resource workers`).
- Trailing punctuation on the subject.

---

## Commit body

Optional but encouraged for non-trivial changes. Use it for the *why*. The diff already shows the *what*.

- Reference the issue or discussion the change came out of.
- Note any behavior change that an operator or downstream tool would notice.
- Call out follow-up work explicitly so the next person knows it's intentionally unfinished.

---

## When to bundle vs. split a PR

Default: **one logical change per PR.** "Logical" means a reviewer can hold the whole change in their head at once.

When a single PR can legitimately be big:

- A refactor that has to land atomically because intermediate states wouldn't pass tests (`Major architectural overhaul` in capa's history is an example).
- A feature whose UI, runtime wiring, and config plumbing are tangled enough that splitting them produces three PRs none of which makes sense alone.

When to split:

- The PR contains two changes that could land in either order (split them into two).
- A reviewer would have to context-switch between unrelated subsystems mid-review.
- The diff includes "drive-by cleanups" unrelated to the stated goal — those go in a separate PR so they don't fail review for the wrong reason.

A bundled refactor is fine; a bundled "fix two unrelated bugs and add a feature" is not.

---

## PR body

Match the format used elsewhere in the project — short, structured, scannable:

```markdown
## Summary
- Brief bullet of what changed
- Brief bullet of what it enables
- Brief bullet of any follow-up

## Test plan
- [ ] uv run pytest
- [ ] uv run ruff check
- [ ] uv run mypy
- [ ] Manual: launched `uv run capa gui configs/experiments/sim_freerun.yaml`, did X, observed Y
```

For UI changes, include a before/after screenshot (the [UI probe](screenshot-probe.md) is built for exactly this).

For changes that touch device adapters, run the corresponding [hardware smoke test](hardware-tests.md) on the rig and paste the relevant assertion line into the PR body.

---

## CI gating today — and what's missing

The only configured workflow today is [.github/workflows/docs.yml](../../.github/workflows/docs.yml). It runs `uv run zensical build --clean` on push and pull-request, and deploys the rendered site to GitHub Pages on push to `main`.

**There is no Python CI workflow.** `ruff check`, `mypy`, and `pytest` are not run automatically on pull requests. This is a known gap in pre-alpha; contributors are responsible for running them locally before pushing:

```powershell
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
```

If a PR lands with any of these failing on `main`, that's a process miss, not a CI miss. Until the Python CI workflow exists, document in the PR's test plan which of these you ran.

The plan is to add a Python CI workflow in the same family as `docs.yml` — `actions/checkout`, `astral-sh/setup-uv`, `uv sync --extra dev`, then run the four gates. If you want to take that on as a separate PR, it's welcome; coordinate with the maintainer first to agree on the matrix (Python version, OS).

---

## Reviews

Today the project has one maintainer (Grayson Bellamy). Review etiquette is therefore informal:

- Tag the maintainer on the PR; expect a response within a few working days.
- For changes to safety-critical paths (anything in `src/capa/runtime/saturation.py`, the [authorization gate](../safety/authorization-gates.md), the [shutdown sequence](../safety/shutdown-sequence.md), or the bundle finalize/seal path), the maintainer reviews directly — no auto-merge, no fast path.
- For docs-only PRs, a single approving review and green `docs.yml` is enough.

This will change as the contributor base grows; the rule of thumb is "two reviewers for code paths a future you might not remember writing." Right now that doesn't exist yet.

---

## Where to go next

- [Running tests](running-tests.md) — what the four-command local gate actually covers.
- [Release process](release-process.md) — what happens after a series of merged PRs adds up to a release.
- [`DOCS_AUTHORING_PLAN.md`](https://github.com/GraysonBellamy/capa/blob/main/DOCS_AUTHORING_PLAN.md) — the staged plan for the docs themselves; relevant when your PR adds new pages.
