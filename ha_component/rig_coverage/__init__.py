"""Rig-only helper: measures which lines of DeviceSide's `core/` HA's own process runs between two service calls.

`rig_coverage.start` erases any measurement and starts a fresh one; `rig_coverage.stop` stops it and returns
`{absolute path: [executed lines]}`, `{}` when none is running. Coverage starts mid-process, so no restart and no data
file: the sysmon core also sees tasks that began before `start`. Rig only: never ships with DeviceSide.
"""

import os
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

DOMAIN = "rig_coverage"
_RUNNING = f"{DOMAIN}_running"


def _stop(hass: HomeAssistant) -> dict[str, list[int]]:
    cov = hass.data.pop(_RUNNING, None)
    if cov is None:
        return {}
    cov.stop()
    data = cov.get_data()
    return {path: sorted(data.lines(path) or ()) for path in data.measured_files()}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    core = hass.config.path("custom_components", "deviceside", "core")

    def start(call: ServiceCall) -> None:
        _stop(hass)
        os.environ["COVERAGE_CORE"] = "sysmon"
        import coverage  # noqa: PLC0415 - installed from the manifest's requirements

        cov = coverage.Coverage(source=[core], data_file=None)
        cov.start()
        hass.data[_RUNNING] = cov

    async def stop(call: ServiceCall) -> dict[str, Any]:
        return await hass.async_add_executor_job(_stop, hass)

    async def begin(call: ServiceCall) -> None:
        await hass.async_add_executor_job(start, call)

    async_register_admin_service(hass, DOMAIN, "start", begin)
    async_register_admin_service(hass, DOMAIN, "stop", stop, supports_response=SupportsResponse.ONLY)
    return True
