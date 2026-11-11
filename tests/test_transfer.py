import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_teleoperator.files import file_operation, local_file_operation


def _payload(data):
    return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}


class TransferTests(unittest.TestCase):
    def test_binary_transfer_is_verified_exclusive_and_move_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatch = file_operation
            data = b'\x00binary\xff\n'
            digest = hashlib.sha256(data).hexdigest()
            path = str(Path(tmp) / 'file.bin')
            args = {'path': path, 'data': base64.b64encode(data).decode(), 'sha256': digest}
            written = dispatch('file_put', args)
            self.assertEqual(written['sha256'], digest)
            self.assertEqual(Path(path).read_bytes(), data)
            with self.assertRaises(FileExistsError):
                dispatch('file_put', args)
            loaded = dispatch('file_get', {'path': path})
            self.assertEqual(base64.b64decode(loaded['data']), data)
            with self.assertRaisesRegex(ValueError, 'changed'):
                dispatch('file_remove', {'path': path, 'sha256': 'bad'})
            self.assertTrue(Path(path).exists())
            dispatch('file_remove', {'path': path, 'sha256': digest})
            self.assertFalse(Path(path).exists())

    def test_corrupt_payload_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                file_operation('file_put', {'path': str(path), 'data': 'eA==', 'sha256': 'bad'})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_intermediate_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / 'real'
            real.mkdir()
            target = real / 'file.bin'
            target.write_bytes(b'abc')
            link = base / 'linkdir'
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                file_operation('file_get', {'path': str(link / 'file.bin')})
            with self.assertRaisesRegex(ValueError, 'not a directory'):
                file_operation('file_get', {'path': str(target) + '/child'})
            self.assertEqual(target.read_bytes(), b'abc')


class LocalTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'transfer'
        self.root.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.hermes = self.base / 'hermes'
        self.hermes.mkdir()
        for target, value in (('_hermes_home', self.hermes), ('_gateway_home', self.home)):
            patcher = patch(f'hermes_teleoperator.files.{target}', return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def put(self, path, allow_delete=False):
        data = b'local payload'
        result = local_file_operation('file_put', {'path': str(path), **_payload(data)},
                                      self.root, allow_delete=allow_delete)
        self.assertEqual(Path(result['path']).read_bytes(), data)
        return data

    def test_round_trip_inside_transfer_dir_and_delete_gate(self):
        path = self.root / 'file.bin'
        data = self.put(path)
        digest = hashlib.sha256(data).hexdigest()
        loaded = local_file_operation('file_get', {'path': str(path)}, self.root)
        self.assertEqual(base64.b64decode(loaded['data']), data)
        self.assertEqual(loaded['sha256'], digest)
        with self.assertRaises(FileExistsError):
            local_file_operation('file_put', {'path': str(path), **_payload(b'new')}, self.root)
        with self.assertRaisesRegex(ValueError, 'allow_local_delete'):
            local_file_operation('file_remove', {'path': str(path), 'sha256': digest}, self.root)
        self.assertTrue(path.exists())
        with self.assertRaisesRegex(ValueError, 'changed'):
            local_file_operation('file_remove', {'path': str(path), 'sha256': '0' * 64},
                                 self.root, allow_delete=True)
        self.assertTrue(path.exists())
        local_file_operation('file_remove', {'path': str(path), 'sha256': digest},
                             self.root, allow_delete=True)
        self.assertFalse(path.exists())

    def test_disabled_and_escaping_paths_are_refused(self):
        self.put(self.root / 'kept.bin')
        with self.assertRaisesRegex(ValueError, 'transfer_dir'):
            local_file_operation('file_get', {'path': str(self.root / 'kept.bin')}, None)
        with self.assertRaisesRegex(ValueError, 'transfer_dir'):
            local_file_operation('file_get', {'path': str(self.root / 'kept.bin')},
                                 self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            local_file_operation('file_get', {'path': str(self.root / 'kept.bin')},
                                 'relative-transfer-dir')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            local_file_operation('file_get', {'path': 'relative.bin'}, self.root)
        with self.assertRaisesRegex(ValueError, 'outside'):
            local_file_operation('file_get', {'path': str(self.base / 'outside.bin')}, self.root)
        with self.assertRaisesRegex(ValueError, 'outside'):
            local_file_operation('file_get',
                                 {'path': str(self.root / '..' / 'outside.bin')}, self.root)
        with self.assertRaisesRegex(ValueError, 'outside'):
            local_file_operation('file_put',
                                 {'path': str(self.root / '..' / 'new.bin'), **_payload(b'x')},
                                 self.root)
        self.assertFalse((self.base / 'new.bin').exists())

    def test_symlinks_resolve_before_containment(self):
        outside = self.base / 'outside'
        outside.mkdir()
        secret = outside / 'secret.bin'
        secret.write_bytes(b'outside secret')
        (self.root / 'escape.bin').symlink_to(secret)
        with self.assertRaisesRegex(ValueError, 'outside'):
            local_file_operation('file_get', {'path': str(self.root / 'escape.bin')}, self.root)
        (self.root / 'escape-dir').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'outside'):
            local_file_operation('file_put',
                                 {'path': str(self.root / 'escape-dir' / 'new.bin'),
                                  **_payload(b'x')}, self.root)
        self.assertFalse((outside / 'new.bin').exists())
        # A symlink that stays inside the configured directory remains usable.
        real = self.root / 'real.bin'
        real.write_bytes(b'inside')
        (self.root / 'inside-link.bin').symlink_to(real)
        loaded = local_file_operation('file_get', {'path': str(self.root / 'inside-link.bin')},
                                      self.root)
        self.assertEqual(base64.b64decode(loaded['data']), b'inside')

    def test_hermes_home_is_refused_even_inside_transfer_dir(self):
        secret = self.hermes / 'auth.json'
        secret.write_text('{"token": "secret"}')
        transfer_dir = self.base
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_get', {'path': str(secret)}, transfer_dir)
        (self.base / 'hermes-link').symlink_to(secret)
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_get', {'path': str(self.base / 'hermes-link')},
                                 transfer_dir)
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_put',
                                 {'path': str(self.hermes / '.env'), **_payload(b'x')},
                                 transfer_dir)
        self.assertFalse((self.hermes / '.env').exists())
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_remove',
                                 {'path': str(secret), 'sha256': hashlib.sha256(secret.read_bytes()).hexdigest()},
                                 transfer_dir, allow_delete=True)
        self.assertTrue(secret.exists())

    def test_ssh_directory_is_refused_even_inside_transfer_dir(self):
        ssh = self.home / '.ssh'
        ssh.mkdir()
        key = ssh / 'id_ed25519'
        key.write_text('PRIVATE KEY')
        transfer_dir = self.base
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_get', {'path': str(key)}, transfer_dir)
        (self.base / 'ssh-link').symlink_to(key)
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_get', {'path': str(self.base / 'ssh-link')}, transfer_dir)
        (self.base / 'ssh-dir-link').symlink_to(ssh)
        with self.assertRaisesRegex(ValueError, 'protected'):
            local_file_operation('file_put',
                                 {'path': str(self.base / 'ssh-dir-link' / 'authorized_keys'),
                                  **_payload(b'ssh-ed25519 attacker')},
                                 transfer_dir)
        self.assertFalse((ssh / 'authorized_keys').exists())

    def test_failed_staging_create_keeps_the_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('hermes_teleoperator.files.time.time_ns', return_value=123):
                staging = root / f'.teleoperator-{os.getpid()}-123'
                staging.write_bytes(b'keep')
                dest = root / 'out.bin'
                with self.assertRaises(FileExistsError):
                    file_operation('file_put', {'path': str(dest), **_payload(b'abc')})
                self.assertEqual(staging.read_bytes(), b'keep')
                self.assertFalse(dest.exists())
