"""Small binary file transfer; immutable copy-before-delete, no overwrite."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile

MAX_FILE = 8 * 1024 * 1024


def file_operation(method, params):
    path = Path(params['path']).expanduser().absolute()
    if method == 'file_get':
        if not path.is_file() or path.is_symlink():
            raise ValueError('Transfer requires a regular, non-symlink file')
        with path.open('rb') as f:
            data = f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError('File exceeds 8 MiB transfer limit; use native scp/sftp for larger files')
        return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    if method == 'file_put':
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
    if method == 'file_remove':
        snapshot = file_operation('file_get', {'path': str(path)})
        if snapshot['sha256'] != params['sha256']:
            raise ValueError('Source changed; refusing deletion')
        path.unlink()
        return {'removed': str(path)}
    raise ValueError('Unknown file operation')
