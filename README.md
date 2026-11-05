# ha-rig

The automated test rig for [Device Side](https://github.com/jasonmx/deviceside): it runs Device Side's actions
against a live Home Assistant and its real devices, and writes the test records in device-side's
`docs/test-records/` that clear an adapter's `experimental` flag and gate each release. Private; device-side keeps
only the records.

Setup, running it, and writing step files: [docs/rig.md](docs/rig.md).

```sh
uv sync && .venv/bin/pytest          # the rig's own tests, against a deviceside checkout beside this one
cd ../deviceside && ../ha-rig/scripts/rig.py --dry-run
```
