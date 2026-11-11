"""The destination installer needs only POSIX utilities, not Python/Hermes."""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import re
import inspect
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StandaloneInstallTests(unittest.TestCase):
    @staticmethod
    def ssh_snapshot(home):
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

    def invoke(self, home, binary, *extra, env_extra=None):
        env = {k: v for k, v in {**os.environ, 'HOME': str(home)}.items()
               if k not in ('SUDO_USER', 'HERMES_HOME', 'PYTHONPATH', 'TELEOPERATOR_HOME')}
        env.update(env_extra or {})
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        return subprocess.run(['bash', str(ROOT / 'install.sh'), '--binary', str(binary),
            '--sha256', digest, '--allow-root', *extra], env=env, capture_output=True, text=True, timeout=30)

    def test_local_binary_install_is_independent_of_python_and_hermes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            home.mkdir()
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nprintf STANDALONE_OK\n')
            result = self.invoke(home, binary)
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = home / '.local/bin/hermes-teleoperator-receiver'
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'STANDALONE_OK')
            self.assertNotIn('python', launcher.read_text().lower())
            again = self.invoke(home, binary)
            self.assertEqual(again.returncode, 0, again.stderr)

    def test_failed_launcher_switch_preserves_legacy_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            base = home / '.local/lib/hermes-teleoperator'
            base.mkdir(parents=True)
            old = base / 'receiver'
            old.write_text('#!/bin/sh\nprintf OLD\n')
            old.chmod(0o700)
            launcher = home / '.local/bin/hermes-teleoperator-receiver'
            launcher.parent.mkdir(parents=True)
            launcher.write_text(f'#!/bin/sh\nexec "{old}" "$@"\n')
            launcher.chmod(0o700)
            original = launcher.read_bytes()
            binary = root / 'new-receiver'
            binary.write_text('#!/bin/sh\nprintf NEW\n')
            commands = root / 'commands'
            commands.mkdir()
            mv = commands / 'mv'
            mv.write_text('#!/bin/bash\nfor arg in "$@"; do\n'
                          '  case "$arg" in */.local/bin/hermes-teleoperator-receiver) '
                          'echo simulated-launcher-failure >&2; exit 1;; esac\n'
                          'done\nexec /usr/bin/mv "$@"\n')
            mv.chmod(0o700)
            result = self.invoke(home, binary, env_extra={'PATH': f'{commands}:/usr/bin:/bin'})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('simulated-launcher-failure', result.stderr)
            self.assertEqual(launcher.read_bytes(), original)
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'OLD')
            success = self.invoke(home, binary)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'NEW')
            upgraded_launcher = launcher.read_bytes()
            binary.write_text('#!/bin/sh\nprintf LATER\n')
            failed = self.invoke(home, binary, env_extra={'PATH': f'{commands}:/usr/bin:/bin'})
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('simulated-launcher-failure', failed.stderr)
            self.assertEqual(launcher.read_bytes(), upgraded_launcher)
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'NEW')

    def test_installer_has_no_ssh_state_access(self):
        self.assertNotIn('.ssh', (ROOT / 'install.sh').read_text())

    def test_public_key_validation_rejects_injection_without_changing_live_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            key = root / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            public = Path(str(key) + '.pub').read_text().strip()
            self.assertEqual(self.invoke(root, binary).returncode, 0)
            launcher = root / '.local/bin/hermes-teleoperator-receiver'
            before = launcher.read_bytes()
            for bad in (key.read_text(), public + '\n' + public, public + '\r',
                        'ssh-ed25519 AAAA', 'restrict ' + public, 'ssh-rsa AAAA'):
                with self.subTest(bad=bad[:30]):
                    result = self.invoke(root, binary, '--public-key', bad)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((root / '.ssh').exists())
                    self.assertEqual(launcher.read_bytes(), before)
            result = self.invoke(root, binary, '--public-key', public.split()[0] + ' '
                                 + public.split()[1] + ' "; touch /not-executed; #')
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = next(line for line in result.stdout.splitlines() if line.startswith('restrict,command='))
            self.assertNotIn('touch', entry)
            self.assertNotIn('/not-executed', entry)

    def test_empty_public_key_file_is_rejected_without_ssh_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            key = root / 'empty.pub'
            key.touch()
            result = self.invoke(root, binary, '--public-key-file', str(key))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('public key', result.stderr.lower())
            self.assertFalse((root / '.ssh').exists())
            self.assertFalse((root / '.local/bin/hermes-teleoperator-receiver').exists())

    def test_checksum_mismatch_does_not_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('payload')
            result = self.invoke(root, binary, '--sha256', '0' * 64)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('checksum', result.stderr.lower())
            self.assertFalse((root / '.local/bin/hermes-teleoperator-receiver').exists())

    def test_restricted_public_key_entry_is_printed_without_creating_ssh_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            key = root / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            for _ in range(2):
                result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((root / '.ssh').exists())
                entries = [line for line in result.stdout.splitlines() if line.startswith('restrict,command=')]
                self.assertEqual(entries, [f'restrict,command="\'{root}/.local/bin/hermes-teleoperator-receiver\'" '
                                           + ' '.join(Path(str(key) + '.pub').read_text().split()[:2])
                                           + ' hermes-teleoperator'])
                self.assertIn('manual', result.stdout.lower())

    def test_manual_entry_preserves_existing_ssh_tree_and_unrelated_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            key = root / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            (root / '.ssh').mkdir(mode=0o700)
            auth = root / '.ssh/authorized_keys'
            original = b'# existing unterminated entry'
            auth.write_bytes(original)
            auth.chmod(0o400)
            (auth.parent / 'config').write_text('Host preserved\n')
            (auth.parent / 'nested').mkdir()
            (auth.parent / 'nested/key').write_bytes(b'private sentinel')
            (auth.parent / 'link').symlink_to('nested/key')
            unrelated = root / '.config/hermes-teleoperator/config.json'
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b'{"preserved": true}')
            before = self.ssh_snapshot(root)
            result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.ssh_snapshot(root), before)
            self.assertEqual(auth.read_bytes(), original)
            self.assertEqual(unrelated.read_bytes(), b'{"preserved": true}')

    def test_parallel_installs_print_each_dedicated_entry_without_ssh_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            keys = []
            for index in range(6):
                key = root / f'key{index}'
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
                keys.append(Path(str(key) + '.pub'))
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(keys)) as pool:
                results = list(pool.map(lambda key: self.invoke(root, binary, '--public-key-file',
                                                               str(key)), keys))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / '.ssh').exists())
            for key, result in zip(keys, results):
                self.assertEqual(result.stdout.count(key.read_text().split()[1]), 1)

    def test_forced_command_survives_both_sshd_dequote_passes(self):
        """sshd strips one backslash layer for the config option, then the login shell parses
        what is left. A command that still works after BOTH passes is the only acceptable one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            home.mkdir()
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nprintf FORCED_OK\n')
            key = root / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            result = self.invoke(home, binary, '--public-key-file', str(key) + '.pub')
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = next(line for line in result.stdout.splitlines() if line.startswith('restrict,command='))
            match = re.match(r'restrict,command="((?:\\.|[^"\\])*)"', entry)
            assert match is not None, entry
            option = match.group(1)
            # No escapes at all: a safe-charset path is single-quoted and needs none, which is
            # the property that makes it survive sshd's two passes unchanged.
            self.assertNotIn('\\', option)
            command = re.sub(r'\\(.)', r'\1', option)   # pass 1: OpenSSH option dequoting
            self.assertEqual(subprocess.check_output(['/bin/sh', '-c', command], text=True),
                             'FORCED_OK')              # pass 2: the user's shell

    def test_metacharacter_home_installs_but_refuses_unsafe_manual_entry(self):
        """A path needing shell quoting cannot be forced-command quoted safely, so the installer
        must fail closed rather than print an entry that executes injected text."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "target 'quoted\" $HOME; user"
            home.mkdir()
            binary = Path(tmp) / 'receiver'
            binary.write_text('#!/bin/sh\nprintf QUOTED_OK\n')
            key = Path(tmp) / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            # The binary itself still installs and runs: only the SSH key is refused.
            plain = self.invoke(home, binary)
            self.assertEqual(plain.returncode, 0, plain.stderr)
            launcher = home / '.local/bin/hermes-teleoperator-receiver'
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'QUOTED_OK')
            refused = self.invoke(home, binary, '--public-key-file', str(key) + '.pub')
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('sshd dequotes command= twice', refused.stderr)
            self.assertFalse((home / '.ssh/authorized_keys').exists())

    def test_backslash_home_checksum_and_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / r'back\slash'
            home.mkdir()
            binary = Path(tmp) / 'receiver'
            binary.write_text('#!/bin/sh\nprintf BACKSLASH_OK\n')
            result = self.invoke(home, binary)
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = home / '.local/bin/hermes-teleoperator-receiver'
            self.assertEqual(subprocess.check_output([str(launcher)], text=True), 'BACKSLASH_OK')

    def test_sudo_user_rejected_even_for_non_root(self):
        credentials = {}
        if os.geteuid() == 0:
            import pwd
            try:
                nobody = pwd.getpwnam('nobody')
            except KeyError:
                self.skipTest('no unprivileged nobody account')

            if 'user' not in inspect.signature(subprocess.Popen).parameters:
                self.skipTest('subprocess credential arguments unavailable')
            credentials = {'user': nobody.pw_uid, 'group': nobody.pw_gid, 'extra_groups': []}

        # Feed stdin so an unprivileged process need not traverse a root-owned checkout.
        env = {**os.environ, 'SUDO_USER': 'original-user', 'HOME': '/nonexistent'}
        result = subprocess.run(['bash', '-s'], input='id -u\n' + (ROOT / 'install.sh').read_text(),
                                cwd='/', env=env, **credentials,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Do not run via sudo', result.stderr)
        self.assertEqual(int(result.stdout.strip()), credentials.get('user', os.geteuid()))

    def test_manual_entry_does_not_inspect_unsafe_ssh_state(self):
        for unsafe in ('symlink', 'group-writable', 'fifo'):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                binary = root / 'receiver'
                binary.write_text('#!/bin/sh\nexit 0\n')
                key = root / 'key'
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
                (root / '.ssh').mkdir(mode=0o700)
                auth = root / '.ssh/authorized_keys'
                target = root / 'untouched'
                target.write_text('existing key\n')
                if unsafe == 'symlink':
                    auth.symlink_to(target)
                elif unsafe == 'fifo':
                    os.mkfifo(auth)
                else:
                    auth.write_text('existing key\n')
                    auth.chmod(0o620)
                before = self.ssh_snapshot(root)
                result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.ssh_snapshot(root), before)
                self.assertEqual(target.read_text(), 'existing key\n')

    def test_writable_ancestors_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.local').mkdir()
            (root / '.local').chmod(0o777)
            binary = root / 'receiver'
            binary.write_text('payload')
            result = self.invoke(root, binary)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('writable', result.stderr)


