# ha-rig

The automated test rig for [DeviceSide](https://github.com/DeviceSide/DeviceSide): it runs DeviceSide's actions
against a live Home Assistant and its real devices, and writes the test records in deviceside (each
integration's `docs/integrations/<integration>/test-records/`) that sign off an integration's changes for a
release. Private; deviceside keeps only the records.

Setup, running it, and writing step files: [docs/rig.md](docs/rig.md).

```sh
uv sync && .venv/bin/pytest          # the rig's own tests, against a deviceside checkout beside this one
cd ../deviceside && ../ha-rig/scripts/rig.py --dry-run
```

## Options

`./scripts/rig.py --help`, at 100 columns (`tests/test_rig.py` fails when this block drifts from it):

<!-- rig-help -->
```text
usage: rig.py [-h] [--profile PROFILE] [--dry-run] [--check] [--no-deploy]
              [--integration {hue,matter,shelly,zha,zigbee2mqtt}] [--action ACTION] [-k KEYWORD]
              [--observed] [--facts] [--unattended] [--serial] [--debug-logs] [--deploy-only]
              [--for DURATION] [--fresh] [--ignore-unreachables] [--recover] [--promote DIR]
              [--roam-check] [--wait-free] [--hold INTEGRATION] [--exclusive]
              [--visual {none,profile}]

Run the rig tests against the live HA, behind an authorization gate.

options:
  -h, --help            show this help message and exit
  --profile PROFILE     the profile to run (profiles/NAME.yaml); needed if there are two
  --dry-run             print the plan and stop before the gate
  --check               check the run's devices are in HA, on their integration and available, and
                        where its roaming bulbs are, then stop: no lock, no deploy, nothing
                        changed
  --no-deploy           test whatever HA already runs
  --integration {hue,matter,shelly,zha,zigbee2mqtt}
                        limit to an integration key (repeatable); with --wait-free, wait for it
  --action ACTION       limit to an action's step files (repeatable)
  -k KEYWORD            run only test cases whose id contains this text
  --observed            also run the observed tests, which ask the person at the rig what they
                        saw; a default run skips them
  --facts               also run the firmware facts, which prove how a device behaves; a default
                        run skips them: a pass stands
  --unattended          ask no one: with --observed, the observed tests are reported not observed
  --serial              run every case one at a time, with no lanes: to tell a case that fails in
                        a lane from one that fails
  --debug-logs          run HA's DeviceSide and the run's integrations, and Z2M, at debug, and
                        keep the run's captured logs (they are kept after a failure whatever this
                        says)
  --deploy-only         deploy this checkout's branch and restart HA, then release the locks: no
                        profile, no tests; unrestricted
  --for DURATION        start no case after this long (30m, 90s, 1h15m), let in-flight ones
                        finish, put back and exit 75; rerun the same way to resume
  --fresh               rerun the selected suites whole, dropping the cases an earlier run at this
                        commit saved
  --ignore-unreachables
                        run what the devices that are up allow: skip the cases of an unavailable
                        fixed entity, and while any is, of fixed groups whose members the profile
                        doesn't list and of fixed scenes; their suites write no record
  --recover             put back what an aborted run left changed, then stop
  --promote DIR         copy the test records a run staged in DIR (it prints the command) into the
                        checkout's records and take their suites out of the ledger; no lock, no HA
  --roam-check          move the profile's roam bulbs to every network and home, no suites
  --wait-free           wait until no rig run holds the locks (with --integration, those
                        integrations' and HA exclusively), without taking them; another run may
                        take them first
  --hold INTEGRATION    hold this integration's lock (repeatable) and HA shared while running the
                        command after --, for a probe
  --exclusive           with a command after --: hold HA exclusively, for a probe that restarts HA
                        or reloads DeviceSide
  --visual {none,profile}
                        with a command after --: declare what the probe changes that anyone could
                        see, making it unrestricted: none, nothing; profile, only what the pre-
                        authorized profile covers. Left out, the probe is restricted
```
<!-- /rig-help -->
