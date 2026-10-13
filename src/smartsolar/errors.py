from __future__ import annotations


class SmartsolarError(Exception):
    """Base class for all errors raised by smartsolar."""


class ConfigError(SmartsolarError):
    pass


class NotConnectedError(SmartsolarError):
    """No GATT link is open, or it dropped during an operation."""


class LinkError(SmartsolarError):
    """A BLE write or notification setup failed, but the link may still be up."""


class ConnectError(SmartsolarError):
    """Opening the BLE connection failed."""

    def __init__(self, message: str, *, in_progress: bool = False, not_found: bool = False) -> None:
        super().__init__(message)
        self.in_progress = in_progress
        self.not_found = not_found


class PairingError(SmartsolarError):
    """Pairing with the device failed (wrong passkey, no answer, rejected)."""


class ProtocolError(SmartsolarError):
    """The device sent something the GATT protocol parser cannot use, or did not answer in time."""


class InvalidWriteError(SmartsolarError):
    """A write request failed validation before anything was sent."""


class WriteFailed(SmartsolarError):
    """The device did not accept a write, or does not report the written value."""


class AdvertError(SmartsolarError):
    """An Instant Readout advertisement cannot be decoded."""


class KeyMismatch(AdvertError):
    """The key's check byte differs from the one in the advertisement: wrong or outdated key."""


class NoDataError(SmartsolarError):
    """There is no (recent) data of the requested kind."""


class BadRequest(SmartsolarError):
    """A request to the daemon is malformed or its arguments are invalid."""


class DaemonUnreachable(SmartsolarError):
    """The CLI could not reach the daemon's control socket."""


class DaemonError(SmartsolarError):
    """The daemon answered a request with an error."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.type = error_type
        self.message = message
