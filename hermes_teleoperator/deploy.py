"""Pinned gateway-led deployment. Run ``python -m hermes_teleoperator.deploy HOST``."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request

from .transport import RpcProcess, ssh_argv
from .version import RECEIVER_SHA256, VERSION


class DeployError(RuntimeError):
    """A failed deployment is not automatically retried."""


class DeploymentUnverified(DeployError):
    """The launcher switch committed or its outcome is unknown; inspect before retrying."""


# Every target published by release.yml, including non-default glibc variants.
TARGETS = (
    'x86_64-unknown-linux-musl', 'aarch64-unknown-linux-musl',
    'armv7-unknown-linux-musleabihf', 'i686-unknown-linux-musl',
    'riscv64gc-unknown-linux-musl', 'x86_64-unknown-linux-gnu',
    'aarch64-unknown-linux-gnu', 'armv7-unknown-linux-gnueabihf',
    'i686-unknown-linux-gnu', 'powerpc64le-unknown-linux-gnu',
    's390x-unknown-linux-gnu', 'aarch64-apple-darwin', 'x86_64-apple-darwin',
)
ASSETS = frozenset('hermes-teleoperator-receiver-' + t for t in TARGETS)
MAX_BINARY = 64 * 1024 * 1024


def validate_version(version=None):
    tag = 'v' + VERSION
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', tag):
        raise DeployError('Invalid gateway VERSION')
    if version is not None and version != tag:
        raise DeployError(f'Deployment requires exact gateway tag {tag}; no downgrade or latest')
    return tag


def _fetch(url, destination, limit):
    """Bound each read and total download; HTTPS redirects only."""
    class HTTPSOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not newurl.startswith('https://'):
                raise DeployError('Refusing non-HTTPS release redirect')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(HTTPSOnly())
    deadline = time.monotonic() + 120
    with opener.open(url, timeout=20) as response, destination.open('xb') as output:
        if response.status != 200:
            raise DeployError('Release asset unavailable')
        size = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit or time.monotonic() > deadline:
                raise DeployError('Release download exceeds size/time limit')
            output.write(chunk)


def pinned_digest(asset, pins=None):
    """Return the source-committed SHA256 for ``asset`` at the exact tag.

    Source is the trust root. A release manifest is never consulted here, and a
    missing/unknown pin fails closed instead of falling back to ``SHA256SUMS``.
    """
    if asset not in ASSETS:
        raise DeployError('Unknown release asset')
    pins = RECEIVER_SHA256 if pins is None else pins
    digest = pins.get(asset)
    if not digest:
        raise DeployError(f'No SHA256 pinned in source for {asset} at v{VERSION}; refusing release download')
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise DeployError('Malformed SHA256 pinned in source')
    return digest


def download_release(asset, directory, *, fetch=None, pins=None):
    """Fetch only the exact tag to a gateway-owned private temporary directory."""
    expected = pinned_digest(asset, pins)
    fetch = fetch or _fetch
    base = 'https://github.com/Tsurgcom/hermes-teleoperator/releases/download/' + validate_version()
    manifest = directory / 'SHA256SUMS'
    fetch(base + '/SHA256SUMS', manifest, 32768)
    try:
        text = manifest.read_text(encoding='ascii')
    except UnicodeError as exc:
        raise DeployError('Malformed checksum manifest') from exc
    if len(text) > 32768:
        raise DeployError('Oversized checksum manifest')
    entries = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([a-f0-9]{64}) [ *]([A-Za-z0-9._-]+)', line)
        if not match or match[2] not in ASSETS or match[2] in entries:
            raise DeployError('Malformed, unknown, or duplicate checksum manifest entry')
        entries[match[2]] = match[1]
    if asset not in entries:
        raise DeployError(f'Exact release asset missing from SHA256SUMS: {asset}')
    binary = directory / 'receiver'
    fetch(base + '/' + asset, binary, MAX_BINARY)
    actual = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not 0 < binary.stat().st_size <= MAX_BINARY or actual != expected:
        # A compromised release rewrites both the asset and its manifest. The
        # source pin is authoritative, so the binary is checked before the
        # downloaded manifest is allowed to say anything.
        raise DeployError('Release binary does not match the SHA256 pinned in source')
    if entries[asset] != expected:
        raise DeployError(
            f'SHA256SUMS disagrees with the SHA256 pinned in source for {asset}; refusing rewritten release')
    return binary, expected



def _version(text):
    match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', text)
    if not match:
        raise DeployError('Cannot establish OS/libc version')
    return tuple(int(x or 0) for x in match.groups())


def select_asset(probe):
    """Choose the ABI of the running shell, not the kernel's uname architecture."""
    if probe.get('os') == 'Darwin':
        arm = probe.get('arm64') == '1'
        if probe.get('translated') == '1' and not arm:
            raise DeployError('Rosetta detected but native ARM capability is unclear')
        arch = 'aarch64' if arm else probe.get('arch')
        minimum = (11, 0, 0) if arm else (10, 12, 0)
        if arch not in ('aarch64', 'x86_64') or _version(probe.get('macos', '')) < minimum:
            raise DeployError('Unsupported macOS CPU/version (arm64 >=11, Intel >=10.12)')
        return f'hermes-teleoperator-receiver-{arch}-apple-darwin'
    if probe.get('os') != 'Linux':
        raise DeployError('Only Linux and macOS receivers are shipped; Windows is unsupported')
    if _version(probe.get('kernel', '')) < (3, 2, 0):
        raise DeployError('Linux kernel >=3.2 is required')
    try:
        h = bytes(map(int, probe.get('elf', '').split()))
    except ValueError as exc:
        raise DeployError('Invalid shell ELF evidence') from exc
    if len(h) < 52 or h[:4] != b'\x7fELF' or h[4] not in (1, 2) or h[5] not in (1, 2):
        raise DeployError('Cannot establish shell ELF class/endianness; refusing to guess')
    endian = 'little' if h[5] == 1 else 'big'
    machine = int.from_bytes(h[18:20], endian)
    flags = int.from_bytes(h[48:52] if h[4] == 2 else h[36:40], endian)
    cpu = probe.get('cpu', '').lower()
    key = (machine, h[4], endian)
    static = {(62, 2, 'little'): 'x86_64', (3, 1, 'little'): 'i686',
              (183, 2, 'little'): 'aarch64', (40, 1, 'little'): 'armv7',
              (243, 2, 'little'): 'riscv64gc'}
    arch = static.get(key)
    if arch == 'i686' and not re.search(r'\bsse2\b', cpu):
        raise DeployError('i686 receiver requires an SSE2-capable CPU')
    if arch == 'armv7' and (flags & 0xff000000 != 0x05000000 or
                           flags & 0x600 != 0x400 or probe.get('armv7') != '1'):
        raise DeployError('ARM requires proven ARMv7+ EABI5 hard-float; soft/unknown float ABI unsupported')
    if arch in ('aarch64', 'riscv64gc'):
        floor = (4, 1, 0) if arch == 'aarch64' else (4, 20, 0)
        if _version(probe['kernel']) < floor:
            raise DeployError(f'{arch} receiver requires Linux kernel >= {floor[0]}.{floor[1]}')
    isa = re.search(r'\brv64([a-z0-9_]+)', cpu)
    extensions = [] if not isa else isa[1].split('_')
    letters = set(extensions[0]) if extensions else set()
    letters.update(x[0] for x in extensions[1:] if re.fullmatch(r'[imafdc](?:\d+p\d+)?', x))
    riscv_gc = bool(extensions and (extensions[0].startswith('g') or set('imafdc') <= letters))
    if arch == 'riscv64gc' and (flags & 6 != 4 or not riscv_gc):
        raise DeployError('RISC-V requires proven rv64gc CPU and double-float ABI')
    if arch:
        suffix = 'musleabihf' if arch == 'armv7' else 'musl'
        return f'hermes-teleoperator-receiver-{arch}-unknown-linux-{suffix}'
    dynamic = {(21, 2, 'little'): ('powerpc64le', (2, 19, 0)),
               (22, 2, 'big'): ('s390x', (2, 17, 0))}
    if key in dynamic:
        arch, floor = dynamic[key]
        match = re.fullmatch(r'glibc (\d+\.\d+(?:\.\d+)?)', probe.get('glibc', ''))
        if not match or _version(match[1]) < floor:
            raise DeployError(f'{arch} requires proven glibc >= {floor[0]}.{floor[1]}; no musl asset exists')
        return f'hermes-teleoperator-receiver-{arch}-unknown-linux-gnu'
    raise DeployError('Unsupported shell CPU/word size/endianness; no architecture downgrade is attempted')


