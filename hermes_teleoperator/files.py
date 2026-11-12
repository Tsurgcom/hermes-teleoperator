"""Small binary file transfer; immutable copy-before-delete, no overwrite.

The wire protocol is shared with the standalone receiver. Gateway-local calls
must use ``local_file_operation``, which confines every path to a configured
transfer directory and refuses Hermes/SSH secrets before touching the disk.
"""
import base64
import ctypes
import ctypes.util
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys


MAX_FILE = 8 * 1024 * 1024


def _hermes_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser()
    except ImportError:
        return Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser()


def _gateway_home():
    return Path.home()


def _resolve(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError('Transfer path cannot be resolved safely') from exc


def _protected_roots():
    return (_resolve(_hermes_home()), _resolve(Path(_gateway_home()) / '.ssh'))


def local_transfer_path(raw, transfer_dir):
    """Resolve a gateway-local transfer path or refuse it.

    Resolution happens before every containment check, so a symlink cannot make
    a protected or out-of-tree target look local.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError('Transfer path must be a non-empty string')
    if not isinstance(transfer_dir, (str, os.PathLike)) or not os.fspath(transfer_dir):
        raise ValueError('Local transfers are disabled; set transfer_dir in teleoperator.json')
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError('Transfer path must be absolute')
    root_candidate = Path(transfer_dir).expanduser()
    if not root_candidate.is_absolute():
        raise ValueError('Configured transfer_dir must be an absolute path')
    root = _resolve(root_candidate)
    if not root.is_dir():
        raise ValueError('Configured transfer_dir must be an existing directory')
    resolved = _resolve(candidate)
    for protected in _protected_roots():
        if resolved.is_relative_to(protected):
            raise ValueError('Transfer path is in a protected location (HERMES_HOME or ~/.ssh)')
    if resolved == root:
        raise ValueError('Transfer path must name a file inside the configured transfer directory')
    if not resolved.is_relative_to(root):
        raise ValueError('Transfer path is outside the configured transfer directory')
    return resolved


def _parts(path):
    """Absolute path components, rejecting traversal and empty names."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Transfer path must be absolute')
    names = []
    for part in path.parts[1:]:
        if part in ('', '.', '..'):
            raise ValueError('Transfer path must not contain . or ..')
        names.append(part)
    if not names:
        raise ValueError('Transfer requires a regular, non-symlink file')
    return names


def _raise_walk(exc, dir_fd, name):
    # Linux reports O_NOFOLLOW|O_DIRECTORY on a symlink as ENOTDIR. lstat tells
    # a swapped-in symlink from a real non-directory; other ENOTDIR stays as-is.
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except OSError:
            info = None
        if (info is not None and stat.S_ISLNK(info.st_mode)) or exc.errno == errno.ELOOP:
            raise ValueError('Transfer path contains a symlink') from exc
    if exc.errno == errno.ENOTDIR:
        raise ValueError('Transfer path component is not a directory') from exc
    if exc.errno == errno.ENOENT:
        raise ValueError('Transfer requires a regular, non-symlink file') from exc
    raise exc


def _open_parent(path):
    """Directory fd of the parent, walking from / without following symlinks."""
    names = _parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    dir_fd = os.open('/', flags)
    try:
        for name in names[:-1]:
            try:
                next_fd = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                try:
                    _raise_walk(exc, dir_fd, name)
                finally:
                    os.close(dir_fd)
                    dir_fd = -1
            os.close(dir_fd)
            dir_fd = next_fd
        return dir_fd, names[-1]
    except Exception:
        if dir_fd >= 0:
            os.close(dir_fd)
        raise


def _open_leaf(dir_fd, leaf):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        return os.open(leaf, flags, dir_fd=dir_fd)
    except OSError as exc:
        _raise_walk(exc, dir_fd, leaf)


def _read_fd(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Transfer requires a regular, non-symlink file')
    chunks = []
    total = 0
    while total <= MAX_FILE:
        chunk = os.read(fd, MAX_FILE + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b''.join(chunks)
    if len(data) > MAX_FILE:
        raise ValueError('File exceeds 8 MiB transfer limit; use native scp/sftp for larger files')
    return data, info


def _file_get(path):
    dir_fd, leaf = _open_parent(path)
    try:
        fd = _open_leaf(dir_fd, leaf)
        try:
            data, _info = _read_fd(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError(errno.EIO, 'short write')
        view = view[written:]


def _libc():
    name = ctypes.util.find_library('c')
    if not name:
        return None
    libc = ctypes.CDLL(name, use_errno=True)
    libc.linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    libc.linkat.restype = ctypes.c_int
    if hasattr(libc, 'renameat2'):
        libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        libc.renameat2.restype = ctypes.c_int
    if hasattr(libc, 'unlinkat'):
        libc.unlinkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        libc.unlinkat.restype = ctypes.c_int
    return libc


_LIBC = _libc() if sys.platform.startswith('linux') else None
_AT_EMPTY_PATH = 0x1000


def _same_inode(left, right):
    return stat.S_ISREG(right.st_mode) and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_published(stage_fd, dir_fd, leaf):
    published = _open_leaf(dir_fd, leaf)
    try:
        if not _same_inode(os.fstat(stage_fd), os.fstat(published)):
            raise ValueError('Published file did not match the staged bytes')
    finally:
        os.close(published)


_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x4
_O_TMPFILE = getattr(os, 'O_TMPFILE', 0)
_FALLBACK_LINK_ERRORS = (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOENT)


def _rename_noreplace(src_dir, src, dst_dir, dst):
    """Rename without replacing an existing destination."""
    if _LIBC is not None and sys.platform.startswith('linux') and hasattr(_LIBC, 'renameat2'):
        rc = _LIBC.renameat2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), _RENAME_NOREPLACE)
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), dst)
        raise OSError(err, os.strerror(err), dst)
    if _LIBC is not None and sys.platform == 'darwin' and hasattr(_LIBC, 'renameatx_np'):
        _LIBC.renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        _LIBC.renameatx_np.restype = ctypes.c_int
        rc = _LIBC.renameatx_np(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), _RENAME_EXCL)
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), dst)
        raise OSError(err, os.strerror(err), dst)
    raise OSError(errno.EOPNOTSUPP, 'atomic no-replace rename is unavailable')


