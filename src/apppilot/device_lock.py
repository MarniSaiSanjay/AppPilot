"""Fail-fast inter-process ownership for one Android device serial."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable


class DeviceLockError(RuntimeError):
    """The selected Android device is already owned or cannot be locked."""


def normalize_device_serial(serial: str) -> str:
    normalized = " ".join((serial or "").split()).casefold()
    if not normalized:
        raise ValueError("Android device serial is empty")
    return normalized


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class DeviceProcessLock:
    """Atomic directory lock keyed by normalized device serial."""

    def __init__(
        self,
        device_serial: str,
        *,
        run_id: str | None = None,
        lock_root: str | Path | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        clock: Callable[[], float] = time.time,
        process_alive: Callable[[int], bool] = _process_alive,
    ) -> None:
        self.device_serial = device_serial.strip()
        self.normalized_serial = normalize_device_serial(device_serial)
        self.run_id = run_id or uuid.uuid4().hex
        self.pid = os.getpid() if pid is None else pid
        self.hostname = hostname or socket.gethostname()
        self._clock = clock
        self._process_alive = process_alive
        root = (
            Path(tempfile.gettempdir()) / "apppilot-device-locks"
            if lock_root is None
            else Path(lock_root)
        )
        lock_key = hashlib.sha256(
            self.normalized_serial.encode("utf-8")
        ).hexdigest()[:24]
        self._root = root
        self._path = root / f"{lock_key}.lock"
        self._owner_path = self._path / "owner.json"
        self._token = uuid.uuid4().hex
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self._path.mkdir()
            except FileExistsError:
                owner = self._read_owner()
                if owner is not None and self._owner_is_stale(owner):
                    self._reclaim_stale()
                    continue
                raise DeviceLockError(self._owned_message(owner))
            except OSError as error:
                raise DeviceLockError(
                    f"could not create device lock: {error}"
                ) from error
            try:
                self._write_owner()
            except Exception:
                try:
                    self._path.rmdir()
                except OSError:
                    pass
                raise
            self._acquired = True
            return
        raise DeviceLockError("device lock could not be acquired")

    def release(self) -> None:
        if not self._acquired:
            return
        owner = self._read_owner()
        if owner is None or owner.get("token") != self._token:
            self._acquired = False
            return
        try:
            self._owner_path.unlink(missing_ok=True)
            self._path.rmdir()
        except OSError:
            pass
        finally:
            self._acquired = False

    def __enter__(self) -> "DeviceProcessLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    def _write_owner(self) -> None:
        owner = {
            "pid": self.pid,
            "run_id": self.run_id,
            "device": self.device_serial,
            "normalized_device": self.normalized_serial,
            "hostname": self.hostname,
            "created_at_epoch": self._clock(),
            "token": self._token,
        }
        temporary = self._path / f"owner-{self._token}.tmp"
        try:
            temporary.write_text(
                json.dumps(owner, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self._owner_path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise DeviceLockError(
                f"could not write device lock metadata: {error}"
            ) from error

    def _read_owner(self) -> dict | None:
        try:
            decoded = json.loads(
                self._owner_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        required = {
            "pid": int,
            "run_id": str,
            "device": str,
            "normalized_device": str,
            "hostname": str,
            "token": str,
        }
        if any(
            not isinstance(decoded.get(name), expected_type)
            for name, expected_type in required.items()
        ):
            return None
        return decoded

    def _owner_is_stale(self, owner: dict) -> bool:
        return (
            owner.get("hostname") == self.hostname
            and owner.get("normalized_device") == self.normalized_serial
            and not self._process_alive(owner["pid"])
        )

    def _reclaim_stale(self) -> None:
        reclaimed = self._root / (
            f"{self._path.name}.stale-{uuid.uuid4().hex}"
        )
        try:
            os.replace(self._path, reclaimed)
        except FileNotFoundError:
            return
        except OSError as error:
            raise DeviceLockError(
                f"could not reclaim stale device lock: {error}"
            ) from error
        shutil.rmtree(reclaimed, ignore_errors=True)

    def _owned_message(self, owner: dict | None) -> str:
        if owner is None:
            return (
                f"device {self.device_serial!r} is locked by another AppPilot "
                f"process; lock metadata is malformed at {self._path}"
            )
        return (
            f"device {self.device_serial!r} is already in use by AppPilot "
            f"PID {owner['pid']} (run {owner['run_id']}, "
            f"host {owner['hostname']})"
        )
