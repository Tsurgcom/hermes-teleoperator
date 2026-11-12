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
import time

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


def _unlink_if_same(dir_fd, name, stage_fd):
    try:
        found = _open_leaf(dir_fd, name)
    except (OSError, ValueError):
        return
    try:
        if _same_inode(os.fstat(stage_fd), os.fstat(found)):
            os.unlink(name, dir_fd=dir_fd)
    finally:
        os.close(found)


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
    try:
        os.lstat(dst, dir_fd=dst_dir)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_dir, dst_dir_fd=dst_dir)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)


def _private_name(prefix):
    return f'.teleoperator-{prefix}-{os.getpid()}-{time.time_ns()}-{os.urandom(4).hex()}'


def _kept_name():
    return _private_name('kept')


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


def _displace(dir_fd, name, stage_fd):
    """Move a mismatched destination aside. Never unlink it and never overwrite."""
    try:
        found = _open_leaf(dir_fd, name)
    except (OSError, ValueError):
        return
    try:
        if _same_inode(os.fstat(stage_fd), os.fstat(found)):
            return
    finally:
        os.close(found)
    for _ in range(8):
        try:
            _rename_noreplace(dir_fd, name, dir_fd, _kept_name())
            return
        except FileExistsError:
            continue


def _publish_by_name(stage_fd, dir_fd, staging, leaf):
    """Name-based link used only when inode links are unavailable.

    The staging directory entry is re-checked first. If the destination still
    is not the staged inode, it is renamed aside before the error is reported
    so the caller does not observe those bytes at the destination.
    """
    pre = _open_leaf(dir_fd, staging)
    try:
        if not _same_inode(os.fstat(stage_fd), os.fstat(pre)):
            raise ValueError('Staging file did not match the staged bytes')
    finally:
        os.close(pre)
    os.link(staging, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    try:
        _require_published(stage_fd, dir_fd, leaf)
    except Exception:
        _displace(dir_fd, leaf, stage_fd)
        raise


def _publish(stage_fd, dir_fd, staging, leaf):
    """Link the open inode, then refuse to report its checksum if the destination is a different file."""
    if _link_inode(stage_fd, dir_fd, leaf):
        _require_published(stage_fd, dir_fd, leaf)
        return
    if staging is None:
        raise OSError(errno.EOPNOTSUPP, 'filesystem cannot link the staged file into place')
    _publish_by_name(stage_fd, dir_fd, staging, leaf)


def _open_unnamed(dir_fd):
    """Open a file with no directory entry, so nothing can replace it by name."""
    if not _O_TMPFILE:
        raise OSError(errno.EOPNOTSUPP, 'O_TMPFILE unavailable')
    return os.open('.', _O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)


def _open_stage(dir_fd):
    try:
        return _open_unnamed(dir_fd), None
    except OSError as exc:
        if exc.errno not in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTSUP, errno.EISDIR,
                             errno.ENOSYS, errno.EPERM, errno.EACCES):
            raise
    name = f'.teleoperator-{os.getpid()}-{time.time_ns()}'
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    return fd, name


def _make_seal(dir_fd):
    """Private directory that holds a file during deletion.

    The final unlink happens here, not at a predictable public temporary name.
    """
    for _ in range(8):
        name = f'.teleoperator-seal-{os.getpid()}-{time.time_ns()}-{os.urandom(8).hex()}'
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            continue
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            entries = os.listdir(f'/proc/self/fd/{fd}')
        except OSError:
            os.close(fd)
            continue
        if stat.S_ISDIR(info.st_mode) and not entries:
            return fd, name
        os.close(fd)
        # A non-empty directory is not ours to clean; rmdir fails closed on it.
        try:
            os.rmdir(name, dir_fd=dir_fd)
        except OSError:
            pass
    raise OSError(errno.EIO, 'could not create a directory for deletion')


def _link_held(held_fd, dir_fd):
    """Add a second name for held_fd so one rename-over cannot drop its last link.

    Returns the new name, or None when this filesystem cannot make the link.
    The caller's inode is left unchanged in that case.
    """
    if _LIBC is None:
        return None
    for _ in range(8):
        name = _private_name('hold')
        rc = _LIBC.linkat(held_fd, b'', dir_fd, os.fsencode(name), _AT_EMPTY_PATH)
        if rc == 0:
            return name
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            continue
        return None
    return None