FAKE_CURL = r'''#!/bin/bash
set -eu
if [[ -n ${FAKE_CURL_MARKER:-} ]]; then printf '%s\n' "$*" >> "$FAKE_CURL_MARKER"; fi
out=''; url=''
while (($#)); do
    case $1 in
        -o) out=$2; shift 2 ;;
        --proto) shift 2 ;;
        --tlsv1.2|-fsSL|-fsS|-fs|-f|-s|-S|-L) shift ;;
        https://*) url=$1; shift ;;
        *) shift ;;
    esac
done
[[ -n $out && -n $url ]] || exit 2
if [[ $url == */SHA256SUMS ]]; then
    cp -- "${FAKE_MANIFEST:?}" "$out"
else
    cp -- "${FAKE_BINARY:?}" "$out"
fi
'''


class ReleasePinTests(unittest.TestCase):
    """Release downloads trust the digest embedded in install.sh, not SHA256SUMS."""

    ASSET_X86_64 = 'hermes-teleoperator-receiver-x86_64-unknown-linux-musl'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.commands = self.root / 'commands'
        self.commands.mkdir()
        curl = self.commands / 'curl'
        curl.write_text(FAKE_CURL)
        curl.chmod(0o700)
        self.marker = self.root / 'curl-calls'
        self.binary = self.root / 'payload'
        self.manifest = self.root / 'SHA256SUMS'
        self.addCleanup(self.tmp.cleanup)

    def run_install(self, *extra, script=ROOT / 'install.sh', env=None, allow_root=True):
        base = {**os.environ, 'HOME': str(self.home),
                'PATH': f'{self.commands}:/usr/bin:/bin',
                'FAKE_BINARY': str(self.binary), 'FAKE_MANIFEST': str(self.manifest),
                'FAKE_CURL_MARKER': str(self.marker)}
        for key in ('SUDO_USER', 'HERMES_HOME', 'PYTHONPATH', 'TELEOPERATOR_HOME'):
            base.pop(key, None)
        base.update(env or {})
        root_option = ['--allow-root'] if allow_root else []
        return subprocess.run(['bash', str(script), *root_option, *extra], env=base,
                              capture_output=True, text=True, timeout=30)

    def write_payload(self, payload=b'#!/bin/sh\nprintf PINNED_OK\n', manifest_digest=None):
        self.binary.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.manifest.write_text(
            f'{manifest_digest or digest}  {self.ASSET_X86_64}\n', encoding='ascii')
        return digest

    def launcher(self):
        return self.home / '.local/bin/hermes-teleoperator-receiver'

    @unittest.skipUnless(os.geteuid() == 0, 'root install guard requires root')
    def test_root_install_requires_explicit_opt_in_before_download(self):
        result = self.run_install(allow_root=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Root install requires --allow-root', result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.launcher().exists())

    def test_release_install_rejects_tampered_binary_even_when_manifest_matches(self):
        tampered = b'tampered release bytes'
        self.write_payload(tampered, manifest_digest=hashlib.sha256(tampered).hexdigest())
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pinned in this installer', result.stderr)
        self.assertFalse(self.launcher().exists())

    def test_release_install_rejects_binary_that_differs_from_pinned_digest(self):
        from hermes_teleoperator.version import RECEIVER_SHA256
        self.write_payload(b'tampered release bytes',
                           manifest_digest=RECEIVER_SHA256[self.ASSET_X86_64])
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr.lower())
        self.assertFalse(self.launcher().exists())

    def test_release_install_refuses_asset_with_no_embedded_pin_before_downloading(self):
        script = self.root / 'install-no-x86-pin.sh'
        text = (ROOT / 'install.sh').read_text()
        text, count = re.subn(
            r'^\s+hermes-teleoperator-receiver-x86_64-unknown-linux-musl\) printf .*$',
            '', text, flags=re.M)
        self.assertEqual(count, 1)
        script.write_text(text)
        result = self.run_install(script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No SHA256 is pinned in this installer', result.stderr)
        self.assertFalse(self.marker.exists(), 'must fail before any network fetch')
        self.assertFalse(self.launcher().exists())

    def test_release_install_rejects_sha256_that_disagrees_with_embedded_pin(self):
        self.write_payload()
        result = self.run_install('--sha256', '0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match the digest pinned in this installer', result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.launcher().exists())

    def test_release_install_refuses_a_version_other_than_the_pinned_tag(self):
        self.write_payload()
        result = self.run_install('--version', 'v1.0.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only authorizes release', result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.launcher().exists())

    def test_release_install_rejects_manifest_without_selected_asset(self):
        self.binary.write_bytes(b'ignored')
        self.manifest.write_text('', encoding='ascii')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no valid entry', result.stderr)
        self.assertFalse(self.launcher().exists())

    def test_embedded_pin_drives_install_when_manifest_agrees(self):
        # The real v1.1.2 bytes are not in this fixture, so substitute the test
        # binary's digest in a copy. Parity tests keep the real table honest.
        payload = b'#!/bin/sh\nprintf PINNED_OK\n'
        digest = self.write_payload(payload)
        script = self.root / 'install-fixture-pin.sh'
        text = (ROOT / 'install.sh').read_text()
        text, count = re.subn(
            r"(hermes-teleoperator-receiver-x86_64-unknown-linux-musl\) printf '%s' ')[0-9a-f]{64}(')",
            rf'\g<1>{digest}\g<2>', text)
        self.assertEqual(count, 1)
        script.write_text(text)
        result = self.run_install(script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.check_output([str(self.launcher())], text=True), 'PINNED_OK')
        self.assertEqual(self.marker.read_text().count('/SHA256SUMS '), 1)


if __name__ == '__main__':
    unittest.main()
