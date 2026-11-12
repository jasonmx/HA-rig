"""The rig's HueBridge against a local stub of the bridge's CLIP v2 API."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ha_rig.bridge import BridgeError, HueBridge


@pytest.fixture
async def stub(socket_enabled):
    """`start(reply, status)`: a HueBridge on a local stub that answers every request so and records each as
    (method, path, app key, body); the bridges and stubs are closed after the test."""
    opened: list[tuple[HueBridge, TestServer]] = []

    async def start(reply: dict | None = None, status: int = 200) -> tuple[HueBridge, TestServer, list]:
        received: list = []

        async def handle(request: web.Request) -> web.Response:
            body = await request.json() if request.can_read_body else None
            received.append((request.method, request.path, request.headers.get("hue-application-key"), body))
            return web.json_response(
                reply if reply is not None else {"data": [{"id": "x"}], "errors": []}, status=status
            )

        app = web.Application()
        app.router.add_route("*", "/clip/v2/resource/{tail:.*}", handle)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        bridge = HueBridge(f"{server.host}:{server.port}", "key")
        bridge.base = f"http://{server.host}:{server.port}/clip/v2/resource"
        opened.append((bridge, server))
        return bridge, server, received

    yield start
    for bridge, server in opened:
        await bridge.close()
        await server.close()


async def test_put_sends_the_body_to_the_resource_with_the_app_key(stub):
    bridge, _, received = await stub()
    await bridge.put("light", "abc", {"on": {"on": True}})
    assert received == [("PUT", "/clip/v2/resource/light/abc", "key", {"on": {"on": True}})]


@pytest.mark.parametrize(
    ("status", "reply", "message"),
    [
        (200, {"errors": [{"description": "invalid body"}]}, "got 200: invalid body"),
        (403, {"errors": [{"description": "unauthorized user"}]}, "got 403: unauthorized user"),
        (500, {}, "got 500"),
    ],
)
async def test_put_raises_on_an_http_error_or_a_listed_error(stub, status, reply, message):
    bridge, _, _ = await stub(reply, status)
    with pytest.raises(BridgeError, match=message):
        await bridge.put("light", "abc", {})


async def test_put_raises_when_the_bridge_cannot_be_reached(stub):
    bridge, server, _ = await stub()
    await server.close()
    with pytest.raises(BridgeError, match="PUT light/abc"):
        await bridge.put("light", "abc", {})


async def test_get_post_and_delete_use_the_resource_path_and_return_the_data(stub):
    bridge, _, received = await stub()
    assert await bridge.get("device") == [{"id": "x"}]
    assert await bridge.get("device", "abc") == [{"id": "x"}]
    assert await bridge.post("zigbee_device_discovery", {"action": {"action_type": "search"}}) == [{"id": "x"}]
    await bridge.delete("device", "abc")
    assert received == [
        ("GET", "/clip/v2/resource/device", "key", None),
        ("GET", "/clip/v2/resource/device/abc", "key", None),
        ("POST", "/clip/v2/resource/zigbee_device_discovery", "key", {"action": {"action_type": "search"}}),
        ("DELETE", "/clip/v2/resource/device/abc", "key", None),
    ]


async def test_delete_raises_on_a_listed_error(stub):
    bridge, _, _ = await stub({"errors": [{"description": "not found"}]})
    with pytest.raises(BridgeError, match="DELETE device/abc got 200: not found"):
        await bridge.delete("device", "abc")


@pytest.mark.parametrize(
    ("spelling", "found"),
    [
        ("0x001788010abcdef0", "dev-1"),
        ("00:17:88:01:0A:BC:DE:F0", "dev-1"),
        ("0x0000000000000000", None),
    ],
)
async def test_device_id_by_ieee_reads_zigbee_connectivity_in_any_spelling(stub, spelling, found):
    bridge, _, _ = await stub(
        {"data": [{"mac_address": "00:17:88:01:0a:bc:de:f0", "owner": {"rid": "dev-1", "rtype": "device"}}]}
    )
    assert await bridge.device_id_by_ieee(spelling) == found