def _name_is_held(dir_fd, name, held_fd):
    try:
        found = _open_leaf(dir_fd, name)
    except (OSError, ValueError):
        return False
    try:
        return _same_inode(os.fstat(held_fd), os.fstat(found))
    finally:
        os.close(found)


def _move_held(src_dir, src, dst_dir, dst):
    """Move src onto a free name. Never replace an existing file."""
    try:
        _rename_noreplace(src_dir, src, dst_dir, dst)
        return True
    except FileExistsError:
        pass
    except FileNotFoundError:
        return False
    for _ in range(8):
        try:
            _rename_noreplace(src_dir, src, dst_dir, _kept_name())
            return True
        except FileExistsError:
            continue
        except FileNotFoundError:
            return False
    return False


def _preserve(seal_fd, dir_fd, leaf, held_fd, names):
    """Put held_fd's inode back without moving or unlinking a different file.

    When the original path already names that inode, only the extra links are
    removed. A name that no longer refers to it, such as a file swapped in
    during the re-read, is left in place.
    """
    if _name_is_held(dir_fd, leaf, held_fd):
        for name in names:
            if name and _name_is_held(seal_fd, name, held_fd):
                _unlink_matching(seal_fd, name, held_fd)
        return
    restored = False
    for name in names:
        if not name or not _name_is_held(seal_fd, name, held_fd):
            continue
        if not restored:
            restored = _move_held(seal_fd, name, dir_fd, leaf)
        else:
            _unlink_matching(seal_fd, name, held_fd)


def _recheck(dir_fd, name, held_fd, expected):
    """Re-read name and confirm it still refers to held_fd after that read."""
    check = _open_leaf(dir_fd, name)
    try:
        if not _same_inode(os.fstat(held_fd), os.fstat(check)):
            return False
        os.lseek(check, 0, os.SEEK_SET)
        again, _info = _read_fd(check)
    finally:
        os.close(check)
    if hashlib.sha256(again).hexdigest() != expected:
        return False
    return _name_is_held(dir_fd, name, held_fd)


def _unlink_matching(dir_fd, name, held_fd):
    """Unlink name only when it still refers to held_fd.

    Success requires that this inode's link count dropped by one. A replacement
    swapped onto the name does not drop it, so that unlink is not reported as
    deletion of the caller's file.
    """
    try:
        found = _open_leaf(dir_fd, name)
    except (OSError, ValueError):
        return False
    try:
        held_stat = os.fstat(held_fd)
        if not _same_inode(held_stat, os.fstat(found)):
            return False
        before = held_stat.st_nlink
        if before < 1:
            return False
        os.unlink(name, dir_fd=dir_fd)
        return os.fstat(held_fd).st_nlink + 1 == before
    finally:
        os.close(found)


def _release_seal(seal_fd, seal_name, dir_fd):
    try:
        try:
            os.rmdir(seal_name, dir_fd=dir_fd)
        except OSError:
            pass
    finally:
        os.close(seal_fd)


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
        if staging is not None and stage_fd >= 0:
            try:
                _unlink_if_same(dir_fd, staging, stage_fd)
            except OSError:
                pass
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
            seal_fd, seal_name = _make_seal(dir_fd)
            try:
                # hold keeps the inode reachable if a writer replaces the moved
                # name while it is being re-read. data is the name we unlink.
                hold = _link_held(fd, seal_fd)
                moved = False
                try:
                    _rename_noreplace(dir_fd, leaf, seal_fd, 'data')
                    moved = True
                    if not _recheck(seal_fd, 'data', fd, expected):
                        raise ValueError('Source changed; refusing deletion')
                    if not _unlink_matching(seal_fd, 'data', fd):
                        raise ValueError('Source changed; refusing deletion')
                    if hold is not None and not _unlink_matching(seal_fd, hold, fd):
                        raise ValueError('Source changed; refusing deletion')
                except Exception:
                    try:
                        names = [hold, 'data'] if moved else [hold]
                        _preserve(seal_fd, dir_fd, leaf, fd, names)
                    except OSError:
                        pass
                    raise
            finally:
                _release_seal(seal_fd, seal_name, dir_fd)
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
