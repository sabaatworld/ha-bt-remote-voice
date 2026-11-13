import pytest
from dbus_fast import Variant

from custom_components.atvv_remote.bluez import choose_write, paired_devices, select_remote
from custom_components.atvv_remote.protocol import SERVICE


def obj(address, *, paired=True, services=None):
    return {
        "org.bluez.Device1": {
            "Address": Variant("s", address),
            "Name": Variant("s", "Test Remote"),
            "Paired": Variant("b", paired),
            "Connected": Variant("b", False),
            "UUIDs": Variant("as", services or []),
        }
    }


def test_picker_excludes_unpaired_and_preserves_asleep_candidates():
    items = {
        "/org/bluez/hci1/dev_test": obj("02:00:00:00:00:01", services=[SERVICE]),
        "/org/bluez/hci0/dev_other": obj("02:00:00:00:00:02"),
        "/org/bluez/hci0/dev_bad": obj("02:00:00:00:00:03", paired=False),
    }
    remotes = paired_devices(items)
    assert [(x.address, x.compatible) for x in remotes] == [
        ("02:00:00:00:00:01", True),
        ("02:00:00:00:00:02", None),
    ]
    assert select_remote(items, "02:00:00:00:00:01").path == "/org/bluez/hci1/dev_test"
    with pytest.raises(ValueError, match="not_paired"):
        select_remote(items, "02:00:00:00:00:03")


def test_write_mode_uses_supported_flags():
    assert choose_write(["write-without-response"]) == "command"
    assert choose_write(["write"]) == "request"
    with pytest.raises(ValueError):
        choose_write(["notify"])


def test_duplicate_bonds_require_disambiguation_instead_of_arbitrary_adapter():
    items = {
        "/org/bluez/hci0/dev_test": obj("02:00:00:00:00:01"),
        "/org/bluez/hci1/dev_test": obj("02:00:00:00:00:01"),
    }
    with pytest.raises(ValueError, match="multiple_adapters"):
        select_remote(items, "02:00:00:00:00:01")


async def test_partial_subscription_failure_releases_only_owned_session():

    from custom_components.atvv_remote.bluez import CHAR, BlueZTransport
    from custom_components.atvv_remote.protocol import CTL, RX, TX

    class Bus:
        def add_message_handler(self, handler):
            pass

        def remove_message_handler(self, handler):
            pass

        def disconnect(self):
            self.disconnected = True

    transport = BlueZTransport()
    bus = Bus()
    calls = []
    dev = "/org/bluez/hci1/dev_test"
    service = dev + "/service01"
    objects = {
        dev: obj("02:00:00:00:00:01", services=[SERVICE]),
        service: {"org.bluez.GattService1": {"UUID": Variant("s", SERVICE)}},
    }
    objects[dev]["org.bluez.Device1"]["Connected"] = Variant("b", True)
    for index, uuid in enumerate((TX, CTL, RX)):
        objects[service + f"/char{index}"] = {
            CHAR: {
                "UUID": Variant("s", uuid),
                "Service": Variant("o", service),
                "Flags": Variant("as", ["write-without-response"] if uuid == TX else ["notify"]),
            }
        }

    async def connect():
        transport.bus = bus

    async def call(**kw):
        calls.append(kw)
        if kw["member"] == "StartNotify" and kw["path"].endswith("char2"):
            raise ConnectionError("failed")
        return []

    from unittest.mock import AsyncMock, patch

    with (
        patch.object(transport, "connect_bus", connect),
        patch.object(transport, "objects", AsyncMock(return_value=objects)),
        patch.object(transport, "call", call),
    ):
        with pytest.raises(ConnectionError):
            await transport.open("02:00:00:00:00:01", None, None)
    assert [x["path"] for x in calls if x["member"] == "StopNotify"] == [service + "/char1"]
    assert bus.disconnected
