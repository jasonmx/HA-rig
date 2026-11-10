# Live check of observed lends (follow-on to `2026-11-10-observed-facts.md`, issue #36)

Issue: #36. Branch: `wt/observed-facts-live`, off `main` once #48 and jasonmx/deviceside#329 have merged.

## Coordination

Intent: Prove on the household's hardware that observed lends work as designed, and record the result. Closes #36.
Touches: docs/plans/2026-11-10-observed-facts-live-check.md, docs/rig.md, tests/
Depends-on: #48

## Context

#48 landed observed lends: `_observed.md` rows, the lend in the observed phase, `--reobserve`, call hashes with numbers
reduced to their sign. Its unit tests use fakes. Nothing has yet shown the lend on real devices, and the spike
(`2026-11-10-observed-facts.md`, Spike results) found no observed test whose twin calls begin another's, so Live 2 needs
a pair built for it. Live 1 is attended, so this waits for Jason at the rig.

## Steps

Through `scripts/rig.py`, behind its authorization gate (never `DEVICE_SIDE_RIG_RESTRICTED=1` without Jason's yes for
the run). The rig moves real lights and restarts HA.

1. **Attended**, on the suite with the longer sequence: answer the observed question; rows are written to
   `_observed.md`.
2. **Build a prefix pair** if none exists: an observed test whose automated twin's calls are a prefix of step 1's
   (for example a test that raises, where step 1's raises then stops), in the same starting state and slots. Name what
   it catches that no existing case does (CLAUDE.md, Changes).
3. `--unattended` on the suite with the prefix test: its observed case is lent, `pass (observed <date>)`, and the record
   row has the footnote.
4. On a scratch branch of deviceside, change the adapter's call (a field's sign, not its value), then `--unattended`:
   `not observed`, no lend. Discard the branch.
5. Note the results in this plan, run `.venv/bin/pytest` and `pre-commit run --all-files`, `/audit-code` if code
   changed, mark ready and merge. This closes #36.

## Model

opus: it runs against real hardware and a wrong read of a lend would pass unseen.

## Handoff

Not started. Waiting on #48 and jasonmx/deviceside#329 merging, and on Jason being at the rig.
