# ha-rig

The automated test rig for [DeviceSide](https://github.com/jasonmx/deviceside): it runs DeviceSide's actions
against a live Home Assistant and its real devices, and writes the test records in deviceside's
`docs/test-records/` that clear an adapter's `experimental` flag and gate each release. Private; deviceside keeps
only the records.

Setup, running it, and writing step files: [docs/rig.md](docs/rig.md).

```sh
uv sync && .venv/bin/pytest          # the rig's own tests, against a deviceside checkout beside this one
cd ../deviceside && ../ha-rig/scripts/rig.py --dry-run
```
