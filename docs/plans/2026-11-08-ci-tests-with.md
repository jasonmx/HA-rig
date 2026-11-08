# CI: test a rig PR against the deviceside PR it names

## Context

ha-rig #30 (the rig side of deviceside #284) merged at 09:51Z on 2026-11-08. Its CI ran against deviceside's newest
branch head, which did *not* yet include deviceside #287, which merged two minutes later. The paired change was never
tested together, and main's tests broke when they met (see `docs/plans/2026-11-08-pinned-adapter-roster.md` on
`wt/pinned-roster` for that fix).

The fix: a `Tests-with: jasonmx/deviceside#N` line in a rig PR's body makes its pytest job run against that deviceside
PR. Unlike `Depends-on`, it doesn't block the merge, so CLAUDE.md's rule that a rig step file lands "with or before"
its deviceside PR still holds.

## Coordination
Intent: A rig PR whose body has `Tests-with: jasonmx/deviceside#N` runs its pytest job against that deviceside PR,
so a paired change is tested together before either side merges.
Touches: .github/workflows/tests.yml, scripts/ci_deviceside.sh, CLAUDE.md

## Steps

0. This PR's CI stays red until the pinned-roster PR merges. Once that PR is open (`gh pr list --head
   wt/pinned-roster`), add `Depends-on: #<n>` to the Coordination section above and rerun `pr-draft`.
1. **`scripts/ci_deviceside.sh`**: picks and checks out the deviceside ref. It takes the deviceside clone's path and
   the rig PR number (empty when not a PR).
   - The rig PR's body is fetched fresh with `gh pr view <n> -R "$GITHUB_REPOSITORY" --json body`, not taken from
     `github.event`. A re-run otherwise replays the old body.
   - It matches only `^Tests-with:\s*jasonmx/deviceside#([0-9]+)\s*$`. The repo is fixed; only the number comes
     from the body. A `Tests-with:` line that doesn't match is an **error**, not a fallback.
   - With N: `gh pr view N -R jasonmx/deviceside --json state,baseRefName`.
     - **OPEN**: fetch `refs/pull/N/head` (this works for forks too) and check it out.
     - **MERGED**: check out the head of its `baseRefName`.
     - **CLOSED** (not merged): fail, naming the PR.
   - Without N: today's rule, unchanged (the most recently committed of `main` and `release/*`).
   - It always prints what it chose and why (`deviceside at refs/pull/287/head (Tests-with #287, open)`).
     Otherwise a silent fallback looks just like a pass.
2. **`tests.yml`**: replace the inline checkout step with the script.
   - Add `pull_request: types: [opened, synchronize, reopened, edited]` so editing the body reruns CI.
   - Add `pull-requests: read` to the job's permissions, and pass `GH_TOKEN: ${{ github.token }}`.
   - Pass the PR number through `env`, never `${{ }}`-interpolated into the script.
   - Update the comment above the step.
3. **CLAUDE.md**, Changes: extend the rule on new or renamed actions. The rig PR names the deviceside PR with
   `Tests-with: jasonmx/deviceside#N` in its plan's Coordination section, which `pr-draft` copies verbatim into the
   PR body.
4. If `pre-commit` has no shellcheck hook, run `shellcheck` by hand on the script.

## Verification

- Run the script locally against a scratchpad clone of deviceside, with stub bodies for: no line; an open PR (find
  one with `gh pr list -R jasonmx/deviceside`, if any); `#287` (merged, so `main`); a closed-unmerged PR if one
  exists; and a malformed line (error). Each prints the ref it chose, or fails.
- On the PR itself: temporarily add `Tests-with: jasonmx/deviceside#287` to the body. The log must show
  `main (Tests-with #287, merged)`. Then edit it out and confirm the `edited` event reran CI.
- `.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code`, then `ci-green`. Merge with `merge-green <n>
  --squash --delete-branch` once the pinned-roster PR has merged.

## Out of scope

- `secrets-history` failing on one of #31's runs: unrelated; look into it separately.
- Running the rig's tests from deviceside's CI: deviceside is public and ha-rig private; the daily cron here still
  catches drift.
