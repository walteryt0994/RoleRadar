"""The private folder that keeps attachment files.

The configuration names this folder explicitly (config.check_files_path),
and it belongs to exactly one library database: a marker file in it holds
that database's id. A folder with other content, or with another database's
marker, is refused at start-up (claim). Inside it:

- files/<owner id>/<storage key>: the kept files;
- staging/<storage key>.part: an upload while it is received and stored.
  It is named with the key its kept file gets, so an attachment's record
  names both of its files, and removing them (remove_file) needs nothing
  else, also after a restart;
- exports/<random>.zip.part: made for an "export all" package, and nameless
  again before a byte of the package is written to it;
- imports/<owner id>/<storage key>: the files of resume imports (U2-B1),
  apart from the attachments: they live for at most seven days and the
  clean-up looks at them with their own records (sweep's imported).

Every path is made here from an owner's UUID and a random 32-character hex
key, never from a name a request sent; the original file name only lives in
the database. New files are created with O_EXCL (nothing is ever
overwritten), O_NOFOLLOW and permission 0600, and a received file gets its
kept name through a hard link, which fails rather than replace a file that
is already there. A folder is checked to be a real folder at its own place
inside the root before it is used, so a symbolic link put into the store
cannot lead a read, write or removal outside it. Removing a file that is
already gone counts as done, so an interrupted clean-up can simply run again.
"""
import hashlib
import logging
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from app.library.errors import ChangedFile, ConfigError, FileStoreError, MissingFile, UnreadableFile

MARKER_NAME = "roleradar-library-id"
OWNER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STORAGE_KEY = re.compile(r"[0-9a-f]{32}")
STAGING_NAME = re.compile(r"[0-9a-f]{32}\.part")
EXPORT_NAME = re.compile(r"[0-9a-f]{32}\.zip\.part")
# The two areas that keep files under owner folders.
FILES, IMPORTS = "files", "imports"
CHUNK_BYTES = 64 * 1024
# Bytes kept from the start of a received file: enough for every type check.
HEAD_BYTES = 16
# A staging or export file nobody touched for this long, or a kept file no
# record refers to, belongs to no running request (seconds of real time).
LEFTOVER_AFTER = 3600.0
NEW_FILE = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

logger = logging.getLogger("roleradar.library")


class StagingFile:
    """One upload while it is received: its size, checksum and first bytes.

    key is the storage key that the upload's record and kept file get.
    """

    def __init__(self, store: "FileStore", key: str, path: Path, handle: BinaryIO):
        self._store = store
        self._handle = handle
        self._digest = hashlib.sha256()
        self.key = key
        self.path = path
        self.size = 0
        self.head = b""

    def write(self, data: bytes) -> None:
        if len(self.head) < HEAD_BYTES:
            self.head += data[: HEAD_BYTES - len(self.head)]

        self._handle.write(data)
        self._digest.update(data)
        self.size += len(data)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def finish(self) -> None:
        """Make the received bytes durable and stop writing."""
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()

    def discard(self) -> bool:
        """Remove the staging name; True once it is gone. A kept name made from it by place() stays."""
        if not self._handle.closed:
            self._handle.close()

        try:
            self._store.check_folder(self._store.staging_dir)
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except (FileStoreError, OSError) as error:
            # Not lost: removing the attachment's file removes this name too
            # (it comes from the same key), and so does the clean-up pass
            # once it is old.
            logger.warning("could not remove a staging file (%s)", type(error).__name__)
            return False
        finally:
            self._store.release(self.path)

        return True


@dataclass
class SweepReport:
    staging: int = 0
    exports: int = 0
    kept: int = 0
    failed: int = 0


