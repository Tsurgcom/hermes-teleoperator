#!/usr/bin/env python3
"""Install a user-local companion without modifying Hermes' Python environment."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def check_user(allow_root=False):
    if os.environ.get('SUDO_USER'):
        raise ValueError('Do not run via sudo; log in as the intended SSH user instead')
    if os.geteuid() == 0 and not allow_root:
        raise ValueError('Root installation requires --allow-root; prefer an ordinary SSH user')


def check_python(python):
    probe = ('import importlib.util,pathlib,sys; assert sys.version_info >= (3,11); '
             'spec=importlib.util.find_spec("model_tools"); assert spec and spec.origin; '
             'root=str(pathlib.Path(spec.origin).parent); sys.path.insert(0,root); '
             'import hermes_yaml,model_tools; print(root)')
    result = subprocess.run([python, '-c', probe], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=sys.stderr)
    return result.stdout.strip().splitlines()[-1]


def validate_key(value):
    if '\n' in value.strip() or '\r' in value:
        raise ValueError('Provide one SSH public key, never a private key')
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('Expected one ssh-ed25519 public key')
    try:
        data = base64.b64decode(parts[1], validate=True)
        if len(data) != 51 or data[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
            raise ValueError('Invalid ed25519 key')
    except Exception as exc:
        raise ValueError('Invalid ed25519 public key') from exc
    return ' '.join(parts[:2])


def secure_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Refusing symlink: {part}')
    if path.exists() and path.stat().st_uid != os.geteuid():
        raise ValueError(f'Not owned by current user: {path}')


def install(source, python, home, hermes_home, public_key=None, allow_root=False):
    check_user(allow_root)
    home, hermes_home = Path(home).absolute(), Path(hermes_home).absolute()
    python = os.path.abspath(python)  # Preserve venv symlink identity.
    hermes_source = check_python(python)
    base = home / '.local/lib/hermes-teleoperator'
    ssh = home / '.ssh'
    auth = ssh / 'authorized_keys'
    for path in (base, ssh, auth):
        secure_path(path)
    key = validate_key(public_key) if public_key else None
    existing = auth.read_text() if key and auth.exists() else ''
    launcher = base / 'python'
    remote = shlex.join([str(launcher), '-m', 'hermes_teleoperator.companion'])
    # OpenSSH's quoted option format, separate from the shell quoting above.
    command = remote.replace('\\', '\\\\').replace('"', '\\"')
    entry = f'restrict,command="{command}" {key} hermes-teleoperator' if key else None
    if key:
        for line in existing.splitlines():
            if key.split()[1] in line and line != entry:
                raise ValueError('Public key already authorised differently; use a dedicated new key')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = base / 'hermes_teleoperator'
    secure_path(package)
    if package.exists():
        # Prevent following links during upgrades.
        for child in package.rglob('*'):
            secure_path(child)
        shutil.rmtree(package)
    shutil.copytree(Path(source) / 'hermes_teleoperator', package,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    secure_path(launcher)
    launcher.write_text('#!/bin/sh\nexport HERMES_HOME=' + shlex.quote(str(hermes_home)) +
                        '\nexport PYTHONPATH=' + shlex.quote(str(base) + ':' + hermes_source) +
                        '\nexec ' + shlex.quote(python) + ' "$@"\n')
    launcher.chmod(0o700)
    if key:
        assert entry is not None
        ssh.mkdir(mode=0o700, exist_ok=True)
        ssh.chmod(0o700)
        if entry not in existing.splitlines():
            with auth.open('a') as stream:
                if existing and not existing.endswith('\n'):
                    stream.write('\n')
                stream.write(entry + '\n')
        auth.chmod(0o600)
    result = {'python': str(launcher), 'hermes_home': str(hermes_home),
              'user': __import__('pwd').getpwuid(os.geteuid()).pw_name,
              'key_installed': bool(key)}
    manifest = base / 'install.json'
    secure_path(manifest)
    manifest.write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, help='Absolute Hermes Python interpreter path')
    parser.add_argument('--hermes-home', default=os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument('--public-key')
    keys.add_argument('--public-key-file', type=Path)
    parser.add_argument('--allow-root', action='store_true')
    args = parser.parse_args()
    if not Path(args.python).is_absolute():
        parser.error('--python must be an absolute path')
    try:
        result = install(Path(__file__).resolve().parents[1], args.python, Path.home(),
                         args.hermes_home, args.public_key_file.read_text() if args.public_key_file else args.public_key,
                         args.allow_root)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Installation failed: {exc}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
