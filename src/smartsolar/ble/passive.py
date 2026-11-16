"""Passive scanning with BlueZ that reports every advertisement, not only changed ones.

bleak reports an advertisement when BlueZ changes a property of the device (``Device1``), and
BlueZ changes one only when the data differs. In an active scan the signal strength changes
with almost every advertisement; in a passive one BlueZ updates it only on large swings. So a
device whose advertisement never changes (a SmartSolar with Instant Readout off sends
``10 02 | model``) is reported once in the life of bluetoothd, and never to a daemon started
after that.

BlueZ does call ``DeviceFound`` on the advertisement monitor for every matching advertisement
(measured: about 4 per second), but bleak's monitor only logs it. ``install()`` replaces the
monitor class in bleak's BlueZ manager, in this process only, by one whose ``DeviceFound`` runs
bleak's own advertisement callbacks with the device's current properties. The callbacks then
get a ``BLEDevice`` and advertisement data as from any other report.

It uses bleak internals that are the same in 0.22 (Debian) and 3.1: ``manager.AdvertisementMonitor``,
``manager._global_instances``, ``BlueZManager._properties`` and
``BlueZManager._run_advertisement_callbacks``. Without them ``install()`` returns False and the
caller scans actively.
"""

# No `from __future__ import annotations`: dbus-fast reads the D-Bus signature "o" from the annotation.
import asyncio
import logging

log = logging.getLogger(__name__)


class UnexportedMonitor(logging.Filter):
    """Drops dbus-fast's warning about a call to a monitor that is gone.

    To stop a passive scan, bleak removes the monitor from the bus and then unregisters it from
    BlueZ. A ``DeviceFound`` in between fails with ``"org.bluez.AdvertisementMonitor1.DeviceFound"
    with signature "o" could not be found``, at every stop of the scan (before each connection).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("AdvertisementMonitor1" in message and "could not be found" in message)


def install() -> bool:
    """Make passive scans report every advertisement. False if bleak's internals are not as expected."""
    try:
        from bleak.backends.bluezdbus import defs, manager
        from bleak.backends.bluezdbus.advertisement_monitor import AdvertisementMonitor
        from dbus_fast.service import method
    except ImportError as exc:
        log.debug("Cannot patch bleak's advertisement monitor: %s", exc)
        return False
    if getattr(manager.AdvertisementMonitor, "reports_every_advertisement", False):
        return True
    if (
        manager.AdvertisementMonitor is not AdvertisementMonitor
        or not hasattr(getattr(manager, "_global_instances", None), "get")  # a dict (3.x) or WeakKeyDictionary (0.22)
        or not hasattr(manager.BlueZManager, "_run_advertisement_callbacks")
    ):
        log.debug("Cannot patch bleak's advertisement monitor: its BlueZ manager has changed")
        return False

    class Monitor(AdvertisementMonitor):
        reports_every_advertisement = True

        @method()
        def DeviceFound(self, device: "o") -> None:  # noqa: F821, N802 - D-Bus signature and name
            try:
                bluez = manager._global_instances.get(asyncio.get_running_loop())
                properties = None if bluez is None else bluez._properties.get(device, {}).get(defs.DEVICE_INTERFACE)
                if properties is not None:
                    bluez._run_advertisement_callbacks(device, properties)
            except Exception as exc:  # never into dbus-fast's dispatcher
                log.debug("Cannot report the advertisement of %s: %r", device, exc)

    manager.AdvertisementMonitor = Monitor
    logging.getLogger("dbus_fast.message_bus").addFilter(UnexportedMonitor())
    return True
