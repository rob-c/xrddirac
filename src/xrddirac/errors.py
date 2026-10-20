"""What xrddirac raises: one family, joined to xrdcore's vocabulary.

Every error is an :class:`xrdcore.errors.XrdError`, so code that catches the
storage clients' errors catches these too. A DIRAC service answers failure
with ``S_ERROR`` - a message, often an ``Errno`` from DIRAC's ``DErrno``
table - and that becomes a :class:`ServiceError` naming the service and the
method, so a message says *where* it failed as well as why.
"""

from __future__ import annotations

from xrdcore.errors import AuthenticationError, ProtocolError, XrdError

__all__ = [
    "ENOAUTH",
    "AuthorizationError",
    "ConfigError",
    "DiracError",
    "NoSuchServiceError",
    "ServiceError",
    "TooLargeError",
    "WireError",
]

#: DIRAC's ``DErrno.ENOAUTH``: the service refused the caller.
ENOAUTH = 1111


class DiracError(XrdError):
    """Anything xrddirac itself refuses or reports."""


class ConfigError(DiracError, ValueError):
    """A profile, a configuration value or a path that cannot be used as written."""


class NoSuchServiceError(ConfigError):
    """The DIRAC configuration names no URL for a service (``DataManagement/FileCatalog``)."""


class TooLargeError(DiracError, ValueError):
    """A query would return more than the caller allowed; narrow it or raise ``limit``."""


class WireError(DiracError, ProtocolError):
    """A peer sent something DISET or DiracX does not allow: a bad frame, an undecodable value."""


class ServiceError(DiracError):
    """A DIRAC service answered ``S_ERROR``.

    ``errno`` is DIRAC's own number (``DErrno``) when the service gave one,
    else 0; it is *not* an OS errno.
    """

    def __init__(
        self, message: str, *, errno: int = 0, service: str = "", method: str = ""
    ) -> None:
        """Keep the parts; the text says ``service.method: message``."""
        self.message = message
        self.errno = errno
        self.service = service
        self.method = method
        where = f"{service}.{method}: " if service and method else ""
        super().__init__(f"{where}{message}")

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        """Pickle by the constructor's parts (dask moves exceptions between processes)."""
        return _rebuild, (type(self), self.message, self.errno, self.service, self.method)


def _rebuild(
    cls: type[ServiceError], message: str, errno: int, service: str, method: str
) -> ServiceError:
    """A :class:`ServiceError` (or subclass) from what :meth:`ServiceError.__reduce__` kept."""
    return cls(message, errno=errno, service=service, method=method)


class AuthorizationError(ServiceError, AuthenticationError):
    """The service refused who we are (``ENOAUTH``, or an unauthorised action)."""
