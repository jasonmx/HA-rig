# Refuse a rig run from a checkout no PR owns

Issue #42. A run deploys and records the code of the deviceside checkout it is run from, so it must come from a
`wt/*` worktree with an open PR (the PR's owner is whoever works there). `target.refuse_unowned_checkout` runs
first in `scripts/rig.py`'s `wait_or_run`, beside the moved-files refusal; the message names `pr-draft`.
`HA_RIG_UNOWNED_CHECKOUT=1` opts out (main checkout, detached CI clone). It is an env var, not a flag, so
`--help` and README are untouched. CI only runs pytest, where the autouse fixture no-ops the check, and
`ci_deviceside.sh` is unchanged.

## Coordination

Intent: refuse a rig run whose deviceside checkout is not a wt/* worktree with an open PR, unless opted out.
Touches: ha_rig/target.py, scripts/rig.py, tests/conftest.py, tests/test_target.py, docs/rig.md
