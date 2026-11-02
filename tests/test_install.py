"""User-local installation, SSH enrolment and root guardrails."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_rejects_sudo_and_unconfirmed_root(self):
        with patch.dict(os.environ, {'SUDO_USER': 'alice'}), patch.object(os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'sudo'):
                installer.check_user(True)
        with patch.dict(os.environ, {}, clear=True), patch.object(os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'allow-root'):
                installer.check_user(False)

    def test_installs_launcher_and_restricted_key_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            key = home / 'test-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            pub = key.with_suffix('.pub').read_text()
            with patch.object(installer, 'check_user'), patch.object(installer, 'check_python', return_value=str(ROOT)):
                for _ in range(2):
                    result = installer.install(ROOT, sys.executable, home, home / 'profile', pub)
            auth = home / '.ssh/authorized_keys'
            self.assertEqual(len(auth.read_text().splitlines()), 1)
            self.assertTrue(auth.read_text().startswith('restrict,command="'))
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertIn('hermes_teleoperator.companion', auth.read_text())
            response = subprocess.run([result['python'], '-m', 'hermes_teleoperator.companion'],
                input='{"id":1,"method":"ping"}\n', text=True, capture_output=True, check=True)
            self.assertIn(str(home / 'profile'), response.stdout)
            self.assertFalse((home / 'profile/teleoperator.json').exists())

    def test_curl_bash_bootstrap_pipeline_with_local_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name in ('scripts/install.py', 'hermes_teleoperator'):
                    tar.add(ROOT / name, arcname='repo/' + name)
            tools = root / 'tools'
            tools.mkdir()
            curl = tools / 'curl'
            curl.write_text('#!/bin/bash\nwhile (($#)); do if [[ "$1" == -o ]]; then cp "$TEST_ARCHIVE" "$2"; exit; fi; shift; done\nexit 1\n')
            curl.chmod(0o700)
            home = root / 'home'
            home.mkdir()
            # Fake only the installed-Hermes dependency probe; execute all installer code.
            python = root / 'python'
            python.write_text('#!/bin/bash\nif [[ "$1" == -c ]]; then printf "%s\\n" "$TEST_ROOT"; else exec "$TEST_PYTHON" "$@"; fi\n')
            python.chmod(0o700)
            env = {**os.environ, 'PATH': str(tools) + ':' + os.environ['PATH'],
                   'HOME': str(home), 'TEST_ARCHIVE': str(archive), 'TEST_ROOT': str(ROOT),
                   'TEST_PYTHON': sys.executable}
            env.pop('SUDO_USER', None)
            result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--ref', 'a' * 40,
                '--python', str(python), '--allow-root'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / '.local/lib/hermes-teleoperator/python').is_file())

    def test_rejects_bad_keys_and_unsafe_authorized_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(ValueError):
                installer.validate_key('ssh-ed25519 invalid\ncommand="evil"')
            (home / '.ssh').mkdir()
            (home / '.ssh/authorized_keys').symlink_to(home / 'elsewhere')
            with patch.object(installer, 'check_user'), patch.object(installer, 'check_python', return_value=str(ROOT)):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    installer.install(ROOT, sys.executable, home, home / 'profile', 'invalid')


if __name__ == '__main__':
    unittest.main()