def _link_inode(stage_fd, dir_fd, leaf):
    """Link the open inode. False means the filesystem rejected inode links."""
    if _LIBC is None:
        return False
    rc = _LIBC.linkat(stage_fd, b'', dir_fd, os.fsencode(leaf), _AT_EMPTY_PATH)
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), leaf)
    if err in _FALLBACK_LINK_ERRORS:
        return False
    raise OSError(err, os.strerror(err), leaf)


def _publish(stage_fd, dir_fd, staging, leaf):
    """Link the open inode, then refuse to report its checksum if the destination is a different file."""
    if _link_inode(stage_fd, dir_fd, leaf):
        _require_published(stage_fd, dir_fd, leaf)
        return
    raise OSError(errno.EOPNOTSUPP, 'filesystem cannot link the staged file into place')


def _open_unnamed(dir_fd):
    """Open a file with no directory entry, so nothing can replace it by name."""
    if not _O_TMPFILE:
        raise OSError(errno.EOPNOTSUPP, 'O_TMPFILE unavailable')
    return os.open('.', _O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)


def _open_stage(dir_fd):
    # A named stage in a directory writable by an adversary cannot be safely
    # linked or cleaned up by name, even after checking its inode.
    return _open_unnamed(dir_fd), None


def _file_put(path, params):
    if len(params['data']) > (MAX_FILE * 4 // 3 + 8):
        raise ValueError('File exceeds transfer limit')
    data = base64.b64decode(params['data'], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_FILE or digest != params['sha256']:
        raise ValueError('Size or checksum mismatch')
    dir_fd, leaf = _open_parent(path)
    stage_fd = -1
    staging = None
    try:
        stage_fd, staging = _open_stage(dir_fd)
        _write_all(stage_fd, data)
        os.fsync(stage_fd)
        _publish(stage_fd, dir_fd, staging, leaf)
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)
        os.close(dir_fd)
    return {'path': str(path), 'sha256': digest, 'size': len(data)}


def _file_remove(path, params):
    expected = params['sha256']
    dir_fd, leaf = _open_parent(path)
    try:
        fd = _open_leaf(dir_fd, leaf)
        try:
            data, _info = _read_fd(fd)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Source changed; refusing deletion')
            # POSIX provides no atomic unlink-if-inode-and-content-matches.
            # A same-uid writer can replace even an entry inside a mode-0700
            # temporary directory between verification and unlink/rename.
            raise OSError(errno.EOPNOTSUPP, 'Safe source deletion is unavailable; remove it manually')
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return {'removed': str(path)}


def file_operation(method, params):
    """Unconfined wire-protocol operation for a trusted receiver endpoint.

    Opens by walking from ``/`` with ``O_NOFOLLOW``, so a symlink swapped into
    any path component is refused instead of followed.
    """
    path = Path(params['path']).expanduser().absolute()
    if method == 'file_get':
        return _file_get(path)
    if method == 'file_put':
        return _file_put(path, params)
    if method == 'file_remove':
        return _file_remove(path, params)
    raise ValueError('Unknown file operation')


def local_file_operation(method, params, transfer_dir, allow_delete=False):
    """Gateway-local endpoint: confined to transfer_dir, deletes opt-in only."""
    if method not in {'file_get', 'file_put', 'file_remove'}:
        raise ValueError('Unknown file operation')
    if method == 'file_remove' and allow_delete is not True:
        raise ValueError('Local moves are disabled; set allow_local_delete in teleoperator.json')
    path = local_transfer_path(params.get('path'), transfer_dir)
    if method == 'file_get':
        return _file_get(path)
    if method == 'file_put':
        return _file_put(path, params)
    return _file_remove(path, params)
