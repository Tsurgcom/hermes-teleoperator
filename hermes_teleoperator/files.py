"""Small binary file transfer; immutable copy-before-delete, no overwrite.

The wire protocol is shared with the standalone receiver. Gateway-local calls
must use ``local_file_operation``, which confines every path to a configured
transfer directory and refuses Hermes/SSH secrets before touching the disk.
"""
import base64
import hashlib
import os
from pathlib import Path
import tempfile

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


def _file_get(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError('Transfer requires a regular, non-symlink file')
    with path.open('rb') as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError('File exceeds 8 MiB transfer limit; use native scp/sftp for larger files')
    return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def _file_put(path, params):
    if len(params['data']) > (MAX_FILE * 4 // 3 + 8):
        raise ValueError('File exceeds transfer limit')
    data = base64.b64decode(params['data'], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_FILE or digest != params['sha256']:
        raise ValueError('Size or checksum mismatch')
    fd, staging = tempfile.mkstemp(prefix='.teleoperator-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # link publishes atomically and refuses existing destinations.
        os.link(staging, path)
    finally:
        os.unlink(staging)
    return {'path': str(path), 'sha256': digest, 'size': len(data)}


def _file_remove(path, params):
    snapshot = _file_get(path)
    if snapshot['sha256'] != params['sha256']:
        raise ValueError('Source changed; refusing deletion')
    path.unlink()
    return {'removed': str(path)}


def file_operation(method, params):
    """Unconfined wire-protocol operation for a trusted receiver endpoint."""
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