# NUL framing preserves spaces, quotes and newlines without evaluating remote output.
PROBE = r'''
set -eu
emit() { printf '%s\000%s\000' "$1" "$2"; }
emit os "$(uname -s)"
emit kernel "$(uname -r)"
emit arch "$(uname -m)"
emit uid "$(id -u)"
emit user "$(id -un)"
emit home "${HOME:?HOME must identify intended SSH user}"
emit hostname "$(hostname)"
emit sudo "${SUDO_USER:-}"
case $(uname -s) in
 Linux)
   emit elf "$(od -An -tu1 -N52 /proc/$$/exe)"
   emit cpu "$(awk 'BEGIN {n=0} /^$/ {exit} {if (++n<=100) print}' /proc/cpuinfo)"
   emit armv7 "$(awk -F: '/CPU architecture/ {gsub(/ /,"",$2); if ($2+0>=7) print 1; exit}' /proc/cpuinfo)"
   emit glibc "$(getconf GNU_LIBC_VERSION 2>/dev/null || :)"
   ;;
 Darwin)
   emit macos "$(sw_vers -productVersion)"
   emit arm64 "$(sysctl -n hw.optional.arm64 2>/dev/null || printf 0)"
   emit translated "$(sysctl -n sysctl.proc_translated 2>/dev/null || printf 0)"
   ;;
esac
'''