class FileStore:
    def __init__(self, root: Path):
        self.root = Path(os.path.realpath(root))
        self.files_dir = self.root / "files"
        self.staging_dir = self.root / "staging"
        self.exports_dir = self.root / "exports"
        self.imports_dir = self.root / IMPORTS
        self._in_use: set[Path] = set()
        self._lock = threading.Lock()

    # --- start-up ---------------------------------------------------------

    def claimed_by(self) -> str | None:
        """The library id in the folder's marker; None while the folder is new or empty.

        A folder that holds anything without the marker is refused: only a
        folder made for the library is ever used, or swept.
        """
        try:
            info = os.lstat(self.root)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ConfigError("无法读取附件目录。") from error

        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError("附件目录路径指向的不是一个目录；请指定一个新的空目录。")

        names = os.listdir(self.root)

        if not names:
            return None

        if MARKER_NAME not in names:
            raise ConfigError("附件目录里已有其他内容，不是经历库的附件目录；请指定一个新的空目录。")

        return self._read_marker()

    def claim(self, library_id: str) -> None:
        """Make the folder this library's (or check that it is), with its four subfolders."""
        claimed = self.claimed_by()

        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.check_folder(self.root)

            if claimed is None:
                try:
                    fd = os.open(self.root / MARKER_NAME, os.O_WRONLY | NEW_FILE, 0o600)
                except FileExistsError:
                    # Another start-up wrote it a moment ago.
                    claimed = self._read_marker()
                else:
                    with os.fdopen(fd, "w", encoding="ascii") as handle:
                        handle.write(f"{library_id}\n")

            if claimed is not None and claimed != library_id:
                raise ConfigError("这个附件目录属于另一个经历库数据库；请为这个数据库指定它自己的附件目录。")

            for folder in (self.files_dir, self.staging_dir, self.exports_dir, self.imports_dir):
                try:
                    os.mkdir(folder, 0o700)
                except FileExistsError:
                    pass

                self.check_folder(folder)
        except (OSError, FileStoreError) as error:
            raise ConfigError("无法准备附件目录；请检查路径和权限，或指定一个新的空目录。") from error

    def _read_marker(self) -> str:
        try:
            fd = os.open(self.root / MARKER_NAME, READ_FILE)

            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError("not a regular file")

                value = handle.read(100).decode("ascii").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError("附件目录的标记文件无法读取；请指定一个新的空目录。") from error

        if not OWNER_ID.fullmatch(value):
            raise ConfigError("附件目录的标记文件已损坏；请指定一个新的空目录。")

        return value

    # --- folders ----------------------------------------------------------

    def check_folder(self, path: Path) -> None:
        """path must be a real folder (not a link), at its own place on disk."""
        try:
            info = os.lstat(path)
        except FileNotFoundError as error:
            raise MissingFile() from error
        except OSError as error:
            raise UnreadableFile() from error

        if not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != str(path):
            raise UnreadableFile()

    def _area(self, area: str) -> Path:
        if area == FILES:
            return self.files_dir

        if area == IMPORTS:
            return self.imports_dir

        raise ValueError("not an area of the store")

    def _kept_path(self, owner_id: str, key: str, *, create: bool = False, area: str = FILES) -> Path:
        if not OWNER_ID.fullmatch(owner_id) or not STORAGE_KEY.fullmatch(key):
            raise ValueError("not an owner id and storage key")

        base = self._area(area)
        self.check_folder(base)
        folder = base / owner_id

        if create:
            try:
                os.mkdir(folder, 0o700)
            except FileExistsError:
                pass

        self.check_folder(folder)

        return folder / key

    def _staged_path(self, key: str) -> Path:
        if not STORAGE_KEY.fullmatch(key):
            raise ValueError("not a storage key")

        return self.staging_dir / f"{key}.part"

    def hold(self, path: Path) -> None:
        with self._lock:
            self._in_use.add(path)

    def release(self, path: Path) -> None:
        with self._lock:
            self._in_use.discard(path)

    # --- uploads ----------------------------------------------------------

    def new_staging(self) -> StagingFile:
        """A new staging file, named with a new storage key (O_EXCL: never an existing name)."""
        self.check_folder(self.staging_dir)
        key = secrets.token_hex(16)
        path = self._staged_path(key)
        fd = os.open(path, os.O_WRONLY | NEW_FILE, 0o600)
        self.hold(path)

        return StagingFile(self, key, path, os.fdopen(fd, "wb"))

    def place(self, staging: StagingFile, owner_id: str, *, area: str = FILES) -> None:
        """Give a received file its kept name, <area>/<owner id>/<its key>.

        A hard link: it fails, replacing nothing, if the name is taken, and
        the staging name keeps the same bytes until the upload removes it.
        """
        target = self._kept_path(owner_id, staging.key, create=True, area=area)
        os.link(staging.path, target, follow_symlinks=False)
        _sync_folder(target.parent)

    # --- kept files -------------------------------------------------------

    def open_kept(self, owner_id: str, key: str, *, area: str = FILES) -> BinaryIO:
        """The kept file, open for reading; raises MissingFile or UnreadableFile."""
        path = self._kept_path(owner_id, key, area=area)

        try:
            fd = os.open(path, READ_FILE)
        except FileNotFoundError as error:
            raise MissingFile() from error
        except OSError as error:
            # ELOOP: a link stands where the file should be; EACCES: no permission.
            raise UnreadableFile() from error

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise UnreadableFile()

        return os.fdopen(fd, "rb")

    def open_checked(self, owner_id: str, key: str, size: int, sha256: str, *, area: str = FILES) -> BinaryIO:
        """The kept file, after checking that it still has the recorded size and checksum."""
        handle = self.open_kept(owner_id, key, area=area)

        try:
            digest = hashlib.sha256()
            total = 0

            while chunk := handle.read(CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)

            if total != size or digest.hexdigest() != sha256:
                raise ChangedFile()

            handle.seek(0)
        except OSError as error:
            handle.close()
            raise UnreadableFile() from error
        except BaseException:
            handle.close()
            raise

        return handle

    def kept_file_state(self, owner_id: str, key: str) -> str:
        """"present", "missing" or "unreadable", found without reading the file."""
        try:
            info = os.lstat(self._kept_path(owner_id, key))
        except (MissingFile, FileNotFoundError):
            return "missing"
        except (FileStoreError, OSError):
            return "unreadable"

        return "present" if stat.S_ISREG(info.st_mode) else "unreadable"

    def remove_file(self, owner_id: str, key: str, *, area: str = FILES) -> None:
        """Remove an attachment's file under both names the store gives it.

        The staging name goes first: once it is gone, no kept name can be
        made from it any more, so an upload that is still running cannot
        leave one behind. Then the kept name. A name that cannot be removed
        raises, and the caller keeps the record (and its clean-up
        unfinished) until both are gone.
        """
        self.remove_staged(key)
        self.remove_kept(owner_id, key, area=area)

    def remove_staged(self, key: str) -> None:
        """Remove an upload's staging name, if it is still there."""
        path = self._staged_path(key)

        try:
            self.check_folder(self.staging_dir)
        except MissingFile:
            return

        _remove_name(path)

    def remove_kept(self, owner_id: str, key: str, *, area: str = FILES) -> None:
        """Remove a kept file's name.

        An owner folder that is not a real folder of the store raises, so
        the clean-up stays unfinished.
        """
        try:
            path = self._kept_path(owner_id, key, area=area)
        except MissingFile:
            return

        _remove_name(path)

    # --- export packages --------------------------------------------------

    def new_export(self) -> tuple[Path, BinaryIO]:
        """A new, empty file for an export package; its name is removed before it is written to."""
        self.check_folder(self.exports_dir)
        path = self.exports_dir / f"{secrets.token_hex(16)}.zip.part"
        fd = os.open(path, os.O_RDWR | NEW_FILE, 0o600)
        self.hold(path)

        return path, os.fdopen(fd, "w+b")

    def detach_export(self, path: Path) -> None:
        """Remove a package's name; a handle still open keeps its bytes until it is closed."""
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        finally:
            self.release(path)

    # --- clean-up ---------------------------------------------------------

    def sweep(
        self,
        referenced: set[tuple[str, str]],
        now: float | None = None,
        *,
        imported: set[tuple[str, str]] | None = None,
    ) -> SweepReport:
        """Remove leftovers that nobody touched for LEFTOVER_AFTER.

        These are staging and export files that no request of this process
        is using, and kept files that no attachment record refers to
        (referenced holds (owner id, storage key) pairs read before the
        sweep; a file placed since then is too new to be removed). With
        imported, the same for the imports area and the import records'
        pairs; without it, that area is not looked at. Names the store does
        not make, links and folders are left alone.
        """
        now = time.time() if now is None else now
        report = SweepReport()

        with self._lock:
            in_use = set(self._in_use)

        for folder, pattern, kind in (
            (self.staging_dir, STAGING_NAME, "staging"),
            (self.exports_dir, EXPORT_NAME, "exports"),
        ):
            for path in self._old_files(folder, pattern, now):
                if path not in in_use:
                    self._remove_leftover(path, kind, report)

        areas = [(self.files_dir, referenced)]

        if imported is not None:
            areas.append((self.imports_dir, imported))

        for base, known in areas:
            for owner_id in self._names(base):
                if not OWNER_ID.fullmatch(owner_id):
                    continue

                for path in self._old_files(base / owner_id, STORAGE_KEY, now):
                    if (owner_id, path.name) not in known:
                        self._remove_leftover(path, "kept", report)

        return report

    def _names(self, folder: Path) -> list[str]:
        try:
            self.check_folder(folder)
            return os.listdir(folder)
        except (FileStoreError, OSError):
            return []

    def _old_files(self, folder: Path, pattern: re.Pattern, now: float) -> list[Path]:
        found = []

        for name in self._names(folder):
            if not pattern.fullmatch(name):
                continue

            path = folder / name

            try:
                info = os.lstat(path)
            except OSError:
                continue

            if stat.S_ISREG(info.st_mode) and now - info.st_mtime >= LEFTOVER_AFTER:
                found.append(path)

        return found

    def _remove_leftover(self, path: Path, kind: str, report: SweepReport) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        except OSError as error:
            report.failed += 1
            logger.warning("could not remove a leftover %s file (%s)", kind, type(error).__name__)
            return

        setattr(report, kind, getattr(report, kind) + 1)


def _remove_name(path: Path) -> None:
    """Remove one name of a file; a name that is already gone counts as removed.

    A link found there is removed itself; what it points to is never
    touched. A folder found there raises.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return

    if stat.S_ISDIR(info.st_mode):
        raise UnreadableFile()

    try:
        os.unlink(path)
    except FileNotFoundError:
        return

    _sync_folder(path.parent)


def read_chunks(handle: BinaryIO) -> Iterator[bytes]:
    """The file's bytes in chunks; the file is closed also when the reader stops early."""
    try:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk
    finally:
        handle.close()


def _sync_folder(folder: Path) -> None:
    """Make a new or removed name durable. Not every system allows it, so failures are ignored."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return

    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
