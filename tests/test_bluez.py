import pytest
from dbus_fast import Variant

from custom_components.bt_remote_voice.bluez import choose_write, paired_devices, select_remote
from custom_components.bt_remote_voice.protocol import SERVICE


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

    from custom_components.bt_remote_voice.bluez import CHAR, BlueZTransport
    from custom_components.bt_remote_voice.protocol import CTL, RX, TX

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


def test_paired_devices_detects_fire_family():
    from custom_components.bt_remote_voice.fire import FIRE_VOICE_SERVICE

    hid = "00001812-0000-1000-8000-00805f9b34fb"
    items = {
        "/org/bluez/hci0/dev_fire": obj("02:00:00:00:00:01", services=[hid, FIRE_VOICE_SERVICE]),
        "/org/bluez/hci0/dev_atvv": obj("02:00:00:00:00:02", services=[SERVICE]),
        "/org/bluez/hci0/dev_kbd": obj("02:00:00:00:00:03", services=[hid]),
    }
    found = {x.address: (x.family, x.compatible) for x in paired_devices(items)}
    assert found == {
        "02:00:00:00:00:01": ("fire", True),
        "02:00:00:00:00:02": ("atvv", True),
        "02:00:00:00:00:03": ("atvv", None),  # a plain HID device is not a voice remote
    }


# --- Fire remote classification (issue #1) -------------------------------------------------

HID = "00001812-0000-1000-8000-00805f9b34fb"
VENDOR = "5de20000-5e8d-11e6-8b77-86f30ca893d3"


def fire_obj(*, services, modalias=None, manufacturer=None, extra=None):
    device = obj("02:00:00:00:00:09", services=services)["org.bluez.Device1"]
    if modalias is not None:
        device["Modalias"] = Variant("s", modalias)
    if manufacturer is not None:
        device["ManufacturerData"] = Variant(
            "a{qv}", {k: Variant("ay", v) for k, v in manufacturer.items()}
        )
    device.update(extra or {})
    return {"/org/bluez/hci0/dev_fire": {"org.bluez.Device1": device}}


def only(items):
    (remote,) = paired_devices(items)
    return remote


def test_fire_by_vendor_uuid_unchanged():
    remote = only(fire_obj(services=[HID, VENDOR]))
    assert (remote.family, remote.compatible, remote.reasons) == ("fire", True, ("vendor_uuid",))


def test_fire_by_modalias_without_d_part():
    remote = only(fire_obj(services=[HID], modalias="usb:v0171p0413"))
    assert (remote.family, remote.compatible) == ("fire", True)
    assert (remote.vendor_id, remote.product_id) == (0x0171, 0x0413)
    assert remote.reasons == ("modalias_0171",)


def test_fire_by_bluetooth_modalias():
    remote = only(fire_obj(services=[HID], modalias="bluetooth:v0171p0421d0001"))
    assert remote.family == "fire"
    assert remote.product_id == 0x0421


def test_fire_by_manufacturer_data_only():
    remote = only(
        fire_obj(services=[HID], manufacturer={0x0171: bytes.fromhex("0414027d00000008")})
    )
    assert (remote.family, remote.reasons) == ("fire", ("manufacturer_0171",))
    assert remote.manufacturer == ((0x0171, "0414027d00000008"),)


def test_hid_without_amazon_signal_is_atvv_unknown():
    remote = only(fire_obj(services=[HID], modalias="usb:v046dpc52b"))
    assert (remote.family, remote.compatible, remote.reasons) == ("atvv", None, ())


def test_atvv_service_wins_over_amazon_signals():
    remote = only(
        fire_obj(
            services=[HID, SERVICE, VENDOR],
            manufacturer={0x0171: b"\x01"},
            modalias="usb:v0171p0413",
        )
    )
    assert (remote.family, remote.compatible) == ("atvv", True)


@pytest.mark.parametrize("modalias", [None, "", "garbage", "usb:vXYZWp0001", "usb:v0171"])
def test_garbage_modalias_and_missing_properties_do_not_raise(modalias):
    remote = only(fire_obj(services=[HID, VENDOR], modalias=modalias))
    assert remote.family == "fire"
    assert remote.product_id is None and remote.vendor_id is None


def test_remote_fields_are_tuples_and_hashable():
    remote = only(
        fire_obj(
            services=[HID, VENDOR],
            modalias="usb:v0171p0413",
            manufacturer={0x0171: b"\x04\x13"},
            extra={"Appearance": Variant("q", 0x0180), "Bonded": Variant("b", True)},
        )
    )
    assert isinstance(remote.uuids, tuple) and isinstance(remote.manufacturer, tuple)
    assert (remote.appearance, remote.bonded) == (0x0180, True)
    assert hash(remote) is not None


@pytest.mark.parametrize(
    "value",
    [
        Variant("s", "text"),
        Variant("a{qv}", {1: Variant("s", "not bytes")}),
        Variant("y", 3),
        Variant("a{sv}", {"x": Variant("ay", b"\x01")}),
    ],
)
def test_malformed_manufacturer_data_never_breaks_the_device_list(value):
    items = fire_obj(services=[HID, VENDOR])
    items["/org/bluez/hci0/dev_fire"]["org.bluez.Device1"]["ManufacturerData"] = value
    remote = only(items)
    assert remote.family == "fire" and remote.manufacturer == ()