def shell_argv(target, script):
    """Reuse transport SSH policy but send an explicitly quoted POSIX command."""
    allowed = {k: v for k, v in target.items()
               if k in ('host', 'port', 'identity_file', 'known_hosts')}
    argv = ssh_argv({**allowed, 'executable': 'sh'})
    argv[-1] = 'sh -c ' + shlex.quote(script)
    return argv


def _run(target, script, *, data=None, timeout=30):
    try:
        result = subprocess.run(shell_argv(target, script), input=data,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeployError('SSH timed out; outcome may be unknown. No mutation retry; inspect target.') from exc
    if result.returncode:
        detail = result.stderr[-4096:].decode(errors='replace').strip()
        raise DeployError(f'SSH command failed ({result.returncode}); not retried: {detail}')
    if len(result.stdout) > 65536:
        raise DeployError('Oversized SSH response')
    return result.stdout


def parse_probe(data):
    try:
        fields = data.decode('utf-8').split('\0')
        if fields[-1] != '' or len(fields) % 2 != 1:
            raise ValueError('invalid framing')
        pairs = list(zip(fields[0:-1:2], fields[1:-1:2]))
        if len(dict(pairs)) != len(pairs):
            raise ValueError('duplicate fields')
        return dict(pairs)
    except (UnicodeError, ValueError) as exc:
        raise DeployError('Malformed probe (admin SSH must permit a normal shell, not a forced receiver)') from exc


def _path(path):
    if (not isinstance(path, str) or not path.startswith('/') or path == '/' or
            any(c in path for c in '\r\n\0') or
            any(p in ('.', '..', '') for p in path[1:].split('/'))):
        raise DeployError('Paths must be absolute, normalized, non-root, and contain no CR/LF/NUL')
    return path


def validate_probe(probe, *, allow_root=False):
    if probe.get('sudo'):
        raise DeployError('Do not run via sudo; log in as the intended SSH user')
    if not re.fullmatch(r'\d+', probe.get('uid', '')) or not probe.get('user') or not probe.get('hostname'):
        raise DeployError('Cannot establish remote SSH identity')
    if probe['uid'] == '0' and not allow_root:
        raise DeployError('Root deployment requires --allow-root')
    _path(probe.get('home'))


def validate_ping(ping, probe, config_home):
    expected = {'version': VERSION, 'protocol': 2, 'runtime': 'standalone-rust',
                'home': probe['home'], 'hostname': probe['hostname'],
                'platform': 'linux' if probe['os'] == 'Linux' else 'macos',
                'teleoperator_home': config_home}
    if not isinstance(ping, dict) or any(ping.get(k) != v for k, v in expected.items()):
        raise DeployError(f'Receiver handshake version/protocol/identity mismatch; expected {expected}, got {ping}')


def _ping(target, executable, probe, config_home):
    clean = {k: v for k, v in target.items()
             if k in ('host', 'port', 'identity_file', 'known_hosts')}
    with RpcProcess(ssh_argv({**clean, 'executable': executable,
                              'teleoperator_home': config_home}), timeout=20) as rpc:
        ping = rpc.call('ping')
    validate_ping(ping, probe, config_home)
    return ping


# Shipped with the wheel as Python source; no network scripts or remote runtimes.
# Same path safety model as install.sh, using platform-native stat on Darwin.
SAFE_SH = r'''
set -eu
umask 077
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ -z "${SUDO_USER:-}" ] || fail 'Do not run via sudo'
[ "$(id -u)" = "$uid" ] && [ "$HOME" = "$home" ] || fail 'SSH identity changed'
case $(uname -s) in
 Linux) metadata() { stat -c '%u %a' "$1"; } ;;
 Darwin) metadata() { stat -f '%u %Lp' "$1"; } ;;
 *) fail 'Unsupported OS' ;;
esac
secure_path() {
    current=$1; leaf=$1
    while :; do
        [ ! -L "$current" ] || fail "Refusing symlink: $current"
        if [ -e "$current" ]; then
            set -- $(metadata "$current")
            owner=$1; mode=$2; bits=$((0$mode))
            [ "$owner" = "$uid" ] || { [ "$current" != "$leaf" ] && [ "$owner" = 0 ]; } || fail "Unsafe owner: $current"
            if [ $((bits & 18)) != 0 ]; then
                [ "$current" != "$leaf" ] && [ "$owner" = 0 ] && [ -d "$current" ] && [ $((bits & 512)) != 0 ] || fail "Writable path: $current"
            fi
        fi
        [ "$current" != / ] || break
        current=$(dirname "$current")
    done
}
secure_path "$base"
secure_path "$launcher"
secure_path "$config"
[ ! -e "$launcher" ] || [ -f "$launcher" ] || fail 'Launcher is not a regular file'
'''


def _bindings(**values):
    return ''.join(k + '=' + shlex.quote(str(v)) + '\n' for k, v in values.items())


def deploy(target, *, allow_root=False, version=None, teleoperator_home=None,
           public_key=None, probe_only=False):
    """Deploy independently of controller sessions; no automatic mutation retries."""
    validate_version(version)
    probe = parse_probe(_run(target, PROBE))
    validate_probe(probe, allow_root=allow_root)
    asset = select_asset(probe)
    home = probe['home']
    base = home + '/.local/lib/hermes-teleoperator'
    launcher = home + '/.local/bin/hermes-teleoperator-receiver'
    config = _path(teleoperator_home or home + '/.config/hermes-teleoperator')
    if public_key is not None:
        public_key = validate_public_key(public_key)
        if not re.fullmatch(r'[A-Za-z0-9/._+-]+', launcher):
            raise DeployError('Cannot safely generate forced-command key entry: launcher path needs shell quoting')
    result = {'asset': asset, 'version': VERSION, 'identity': probe,
              'executable': launcher, 'teleoperator_home': config}
    if probe_only:
        return result
    bindings = _bindings(uid=probe['uid'], home=home, base=base, launcher=launcher, config=config)
    stage = None
    with tempfile.TemporaryDirectory(prefix='teleoperator-deploy-') as directory:
        binary, digest = download_release(asset, Path(directory))
        try:
            raw = _run(target, bindings + SAFE_SH + r'''
mkdir -p "$base"
chmod 700 "$base"
mktemp -d "$base/.deploy.XXXXXXXX"
''')
            stage = raw.decode().rstrip('\n')
            if not re.fullmatch(re.escape(base) + r'/\.deploy\.[A-Za-z0-9]{8,}', stage):
                stage = None
                raise DeployError('Malformed private remote staging path; inspect target')
            context = bindings + _bindings(stage=stage, digest=digest)
            _run(target, context + SAFE_SH + r'''
secure_path "$stage"
trap 'rm -rf "$stage"' HUP INT TERM
cat > "$stage/receiver"
case $(uname -s) in
 Linux) actual=$(sha256sum < "$stage/receiver") ;;
 Darwin) actual=$(shasum -a 256 < "$stage/receiver") ;;
esac
[ "${actual%% *}" = "$digest" ] || fail 'Transferred binary checksum mismatch'
chmod 700 "$stage/receiver"
''', data=binary.read_bytes(), timeout=120)
            # Execute only verified bytes; runtime, exact version and SSH identity
            # are checked before changing any live executable or existing state.
            _ping(target, stage + '/receiver', probe, stage + '/verification-state')
            installed_binary = base + '/receiver-' + VERSION + '-' + digest
            launcher_text = ('#!/bin/sh\nexport TELEOPERATOR_HOME=' + shlex.quote(config) +
                             '\nexec ' + shlex.quote(installed_binary) + ' "$@"\n')
            try:
                _run(target, context + _bindings(installed=installed_binary, content=launcher_text) + SAFE_SH + r'''
# DEPLOY_COMMIT: only reached after successful transfer, hash and handshake.
secure_path "$installed"
[ ! -e "$installed" ] || [ -f "$installed" ] || fail 'Installed binary is not regular'
mkdir -p "$(dirname "$launcher")" "$config"
# Stage the launcher on its own filesystem; mv remains atomic even if .local/bin
# and .local/lib are separate mounts. Never truncate the live launcher.
launcher_tmp=$(mktemp "$(dirname "$launcher")/.receiver-launcher.XXXXXXXX")
trap 'rm -f "$launcher_tmp"' EXIT
printf '%s' "$content" > "$launcher_tmp"
chmod 700 "$launcher_tmp"
# Content-addressed binary first, then one atomic live launcher switch.
mv -f "$stage/receiver" "$installed"
mv -f "$launcher_tmp" "$launcher"
''')
            except Exception as exc:
                raise DeploymentUnverified(
                    f'Launcher commit outcome unknown at {launcher}; it may have changed. '
                    f'Do not retry; inspect the target first. Cause: {exc}'
                ) from exc
            try:
                result['handshake'] = _ping(target, launcher, probe, config)
            except Exception as exc:
                raise DeploymentUnverified(
                    f'Launcher switch committed at {launcher}, but live handshake is unverified. '
                    f'Do not retry; inspect the target first. Cause: {exc}'
                ) from exc
            if public_key:
                # The administrator must explicitly install this dedicated restricted key.
                # Deployment never inspects or changes remote SSH authorization state.
                result['ssh_authorization_entry'] = ('restrict,command="' + launcher + '" '
                                                     + public_key + ' hermes-teleoperator')
        finally:
            if stage:
                try:
                    _run(target, 'rm -rf ' + shlex.quote(stage), timeout=20)
                except Exception as exc:
                    print(f'Warning: cleanup failed for {stage}: {exc}', file=sys.stderr)
    return result


def validate_public_key(text):
    """Accept only an SSH ed25519 public blob, stripping the untrusted comment."""
    line = text.rstrip('\n')
    if len(text) > 4096 or '\n' in line or '\r' in line:
        raise DeployError('Expected one ed25519 public key, never a private key')
    match = re.fullmatch(r'ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\r\n]*)?', line)
    if not match:
        raise DeployError('Expected one ed25519 public key, never a private key')
    try:
        blob = base64.b64decode(match[1], validate=True)
    except ValueError as exc:
        raise DeployError('Invalid public key encoding') from exc
    if blob[:19] != b'\0\0\0\x0bssh-ed25519\0\0\0\x20' or len(blob) != 51:
        raise DeployError('Invalid ed25519 public key blob')
    return 'ssh-ed25519 ' + match[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Deploy an exact-version standalone receiver via administrative SSH. No remote downloads.')
    parser.add_argument('host', help='SSH destination or config alias with normal administrative shell access')
    parser.add_argument('--identity-file', help='Administrative SSH private key path (local only)')
    parser.add_argument('--known-hosts', help='Existing pinned known_hosts file')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--version', help='Must equal v' + VERSION)
    parser.add_argument('--allow-root', action='store_true')
    parser.add_argument('--teleoperator-home', help='Absolute receiver state directory; existing state is preserved')
    parser.add_argument('--public-key-file', help='Optional dedicated gateway ED25519 PUBLIC key; prints ssh_authorization_entry for manual administrator installation, never enrolls it')
    parser.add_argument('--probe', action='store_true', help='Read-only detection and package selection; no downloads or writes')
    args = parser.parse_args(argv)
    try:
        key = None
        if args.public_key_file:
            with open(args.public_key_file, encoding='ascii') as stream:
                key = validate_public_key(stream.read(4097))
        target = {'host': args.host, 'port': args.port,
                  'identity_file': args.identity_file, 'known_hosts': args.known_hosts}
        result = deploy(target, allow_root=args.allow_root, version=args.version,
                        teleoperator_home=args.teleoperator_home, public_key=key, probe_only=args.probe)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except DeploymentUnverified as exc:
        print(f'Deployment requires inspection: {exc}', file=sys.stderr)
        return 3
    except (DeployError, OSError, ValueError, RuntimeError) as exc:
        print(f'Deployment failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
