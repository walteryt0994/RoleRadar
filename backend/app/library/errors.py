"""Errors raised by the experience library.

The web layer turns each of these into a Chinese page. None of them carries
the user's content, so they are safe to log.
"""


class LibraryError(Exception):
    """Base class for expected library failures."""


class ConfigError(LibraryError):
    """The library cannot start with the given configuration."""


class UnsafeStoragePath(ConfigError):
    """The configured data path could reach legacy or repository files."""


class NotFound(LibraryError):
    """The object does not exist for this owner, or is no longer accessible."""


class Conflict(LibraryError):
    """The page was based on an older revision than the one now stored.

    current is a plain snapshot of the stored item (see service.ItemSnapshot).
    """

    def __init__(self, current):
        super().__init__("revision conflict")
        self.current = current


class VersionConflict(LibraryError):
    """A history-version page was prepared for an earlier delete/restore state."""

    def __init__(self, item_id: str):
        super().__init__("version state conflict")
        self.item_id = item_id


class NotAllowed(LibraryError):
    """The action is not allowed in the object's current state."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StorageError(LibraryError):
    """A database operation failed; its transaction was rolled back."""


class ContentError(LibraryError):
    """Submitted content failed validation; nothing was saved."""

    def __init__(
        self,
        message: str,
        field_errors: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}
