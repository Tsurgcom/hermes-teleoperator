import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from hermes_teleoperator.companion import Companion


class TransferTests(unittest.TestCase):
    def test_binary_transfer_is_verified_exclusive_and_move_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Companion()
            data = b'\x00binary\xff\n'
            digest = hashlib.sha256(data).hexdigest()
            path = str(Path(tmp) / 'file.bin')
            args = {'path': path, 'data': base64.b64encode(data).decode(), 'sha256': digest}
            written = c.dispatch('file_put', args)
            self.assertEqual(written['sha256'], digest)
            self.assertEqual(Path(path).read_bytes(), data)
            with self.assertRaises(FileExistsError):
                c.dispatch('file_put', args)
            loaded = c.dispatch('file_get', {'path': path})
            self.assertEqual(base64.b64decode(loaded['data']), data)
            with self.assertRaisesRegex(ValueError, 'changed'):
                c.dispatch('file_remove', {'path': path, 'sha256': 'bad'})
            self.assertTrue(Path(path).exists())
            c.dispatch('file_remove', {'path': path, 'sha256': digest})
            self.assertFalse(Path(path).exists())

    def test_corrupt_payload_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                Companion().dispatch('file_put', {'path': str(path), 'data': 'eA==', 'sha256': 'bad'})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])
