"""Gateway deployment: detection is evidence-based, never a uname-only guess."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import io
import shutil
import socket
import time
from contextlib import redirect_stdout
from unittest.mock import patch
import unittest
from hermes_teleoperator import deploy


def linux(machine=62, bits=64, endian='little', flags=0, **extra):
    header = bytearray(52)
    header[:6] = bytes([127, 69, 76, 70, 2 if bits == 64 else 1,
                        1 if endian == 'little' else 2])
    header[18:20] = machine.to_bytes(2, endian)
    offset = 48 if bits == 64 else 36
    header[offset:offset + 4] = flags.to_bytes(4, endian)
    return {'os': 'Linux', 'kernel': '6.1.0', 'elf': ' '.join(map(str, header)),
            'cpu': 'flags : sse2\nisa : rv64imafdc', 'glibc': '', **extra}


class SelectionTests(unittest.TestCase):
    def test_all_workflow_assets_are_defined(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
        for target in deploy.TARGETS:
            self.assertIn(target, workflow)
        self.assertEqual(len(deploy.ASSETS), 13)

    def test_static_x86_64_selected_from_shell_elf(self):
        self.assertEqual(deploy.select_asset(linux()),
                         'hermes-teleoperator-receiver-x86_64-unknown-linux-musl')

    def test_32bit_userland_and_other_static_targets(self):
        for probe, target in [(linux(3, 32), 'i686'), (linux(183), 'aarch64'),
                              (linux(40, 32, flags=0x5000400, armv7='1'), 'armv7'),
                              (linux(243, flags=5), 'riscv64gc')]:
            with self.subTest(target=target):
                suffix = 'musleabihf' if target == 'armv7' else 'musl'
                self.assertEqual(deploy.select_asset(probe),
                                 f'hermes-teleoperator-receiver-{target}-unknown-linux-{suffix}')

    def test_architecture_specific_kernel_floors_and_underscored_riscv_isa(self):
        for probe in [linux(183, kernel='3.10.0'), linux(243, flags=5, kernel='4.15.0')]:
            with self.assertRaisesRegex(deploy.DeployError, 'kernel'):
                deploy.select_asset(probe)
        self.assertIn('riscv64gc', deploy.select_asset(linux(243, flags=5,
                      cpu='isa : rv64i_m_a_f_d_c_zicsr_zifencei')))

    def test_dynamic_only_platforms_require_proven_glibc_floor(self):
        for machine, endian, arch, floor in [(21, 'little', 'powerpc64le', '2.19'),
                                             (22, 'big', 's390x', '2.17')]:
            with self.subTest(arch=arch):
                self.assertEqual(deploy.select_asset(linux(machine, endian=endian,
                                                          glibc='glibc ' + floor)),
                                 f'hermes-teleoperator-receiver-{arch}-unknown-linux-gnu')
                for libc in ['', 'musl 1.2', 'glibc 2.16']:
                    with self.assertRaises(deploy.DeployError):
                        deploy.select_asset(linux(machine, endian=endian, glibc=libc))

    def test_unknown_softfloat_big_endian_old_kernel_and_weak_cpu_rejected(self):
        cases = [linux(8, 32), linux(40, 32, flags=0x5000200, armv7='1'),
                 linux(40, 32, flags=0x5000400), linux(183, endian='big'),
                 linux(62, bits=32), linux(21, endian='big'),
                 linux(kernel='2.6.32'), linux(3, 32, cpu=''),
                 linux(243, flags=1), linux(243, flags=5, cpu=''),
                 linux(243, flags=5, cpu='isa : rv64imafd_zicsr_zifencei')]
        for probe in cases:
            with self.subTest(probe=probe), self.assertRaises(deploy.DeployError):
                deploy.select_asset(probe)

    def test_rosetta_prefers_native_arm64_and_macos_minimum(self):
        probe = {'os': 'Darwin', 'arch': 'x86_64', 'arm64': '1',
                 'translated': '1', 'macos': '14.0'}
        self.assertEqual(deploy.select_asset(probe),
                         'hermes-teleoperator-receiver-aarch64-apple-darwin')
        probe.update(arm64='0', translated='0', macos='10.15.7')
        self.assertEqual(deploy.select_asset(probe),
                         'hermes-teleoperator-receiver-x86_64-apple-darwin')
        for changes in [dict(macos='10.11'), dict(arch='ppc'),
                        dict(translated='1'), dict(os='Windows')]:
            with self.subTest(changes=changes), self.assertRaises(deploy.DeployError):
                deploy.select_asset({**probe, **changes})


class DownloadTests(unittest.TestCase):
    def test_exact_tag_download_and_unambiguous_manifest(self):
        payload = b'actual release bytes'
        digest = hashlib.sha256(payload).hexdigest()
        asset = deploy.select_asset(linux())
        seen = []
        def fetch(url, destination, limit):
            seen.append(url)
            destination.write_bytes((digest + '  ' + asset + '\n').encode()
                                    if url.endswith('SHA256SUMS') else payload)
        with tempfile.TemporaryDirectory() as tmp:
            path, checksum = deploy.download_release(asset, Path(tmp), fetch=fetch)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(checksum, digest)
        base = f'https://github.com/Tsurgcom/hermes-teleoperator/releases/download/v{deploy.VERSION}'
        self.assertEqual(seen, [base + '/SHA256SUMS', base + '/' + asset])

    def test_malformed_duplicate_missing_and_mismatched_checksums_rejected(self):
        asset = deploy.select_asset(linux())
        digest = hashlib.sha256(b'good').hexdigest()
        for manifest, payload in [(f'{digest}  {asset}\n' * 2, b'good'),
                                  (f'nothex  {asset}\n', b'good'),
                                  (f'{digest}  ../{asset}\n', b'good'),
                                  ('', b'good'), (f'{digest}  {asset}\n', b'bad')]:
            def fetch(url, destination, limit):
                destination.write_bytes(manifest.encode() if url.endswith('SHA256SUMS') else payload)
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(deploy.DeployError):
                    deploy.download_release(asset, Path(tmp), fetch=fetch)

    def test_requested_version_cannot_diverge(self):
        for version in ['latest', 'v1.0.0', '1.1.2', 'v' + deploy.VERSION + '/x']:
            with self.subTest(version=version), self.assertRaises(deploy.DeployError):
                deploy.validate_version(version)
        self.assertEqual(deploy.validate_version('v' + deploy.VERSION), 'v' + deploy.VERSION)


class ShellBoundaryTests(unittest.TestCase):
    def test_probe_uses_real_shell_elf_and_strict_ssh(self):
        argv = deploy.shell_argv({'host': 'alice@example', 'identity_file': '/only/local/key'}, 'printf ok')
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('ForwardAgent=no', argv)
        self.assertIn('ClearAllForwardings=yes', argv)
        self.assertIn('sh -c', argv[-1])
        result = subprocess.run(['sh', '-c', deploy.PROBE], capture_output=True, check=True, timeout=10)
        probe = deploy.parse_probe(result.stdout)
        self.assertEqual(probe['home'], os.environ['HOME'])
        self.assertEqual(deploy.select_asset(probe), deploy.select_asset(linux()))

    def test_root_sudo_and_identity_rejected_before_download(self):
        probe = {**linux(), 'uid': '0', 'user': 'root', 'home': '/root',
                 'hostname': 'host', 'sudo': ''}
        for changes, allow in [({}, False), ({'sudo': 'alice'}, True),
                               ({'home': '/root/../other'}, True)]:
            with self.subTest(changes=changes), self.assertRaises(deploy.DeployError):
                deploy.validate_probe({**probe, **changes}, allow_root=allow)
        deploy.validate_probe(probe, allow_root=True)

    def test_handshake_requires_exact_version_protocol_and_identity(self):
        probe = {'home': '/home/alice', 'hostname': 'host', 'os': 'Linux'}
        ping = {'home': '/home/alice', 'hostname': 'host', 'platform': 'linux',
                'runtime': 'standalone-rust', 'protocol': 2, 'version': deploy.VERSION,
                'teleoperator_home': '/private/state'}
        deploy.validate_ping(ping, probe, '/private/state')
        for changes in [dict(version='1.0.0'), dict(version=None), dict(protocol=1),
                        dict(hostname='other'), dict(home='/root'), dict(platform='macos'),
                        dict(teleoperator_home='/live/state'), dict(runtime='python')]:
            with self.subTest(changes=changes), self.assertRaises(deploy.DeployError):
                deploy.validate_ping({**ping, **changes}, probe, '/private/state')


class DeploymentFlowTests(unittest.TestCase):
    def test_gateway_rejects_corrupt_release_before_remote_staging(self):
        probe = {**linux(), 'uid': '1000', 'user': 'alice', 'home': '/home/alice',
                 'hostname': 'host', 'sudo': ''}
        data = b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in probe.items())
        with patch.object(deploy, '_run', return_value=data) as run, \
             patch.object(deploy, 'download_release', side_effect=deploy.DeployError('bad checksum')):
            with self.assertRaisesRegex(deploy.DeployError, 'bad checksum'):
                deploy.deploy({'host': 'alice@host'})
        self.assertEqual(run.call_count, 1)

    def test_gateway_download_completes_before_any_transfer_and_commit(self):
        probe = {**linux(), 'uid': '1000', 'user': 'alice', 'home': '/home/alice',
                 'hostname': 'host', 'sudo': ''}
        events = []
        def run(target, script, **kwargs):
            if script == deploy.PROBE:
                return b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in probe.items())
            if 'mktemp -d' in script:
                events.append('stage')
                return b'/home/alice/.local/lib/hermes-teleoperator/.deploy.abcdefgh\n'
            if kwargs.get('data') is not None:
                events.append('transfer')
            elif 'DEPLOY_COMMIT' in script:
                events.append('commit')
            else:
                events.append('cleanup')
            return b''
        def download(asset, directory):
            events.append('download')
            binary = directory / 'receiver'
            binary.write_bytes(b'binary')
            return binary, hashlib.sha256(b'binary').hexdigest()
        def ping(target, executable, identity, state):
            events.append('staged-ping' if '.deploy.' in executable else 'installed-ping')
            return {'version': deploy.VERSION}
        with patch.object(deploy, '_run', side_effect=run), \
             patch.object(deploy, 'download_release', side_effect=download), \
             patch.object(deploy, '_ping', side_effect=ping):
            result = deploy.deploy({'host': 'alice@host'})
        self.assertEqual(events, ['download', 'stage', 'transfer', 'staged-ping',
                                  'commit', 'installed-ping', 'cleanup'])
        self.assertEqual(result['asset'], deploy.select_asset(probe))

    def test_transfer_failure_never_commits_and_cleans_stage(self):
        probe = {**linux(), 'uid': '1000', 'user': 'alice', 'home': '/home/alice',
                 'hostname': 'host', 'sudo': ''}
        scripts = []
        def run(target, script, **kwargs):
            scripts.append(script)
            if script == deploy.PROBE:
                return b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in probe.items())
            if 'mktemp -d' in script:
                return b'/home/alice/.local/lib/hermes-teleoperator/.deploy.abcdefgh\n'
            if kwargs.get('data') is not None:
                raise deploy.DeployError('transfer failed')
            return b''
        def download(asset, directory):
            binary = directory / 'receiver'
            binary.write_bytes(b'binary')
            return binary, hashlib.sha256(b'binary').hexdigest()
        with patch.object(deploy, '_run', side_effect=run), \
             patch.object(deploy, 'download_release', side_effect=download):
            with self.assertRaisesRegex(deploy.DeployError, 'transfer failed'):
                deploy.deploy({'host': 'alice@host'})
        self.assertFalse(any('DEPLOY_COMMIT' in s for s in scripts))
        self.assertIn('rm -rf', scripts[-1])


class EnrollmentAndCLITests(unittest.TestCase):
    def test_public_key_validated_locally_never_private_key(self):
        import shutil
        if not shutil.which('ssh-keygen'):
            self.skipTest('ssh-keygen unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                           check=True, timeout=10)
            public = key.with_suffix('.pub').read_text()
            self.assertEqual(deploy.validate_public_key(public), ' '.join(public.split()[:2]))
            for bad in [key.read_text(), public + public, 'ssh-ed25519 AAAA',
                        'restrict ' + public, 'ssh-rsa AAAA']:
                with self.subTest(bad=bad[:30]), self.assertRaises(deploy.DeployError):
                    deploy.validate_public_key(bad)

    def test_forced_command_path_injection_is_rejected_before_any_mutation(self):
        probe = {**linux(), 'uid': '1000', 'user': 'alice',
                 'home': '/home/alice;touch injected', 'hostname': 'host', 'sudo': ''}
        data = b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in probe.items())
        import base64
        public = 'ssh-ed25519 ' + base64.b64encode(
            b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'k' * 32).decode()
        with patch.object(deploy, '_run', return_value=data) as run, \
             patch.object(deploy, 'download_release') as download:
            with self.assertRaisesRegex(deploy.DeployError, 'shell quoting'):
                deploy.deploy({'host': 'alice@host'}, public_key=public)
        self.assertEqual(run.call_count, 1)
        download.assert_not_called()

    def test_cli_probe_has_no_download_or_mutation(self):
        probe = {**linux(), 'uid': '1000', 'user': 'alice', 'home': '/home/alice',
                 'hostname': 'host', 'sudo': ''}
        data = b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in probe.items())
        with patch.object(deploy, '_run', return_value=data) as run, \
             patch.object(deploy, 'download_release') as download, redirect_stdout(io.StringIO()):
            self.assertEqual(deploy.main(['alice@host', '--probe']), 0)
        self.assertEqual(run.call_count, 1)
        download.assert_not_called()


class OfflineExecutionTests(unittest.TestCase):
    def test_deploy_returns_manual_entry_without_touching_ssh_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            home.mkdir(mode=0o700)
            key = Path(tmp) / 'dedicated'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            public = key.with_suffix('.pub').read_text()
            env = {**os.environ, 'HOME': str(home)}
            env.pop('SUDO_USER', None)
            def local_run(target, script, **kwargs):
                self.assertNotIn('.ssh', script)
                self.assertNotIn('authorized_keys', script)
                result = subprocess.run(['sh', '-c', script], input=kwargs.get('data'),
                                        capture_output=True, env=env, timeout=10)
                if result.returncode:
                    raise deploy.DeployError(result.stderr.decode())
                return result.stdout
            def download(asset, directory):
                binary = directory / 'receiver'
                binary.write_bytes(b'verified test payload')
                return binary, hashlib.sha256(binary.read_bytes()).hexdigest()
            def snapshot():
                tree = home / '.ssh'
                if not tree.exists():
                    return None
                result = {}
                for path in [tree, *sorted(tree.rglob('*'))]:
                    st = path.lstat()
                    content = (os.readlink(path) if path.is_symlink() else
                               path.read_bytes() if path.is_file() else None)
                    result[str(path.relative_to(home))] = (
                        st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_nlink,
                        st.st_size, st.st_mtime_ns, st.st_ctime_ns, content)
                return result
            ssh = home / '.ssh'
            ssh.mkdir(mode=0o750)
            (ssh / 'authorized_keys').write_bytes(b'# existing unterminated key')
            (ssh / 'authorized_keys').chmod(0o400)
            (ssh / 'config').write_bytes(b'Host untouched')
            (ssh / 'nested').mkdir()
            (ssh / 'nested/key').write_bytes(b'private sentinel')
            (ssh / 'link').symlink_to('nested/key')
            config = home / '.config/hermes-teleoperator/config.json'
            config.parent.mkdir(parents=True)
            config.write_bytes(b'{"preserved":true}')
            with patch.object(deploy, '_run', side_effect=local_run), \
                 patch.object(deploy, 'download_release', side_effect=download), \
                 patch.object(deploy, '_ping', return_value={}):
                for state in ('populated', 'fifo', 'absent'):
                    with self.subTest(state=state):
                        if state == 'fifo':
                            (ssh / 'authorized_keys').unlink()
                            os.mkfifo(ssh / 'authorized_keys')
                        elif state == 'absent':
                            shutil.rmtree(ssh)
                        before = snapshot()
                        result = deploy.deploy({'host': 'unused'}, allow_root=True, public_key=public)
                        self.assertEqual(snapshot(), before)
                        self.assertEqual(config.read_bytes(), b'{"preserved":true}')
                        self.assertEqual(result['ssh_authorization_entry'],
                                         'restrict,command="' + result['executable'] + '" '
                                         + ' '.join(public.split()[:2]) + ' hermes-teleoperator')

    def test_unsafe_paths_and_corrupt_transfer_preserve_live_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            home.mkdir(mode=0o700)
            launcher = home / '.local/bin/hermes-teleoperator-receiver'
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b'EXISTING LIVE RECEIVER')
            env = {**os.environ, 'HOME': str(home)}
            env.pop('SUDO_USER', None)
            def local_run(target, script, **kwargs):
                data = kwargs.get('data')
                if data:
                    data = b'corrupt over wire'
                result = subprocess.run(['sh', '-c', script], input=data, env=env,
                                        capture_output=True, timeout=30)
                if result.returncode:
                    raise deploy.DeployError(result.stderr.decode())
                return result.stdout
            def download(asset, directory):
                binary = directory / 'receiver'
                binary.write_bytes(b'VALID CHECKSUM')
                return binary, hashlib.sha256(binary.read_bytes()).hexdigest()
            with patch.object(deploy, '_run', side_effect=local_run), \
                 patch.object(deploy, 'download_release', side_effect=download):
                with self.assertRaisesRegex(deploy.DeployError, 'checksum mismatch'):
                    deploy.deploy({'host': 'unused'}, allow_root=True)
                self.assertEqual(launcher.read_bytes(), b'EXISTING LIVE RECEIVER')
                home.chmod(0o777)
                with self.assertRaisesRegex(deploy.DeployError, 'Writable path'):
                    deploy.deploy({'host': 'unused'}, allow_root=True)
                home.chmod(0o700)
                launcher.unlink()
                launcher.symlink_to('/dev/null')
                with self.assertRaisesRegex(deploy.DeployError, 'symlink'):
                    deploy.deploy({'host': 'unused'}, allow_root=True)

    def test_real_shell_offline_install_preserves_state_and_rejects_bad_version(self):
        root = Path(__file__).resolve().parents[1]
        receiver = Path(os.environ.get('TELEOPERATOR_RECEIVER',
                         root / 'receiver/target/release/hermes-teleoperator-receiver'))
        if not receiver.is_file():
            self.skipTest('Build receiver --release for offline execution integration')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            home.mkdir(mode=0o700)
            config = home / '.config/hermes-teleoperator'
            config.mkdir(parents=True)
            (config / 'config.json').write_text('{"preserved":true}')
            env = {**os.environ, 'HOME': str(home)}
            env.pop('SUDO_USER', None)
            # Fake only the SSH boundary; all probe, staging, checksums, installer,
            # real receiver protocol and persistence run as actual OS processes.
            def local_run(target, script, **kwargs):
                result = subprocess.run(['sh', '-c', script], input=kwargs.get('data'),
                                        capture_output=True, env=env, timeout=30)
                if result.returncode:
                    raise deploy.DeployError(result.stderr.decode())
                return result.stdout
            def local_ping(target, executable, probe, state):
                with deploy.RpcProcess(['env', 'HOME=' + str(home),
                                        'TELEOPERATOR_HOME=' + state, executable],
                                       timeout=10) as rpc:
                    ping = rpc.call('ping')
                deploy.validate_ping(ping, probe, state)
                return ping
            payload = receiver.read_bytes()
            asset = deploy.select_asset(linux())
            digest = hashlib.sha256(payload).hexdigest()
            def fetch(url, destination, limit):
                destination.write_bytes((digest + '  ' + asset + '\n').encode()
                                        if url.endswith('SHA256SUMS') else payload)
            with patch.object(deploy, '_run', side_effect=local_run), \
                 patch.object(deploy, '_ping', side_effect=local_ping), \
                 patch.object(deploy, '_fetch', side_effect=fetch):
                result = deploy.deploy({'host': 'unused'}, allow_root=True)
                self.assertEqual(result['handshake']['version'], deploy.VERSION)
                launcher = Path(result['executable'])
                old = launcher.read_bytes()
                result = deploy.deploy({'host': 'unused'}, allow_root=True)
                self.assertEqual(launcher.read_bytes(), old)
                self.assertEqual((config / 'config.json').read_text(), '{"preserved":true}')
                self.assertEqual(list((home / '.local/lib/hermes-teleoperator').glob('.deploy.*')), [])
                with patch.object(deploy, 'VERSION', '0.0.0'):
                    with self.assertRaisesRegex(deploy.DeployError, 'mismatch'):
                        deploy.deploy({'host': 'unused'}, allow_root=True)
                self.assertEqual(launcher.read_bytes(), old)


@unittest.skipUnless(os.geteuid() == 0 and shutil.which('sshd') and shutil.which('ssh-keygen'),
                     'isolated real SSH integration requires root and OpenSSH server')
class LoopbackDeploymentTests(unittest.TestCase):
    def test_real_ssh_deploy_upgrade_forced_key_and_wrong_host_pin(self):
        root = Path(__file__).resolve().parents[1]
        receiver = Path(os.environ.get('TELEOPERATOR_RECEIVER',
                         root / 'receiver/target/release/hermes-teleoperator-receiver'))
        if not receiver.is_file():
            self.skipTest('Build receiver --release')
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            home = d / 'home'
            (home / '.ssh').mkdir(parents=True, mode=0o700)
            for name in ('host', 'admin', 'dedicated'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(d / name)],
                               check=True, timeout=10)
            wrapper = d / 'admin-shell'
            wrapper.write_text('#!/bin/sh\nexport HOME=' + str(home) +
                               '\nexec sh -c "$SSH_ORIGINAL_COMMAND"\n')
            wrapper.chmod(0o700)
            auth = home / '.ssh/authorized_keys'
            original_admin = 'command="' + str(wrapper) + '" ' + (d / 'admin.pub').read_text()
            auth.write_text(original_admin)
            auth.chmod(0o640)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            conf = d / 'sshd_config'
            conf.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {d / 'host'}
PidFile {d / 'pid'}
AuthorizedKeysFile {auth}
StrictModes no
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AllowUsers root
AllowTcpForwarding no
X11Forwarding no
''')
            pub = (d / 'host.pub').read_text().split()
            known = d / 'known_hosts'
            known.write_text(f'[127.0.0.1]:{port} {pub[0]} {pub[1]}\n')
            server = subprocess.Popen([shutil.which('sshd'), '-D', '-e', '-f', str(conf)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if server.poll() is not None:
                        self.fail(server.stderr.read().decode())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            break
                    except OSError:
                        if time.monotonic() > deadline:
                            self.fail('sshd not ready')
                        time.sleep(.02)
                target = {'host': 'root@127.0.0.1', 'port': port,
                          'known_hosts': str(known), 'identity_file': str(d / 'admin')}
                payload = receiver.read_bytes()
                asset = deploy.select_asset(linux())
                digest = hashlib.sha256(payload).hexdigest()
                urls = []
                public_fetch = deploy._fetch
                def fetch(url, destination, limit):
                    urls.append(url)
                    # Opt-in release smoke: exercise actual pinned public downloads
                    # through the same real SSH fixture, never synthetic API results.
                    if os.environ.get('TELEOPERATOR_DEPLOY_PUBLIC_RELEASE') == deploy.VERSION:
                        return public_fetch(url, destination, limit)
                    destination.write_bytes((digest + '  ' + asset + '\n').encode()
                                            if url.endswith('SHA256SUMS') else payload)
                with patch.object(deploy, '_fetch', side_effect=fetch):
                    result = deploy.deploy(target, allow_root=True,
                                           public_key=(d / 'dedicated.pub').read_text())
                    self.assertEqual(result['handshake']['version'], deploy.VERSION)
                    old = Path(result['executable']).read_bytes()
                    result = deploy.deploy(target, allow_root=True,
                                           public_key=(d / 'dedicated.pub').read_text())
                    self.assertEqual(Path(result['executable']).read_bytes(), old)
                    self.assertEqual(auth.read_text(), original_admin)
                    self.assertEqual(auth.stat().st_mode & 0o777, 0o640)
                    self.assertEqual(sorted(p.name for p in auth.parent.iterdir()), ['authorized_keys'])
                    # Explicit test-administrator enrollment, never deployment behavior.
                    auth.write_text(original_admin + result['ssh_authorization_entry'] + '\n')
                    forced = {**target, 'identity_file': str(d / 'dedicated'), 'executable': '/bin/false'}
                    with deploy.RpcProcess(deploy.ssh_argv(forced), timeout=10) as rpc:
                        ping = rpc.call('ping')
                    self.assertEqual(ping['version'], deploy.VERSION)
                    self.assertEqual(ping['teleoperator_home'], result['teleoperator_home'])
                    bad = d / 'bad_known'
                    bad.write_text(f'[127.0.0.1]:{port} ' + (d / 'admin.pub').read_text())
                    with self.assertRaisesRegex(deploy.DeployError, 'Host key verification failed'):
                        deploy.deploy({**target, 'known_hosts': str(bad)}, allow_root=True)
                self.assertEqual(len(urls), 4)
                self.assertEqual(list((home / '.local/lib/hermes-teleoperator').glob('.deploy.*')), [])
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                server.stderr.close()


if __name__ == '__main__':
    unittest.main()
