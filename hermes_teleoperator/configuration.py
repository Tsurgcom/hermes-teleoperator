"""Add-only profile configuration. Locking coordinates cooperating writers only.

Paths are deliberately conservative; SSH aliases and paths requiring shell/SSH
expansion are not accepted. The profile directory must already exist. External
editors that ignore the advisory lock can still race this operation.
"""
try:
    import fcntl
except ImportError:  # Keep the plugin usable; manual setup fails closed without locking.
    fcntl = None
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile


_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_HOST = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')
_USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')
_PATH = re.compile(r'[A-Za-z0-9_./-]+\Z')
_FIELDS = {'name', 'host', 'port', 'executable', 'identity_file', 'known_hosts', 'teleoperator_home'}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON field: {key}')
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, object_pairs_hook=_unique_object)


def valid_host(host):
    if not isinstance(host, str):
        return False
    if _HOST.fullmatch(host):
        return True
    user, separator, address = host.rpartition('@')
    if separator and not _USER.fullmatch(user):
        return False
    try:
        ipaddress.IPv6Address(address)
        return '%' not in address
    except ValueError:
        return False


def validate_machine(raw):
    value = parse_json(raw)
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError('Expected a machine object with only supported fields')
    name, host = value.get('name'), value.get('host')
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.lower() in {'local', 'back', 'status', 'switch', 'reconnect', 'disconnect'}:
        raise ValueError('Invalid or reserved machine name')
    if not valid_host(host):
        raise ValueError('Invalid SSH host or alias')
    if 'port' in value and (type(value['port']) is not int or not 1 <= value['port'] <= 65535):
        raise ValueError('Invalid SSH port')
    for key in ('executable', 'identity_file', 'known_hosts', 'teleoperator_home'):
        if key not in value:
            continue
        path = value[key]
        if (not isinstance(path, str) or not _PATH.fullmatch(path) or path.startswith('-')
                or '..' in path.split('/') or path in {'.', '/'}):
            raise ValueError(f'Unsafe {key} path')
    return name, {key: val for key, val in value.items() if key != 'name'}


def add_machine(path, raw, controller):
    name, target = validate_machine(raw)
    if fcntl is None or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('Manual setup requires secure file locking on this gateway')
    path = Path(path)
    # A separate stable lock inode is required because the config inode is replaced.
    lock_fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError('Invalid configuration lock')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with controller.lock:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                settings, mode = {'machines': {}}, 0o600
            else:
                with os.fdopen(fd, 'r', encoding='utf-8') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError('Configuration must be a regular file')
                    mode = stat.S_IMODE(info.st_mode)
                    settings = parse_json(source.read())
            if not isinstance(settings, dict) or not isinstance(settings.get('machines', {}), dict):
                raise ValueError('machines must be an object')
            targets = settings.setdefault('machines', {})
            if any(not isinstance(k, str) or k.lower() == 'local' for k in targets):
                raise ValueError('Invalid or reserved configured machine')
            if any(k.lower() == name.lower() for k in targets) or any(k.lower() == name.lower() for k in controller.targets):
                raise ValueError('Machine already exists')
            # Never mutate the meaning of an existing live connection on reload.
            if any(targets.get(k) != v for k, v in controller.targets.items()):
                raise ValueError('Existing machine configuration changed; restart before adding')
            targets[name] = target
            fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    json.dump(settings, out, indent=2, ensure_ascii=False)
                    out.write('\n')
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            controller.targets = dict(targets)
    finally:
        os.close(lock_fd)
