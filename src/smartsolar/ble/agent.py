# No ``from __future__ import annotations`` here: dbus-fast reads the annotations as D-Bus signatures.
"""A BlueZ pairing agent that answers the passkey request with a fixed passkey."""

import logging

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/org/trackit/smartsolar/agent"


class PasskeyAgent(ServiceInterface):
    def __init__(self, passkey: int) -> None:
        super().__init__("org.bluez.Agent1")
        self._passkey = passkey

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        log.debug("pin code requested by BlueZ for %s", device)
        return f"{self._passkey:06d}"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        log.debug("passkey requested by BlueZ for %s", device)
        return self._passkey

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        pass

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        pass

    @method()
    def Cancel(self):
        pass


class registered_agent:
    """``async with registered_agent(passkey):`` makes BlueZ use our agent for pairing requests."""

    def __init__(self, passkey: int) -> None:
        self._agent = PasskeyAgent(passkey)
        self._bus = None
        self._manager = None

    async def __aenter__(self):
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus.export(AGENT_PATH, self._agent)
        introspection = await self._bus.introspect("org.bluez", "/org/bluez")
        self._manager = self._bus.get_proxy_object("org.bluez", "/org/bluez", introspection).get_interface("org.bluez.AgentManager1")
        await self._manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
        await self._manager.call_request_default_agent(AGENT_PATH)
        log.debug("pairing agent registered")
        return self

    async def __aexit__(self, *exc):
        try:
            if self._manager is not None:
                await self._manager.call_unregister_agent(AGENT_PATH)
        except Exception as error:
            log.debug("unregistering the agent: %r", error)
        finally:
            if self._bus is not None:
                self._bus.disconnect()
