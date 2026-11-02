"""The destination installer needs only POSIX utilities, not Python/Hermes."""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Optional
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StandaloneInstallTests(unittest.TestCase):
    def invoke(self, home, binary, *extra, env_extra=None):
        env = {k: v for k, v in {**os.environ, 'HOME': str(home)}.items()
               if k not in ('SUDO_USER', 'HERMES_HOME', 'PYTHONPATH', 'TELEOPERATOR_HOME')}
        env.update(env_extra or {})
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        return subprocess.run(['bash', str(ROOT / 'install.sh'), '--binary', str(binary),
            '--sha256', digest, '--allow-root', *extra], env=env, capture_output=True, text=True)

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

    def test_checksum_mismatch_does_not_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('payload')
            result = self.invoke(root, binary, '--sha256', '0' * 64)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('checksum', result.stderr.lower())
            self.assertFalse((root / '.local/bin/hermes-teleoperator-receiver').exists())

    def test_restricted_public_key_enrolment_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            key = root / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            for _ in range(2):
                result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
                self.assertEqual(result.returncode, 0, result.stderr)
            auth = root / '.ssh/authorized_keys'
            self.assertEqual(len(auth.read_text().splitlines()), 1)
            self.assertTrue(auth.read_text().startswith('restrict,command="'))
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)

    def test_key_enrolment_atomically_preserves_existing_content_and_mode(self):
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
            old = root / 'old-authorized_keys'
            os.link(auth, old)
            result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(old.read_bytes(), original, 'the old inode must not be appended to')
            self.assertNotEqual(auth.stat().st_ino, old.stat().st_ino)
            self.assertTrue(auth.read_bytes().startswith(original + b'\nrestrict,command='))
            self.assertEqual(auth.stat().st_mode & 0o777, 0o400)
            self.assertEqual(sorted(p.name for p in auth.parent.iterdir()), ['authorized_keys'])

    def test_parallel_enrolments_preserve_all_dedicated_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'receiver'
            binary.write_text('#!/bin/sh\nexit 0\n')
            keys = []
            for index in range(6):
                key = root / f'key{index}'
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
                keys.append(Path(str(key) + '.pub'))
            wrappers = root / 'wrappers'
            wrappers.mkdir()
            # Widen the real read/rename race, without replacing either operation.
            mv = wrappers / 'mv'
            mv.write_text('#!/bin/bash\ncase "${!#}" in */authorized_keys) sleep 0.15;; esac\n'
                          f'exec {shutil.which("mv")} "$@"\n')
            mv.chmod(0o700)
            env = {'PATH': str(wrappers) + os.pathsep + os.environ['PATH']}
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(keys)) as pool:
                results = list(pool.map(lambda key: self.invoke(root, binary, '--public-key-file',
                                                               str(key), env_extra=env), keys))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            auth = (root / '.ssh/authorized_keys').read_text()
            self.assertEqual(len([line for line in auth.splitlines() if line]), len(keys))
            for key in keys:
                self.assertEqual(auth.count(key.read_text().split()[1]), 1)
            self.assertEqual(sorted(p.name for p in (root / '.ssh').iterdir()), ['authorized_keys'])

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
            auth = (home / '.ssh/authorized_keys').read_text()
            option = re.match(r'restrict,command="((?:\\.|[^"\\])*)"', auth).group(1)
            # No escapes at all: a safe-charset path is single-quoted and needs none, which is
            # the property that makes it survive sshd's two passes unchanged.
            self.assertNotIn('\\', option)
            command = re.sub(r'\\(.)', r'\1', option)   # pass 1: OpenSSH option dequoting
            self.assertEqual(subprocess.check_output(['/bin/sh', '-c', command], text=True),
                             'FORCED_OK')              # pass 2: the user's shell

    def test_metacharacter_home_installs_but_refuses_key_enrolment(self):
        """A path needing shell quoting cannot be forced-command quoted safely, so the installer
        must fail closed on enrolment rather than write an entry that executes injected text."""
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
        drop_privileges: Optional[Callable[[], None]] = None
        if os.geteuid() == 0:
            import pwd
            try:
                nobody = pwd.getpwnam('nobody')
            except KeyError:
                self.skipTest('no unprivileged nobody account')

            def demote():
                os.setgroups([])
                os.setgid(nobody.pw_gid)
                os.setuid(nobody.pw_uid)

            drop_privileges = demote

        # Feed stdin so an unprivileged process need not traverse a root-owned checkout.
        env = {**os.environ, 'SUDO_USER': 'original-user', 'HOME': '/nonexistent'}
        result = subprocess.run(['bash', '-s'], input=(ROOT / 'install.sh').read_text(),
                                cwd='/', env=env, preexec_fn=drop_privileges,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Do not run via sudo', result.stderr)

    def test_authorized_keys_symlink_and_group_writable_rejected(self):
        for unsafe in ('symlink', 'group-writable'):
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
                else:
                    auth.write_text('existing key\n')
                    auth.chmod(0o620)
                result = self.invoke(root, binary, '--public-key-file', str(key) + '.pub')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('symlink' if unsafe == 'symlink' else 'writable', result.stderr)
                self.assertEqual(auth.read_text(), 'existing key\n')
                self.assertEqual(target.read_text(), 'existing key\n')
                self.assertFalse((root / '.local').exists())

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


if __name__ == '__main__':
    unittest.main()
