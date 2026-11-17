"""Single-machine OS locks and same-volume durable file commits."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import uuid


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('unverifiable process identity')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return None
            raise PermissionError('process owner cannot be verified')
        try:
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, *map(ctypes.byref, (created, exited, system, user))):
                raise PermissionError('process startup time cannot be verified')
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    path = Path('/proc') / str(pid) / 'stat'
    try:
        # comm may contain spaces; field 22 follows the closing parenthesis.
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + path.read_text().rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _lock(file, acquire):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


class WorkspaceLock:
    """Metadata does not grant ownership; an actual OS lock is mandatory.

    Lock order: workspace -> run -> SQLite transaction. Never delete lock files.
    Unknown, corrupt, foreign-host or still-live stale metadata requires recovery.
    """
    def __init__(self, path, run_id):
        self.path, self.run_id = Path(path), run_id
        self.file = None

    def _metadata(self, state):
        return {'hostname': socket.gethostname(), 'pid': os.getpid(),
                'process_start_identity': process_identity(os.getpid()), 'run_id': self.run_id,
                'at': datetime.now(timezone.utc).isoformat(), 'state': state}

    def _write(self, value):
        self.file.seek(1)
        self.file.write(json.dumps(value, sort_keys=True).encode())
        self.file.truncate()
        self.file.flush()
        os.fsync(self.file.fileno())

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self.file = os.fdopen(descriptor, 'r+b')
            if self.path.stat().st_size == 0:
                self.file.write(b'\0')
                self.file.flush()
            _lock(self.file, True)
        except OSError:
            if self.file:
                self.file.close()
            raise RuntimeError('LOCK_BUSY_OR_UNAVAILABLE')
        try:
            self.file.seek(1)
            previous_bytes = self.file.read()
            if previous_bytes:
                previous = json.loads(previous_bytes)
                if previous.get('state') != 'RELEASED':
                    if previous.get('hostname') != socket.gethostname():
                        raise RuntimeError('RECOVERY_ERROR: foreign or unknown lock owner')
                    identity = process_identity(previous.get('pid'))
                    if identity is not None and identity == previous.get('process_start_identity'):
                        raise RuntimeError('RECOVERY_ERROR: live owner metadata without confirmed release')
            self._write(self._metadata('HELD'))
            return self
        except Exception as error:
            _lock(self.file, False)
            self.file.close()
            self.file = None
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError('RECOVERY_ERROR: lock metadata cannot be verified') from error

    def __exit__(self, *exception):
        try:
            self._write(self._metadata('RELEASED'))
        finally:
            _lock(self.file, False)
            self.file.close()
            self.file = None


def fsync_directory(path):
    if os.name == 'nt':
        return False  # no portable Windows directory-fsync guarantee claimed
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
        return True
    finally:
        os.close(descriptor)


def atomic_write(path, data, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the same-volume temporary basename short. Content-addressed target
    # names are already long; repeating them can exceed Windows MAX_PATH even
    # when the final destination itself is valid.
    temporary = path.with_name('.rsi-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # same-filesystem atomic no-overwrite commit
            temporary.unlink()
        else:
            os.replace(temporary, path)
        return {'file_fsync': True, 'directory_fsync': fsync_directory(path.parent),
                'same_volume': True, 'exclusive': exclusive}
    finally:
        if temporary.exists():
            temporary.unlink()


def restrict_directory(path):
    """Private evidence ACL: current principal and SYSTEM, or POSIX 0700."""
    path = Path(path).resolve()
    if not path.is_dir(): raise ValueError('restricted evidence directory must exist')
    if os.name != 'nt':
        path.chmod(0o700)
        return {'mode':'0700'}
    import csv
    import io
    import subprocess
    principal = list(csv.reader(io.StringIO(subprocess.check_output(
        ['whoami','/user','/fo','csv','/nh'],text=True))))[0][1]
    subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',
        '*'+principal+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,capture_output=True)
    return {'acl':'current Windows principal and SYSTEM only','inherited_access_removed':True}
