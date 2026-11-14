"""Local paired-device transport. Never pairs, trusts or disconnects HID devices."""

import asyncio
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .protocol import CTL, GET_CAPS, RX, SERVICE, TX, capabilities

DEVICE = "org.bluez.Device1"
CHAR = "org.bluez.GattCharacteristic1"


@dataclass(frozen=True)
class Remote:
    address: str
    name: str
    path: str
    connected: bool
    compatible: bool | None


def paired_devices(objects: dict) -> list[Remote]:
    result = []
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE, {})

        def get(key, default=None, props=props):
            val = props.get(key)
            return val.value if val is not None else default

        if not get("Paired", False):
            continue
        address = get("Address", "").upper()
        if not address:
            continue
        uuids = [v.lower() for v in get("UUIDs", [])]
        result.append(
            Remote(
                address,
                get("Name", address),  # the address is data; no English fallback text
                path,
                get("Connected", False),
                True if SERVICE in uuids else None,
            )
        )
    return sorted(result, key=lambda x: (x.compatible is not True, x.name, x.path))


def select_remote(objects: dict, address: str) -> Remote:
    found = [r for r in paired_devices(objects) if r.address == address.upper()]
    if not found:
        raise ValueError("not_paired")
    if len(found) > 1:
        raise ValueError("multiple_adapters")
    return found[0]


def choose_write(flags: list[str]) -> str:
    if "write" in flags:
        return "request"
    if "write-without-response" in flags:
        return "command"
    raise ValueError("unsupported_write")


class BlueZTransport:
    def __init__(self):
        self.bus = None
        self.started = []
        self.matches = []
        self.paths = {}
        self.on_control = None
        self.on_audio = None
        self.lost = asyncio.Event()
        self.caps_future = None
        self.remote = None

    async def connect_bus(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def call(
        self, *, path, interface, member, signature="", body=None, destination="org.bluez"
    ):
        if self.bus is None:
            raise ConnectionError("bluetooth_unavailable")
        reply = await asyncio.wait_for(
            self.bus.call(
                Message(
                    destination=destination,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                )
            ),
            5,
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            # Do not include D-Bus bodies/device identifiers in logs or diagnostics.
            raise ConnectionError("bluetooth_operation_failed")
        return reply.body

    async def objects(self):
        return (
            await self.call(
                path="/", interface="org.freedesktop.DBus.ObjectManager", member="GetManagedObjects"
            )
        )[0]

    async def list_paired(self):
        try:
            await self.connect_bus()
            return paired_devices(await self.objects())
        finally:
            await self.close()

    async def open(self, address, on_control, on_audio):
        try:
            await self.connect_bus()
            objects = await self.objects()
            self.remote = select_remote(objects, address)
            if not self.remote.connected:
                raise ConnectionError("wake_remote")
            for path, interfaces in objects.items():
                props = interfaces.get(CHAR)
                if not props or not path.startswith(self.remote.path + "/"):
                    continue
                uuid = props["UUID"].value.lower()
                if uuid in (TX, RX, CTL):
                    # Match the owning ATVV service, not just a characteristic UUID.
                    parent = objects.get(props["Service"].value, {}).get(
                        "org.bluez.GattService1", {}
                    )
                    if parent.get("UUID") and parent["UUID"].value.lower() == SERVICE:
                        self.paths[uuid] = path
                        if uuid == TX:
                            self.write_mode = choose_write(
                                props.get("Flags", Variant("as", [])).value
                            )
            if len(self.paths) != 3:
                raise ValueError("voice_service_unavailable")
            self.on_control, self.on_audio = on_control, on_audio
            self.caps_future = asyncio.get_running_loop().create_future()
            self.bus.add_message_handler(self.receive)
            match = f"type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path_namespace='{self.remote.path}'"
            await self.call(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[match],
            )
            self.matches.append(match)
            for uuid in (CTL, RX):
                await self.call(path=self.paths[uuid], interface=CHAR, member="StartNotify")
                self.started.append(self.paths[uuid])
            await self.write(GET_CAPS)
            data = await asyncio.wait_for(self.caps_future, 5)
            return capabilities(data)
        except BaseException:
            await self.close()
            raise

    async def write(self, data):
        await self.call(
            path=self.paths[TX],
            interface=CHAR,
            member="WriteValue",
            signature="aya{sv}",
            body=[data, {"type": Variant("s", self.write_mode)}],
        )

    def receive(self, message):
        if (
            message.message_type != MessageType.SIGNAL
            or message.member != "PropertiesChanged"
            or len(message.body) < 2
        ):
            return
        interface, props = message.body[:2]
        if self.remote and message.path == self.remote.path and interface == DEVICE:
            if "Connected" in props and not props["Connected"].value:
                self.lost.set()
            return
        if interface != CHAR or "Value" not in props:
            return
        data = bytes(props["Value"].value)
        if message.path == self.paths.get(CTL):
            if data[:1] == b"\x0b" and self.caps_future and not self.caps_future.done():
                self.caps_future.set_result(data)
            elif self.on_control:
                self.on_control(data)
        elif message.path == self.paths.get(RX) and self.on_audio:
            self.on_audio(data)

    async def check_connected(self):
        objects = await self.objects()
        remote = select_remote(objects, self.remote.address)
        return (
            remote.connected
            and remote.path == self.remote.path
            and all(path in objects for path in self.paths.values())
        )

    async def close(self):
        if self.bus is None:
            return
        # Stop only sessions started on this private bus connection. Dropping the
        # connection also releases BlueZ ownership if a bounded StopNotify fails.
        for path in reversed(self.started):
            try:
                await self.call(path=path, interface=CHAR, member="StopNotify")
            except Exception, asyncio.CancelledError:
                pass
        self.started.clear()
        for match in self.matches:
            try:
                await self.call(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="RemoveMatch",
                    signature="s",
                    body=[match],
                )
            except Exception, asyncio.CancelledError:
                pass
        self.matches.clear()
        self.bus.remove_message_handler(self.receive)
        self.bus.disconnect()
        self.bus = None
        self.on_control = self.on_audio = None
