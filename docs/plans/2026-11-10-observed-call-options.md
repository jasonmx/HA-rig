# Observed calls carry the wire trace's `options`

## Context

jasonmx/deviceside#332 adds an `options` key to `deviceside_wire` events: a transport setting that changes how the call
is encoded (Z2M: `{"hue_native_control": true|false|null}`, stamped on every Z2M set and turn_on_for's `ha`
`light.turn_on`; other transports send no key). The rig reads a call as `(transport, command, params)` only
(`engine.trace`, `observed.py`), so it drops `options`: two runs with the option in different states hash alike, and an
`_observed.md` pass could be lent across calls that differ on the air. `client.wire_tap` already passes the event
through whole.

Decided with Jason (2026-11-10):

- **Hash only when present.** A call is `(transport, command, params, options)`; `options` is hashed only when
  non-empty, so Hue, Shelly, ZHA and Matter hashes are unchanged. Every existing Z2M entry (all pre-stamp) hashes
  differently from now on, so it stops covering a run, which is the wanted behavior: old entries lack `options`.
- **An unknown stamp never lends or covers.** An `options` value of `null` hashes as its own value but the case is
  never lent an observed pass and never recorded as one: an unknown option can't prove a call on the air.

## Coordination

Intent: The rig keeps a wire event's `options` in each call, so observed passes match only calls that are the same on
the air, and a call with an unknown option is neither lent nor recorded.
Touches: ha_rig/engine.py, ha_rig/observed.py, ha_rig/coverage.py, docs/rig.md, tests/
Tests-with: jasonmx/deviceside#332
Depends-on: jasonmx/deviceside#332

## Steps

0. In this worktree, `pr-draft <this plan>` once the first commit exists. #332 must be merged (or the rig's CI
   checkout of it used, per `scripts/ci_deviceside.sh`) before the live check.
1. **`ha_rig/observed.py`**:
   - `Call` becomes `(transport, command, params, options)` with `options: dict` (empty when the event had none).
   - `normalize`: signs `params` as now; `options` passes through exact (booleans and `null` survive `_sign`).
   - `call_hash`: hashes `[transport, command, signed params]` when `options` is empty (today's hash, unchanged),
     else that list plus `options`.
   - `render_call`: appends ` {options}` canonically when non-empty.
   - Legend: a fifth `Options` column in `_observed.md` (`LEGEND_HEADER`, `read`, `render`), written as
     `` `{...}` `` or empty. `read` accepts the old four-cell rows (options empty). `Trace.to_json` writes the call as
     a 4-list; `from_json` accepts 3-lists from an earlier ledger.
   - `Trace` gains `unknown: bool` (true when any call's options hold a `null`); saved and loaded with the ledger
     (absent = false). `Trace.rows` returns `[]` and `lend` returns None when `unknown`, so such a case is neither
     recorded nor lent. Name where the check lives after reading `lend` and `observed_hints`.
2. **`ha_rig/engine.py`** (`trace`, ~line 1534): build each call as `(transport, command, params, call.get("options")
   or {})`. Nothing else in the engine changes.
3. **`ha_rig/coverage.py`** (`observed_hints`): an `unknown` trace gets no hint (it follows from step 1's `lend`; add
   a branch only if `observed_hints` doesn't call it).
4. **`docs/rig.md`** (the wire trace paragraph, ~line 333): a call is `(transport, command, params, options)`; options
   are hashed only when present; a `null` option means the case is never lent or recorded, and the run output says
   so (where the run already explains an unlent observed case, add the reason; don't add a new output path).
5. **Tests** (rig's own, with fakes): `normalize`/`call_hash` (options absent hashes as before, pinned by a literal
   hash; `true` and `false` differ; `null` differs from both); `render`/`read` round trip with and without the column,
   and an old four-cell file reads; `Trace` JSON round trip including an old 3-list ledger; `lend` skips an old entry
   against a run with options, and an `unknown` trace; `engine.trace` carries `options` from a fake wire event and
   sets `unknown` on `null`.
6. **Live check** after #332 merges: a `lab_z2m` run of one observed twin with the wire logger on (rig's
   CLAUDE.local.md for run and lock rules; check the Hue / Shelly fixture swap first). The written `_observed.md`
   legend shows an Options column with `hue_native_control`, and an existing Z2M entry in the deviceside checkout is
   not lent. Don't commit the checkout's `_observed.md` change from the run; that is a person's record.

## Verification

- `.venv/bin/pytest`, `pre-commit run --all-files`; README's `--help` block is unchanged (no option changes).
- `/audit-code` on the diff before the PR (this repo's rule), then `merge-green <n> --squash --delete-branch`.
- After merge, deviceside's `_observed.md` Z2M entries no longer cover runs: tell Jason, since a full-suite pass
  before an RC then needs those observed cases re-seen.
