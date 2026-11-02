"""Real process tests; no network service or global Hermes state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CompanionTests(unittest.TestCase):
    def test_mcp_sampling_must_be_explicitly_disabled(self):
        from hermes_teleoperator.runtime import validate_mcp_config
        with self.assertRaisesRegex(ValueError, 'sampling'):
            validate_mcp_config({'fixture': {'command': 'python3'}}, ['fixture'])
        allowed = validate_mcp_config({'fixture': {'sampling': {'enabled': False}}, 'other': {}}, ['fixture'])
        self.assertEqual(allowed, ['fixture'])

    def test_malformed_frame_returns_error_and_next_request_works(self):
        import io
        from hermes_teleoperator.companion import Companion, serve
        source = io.StringIO('not json\n' + json.dumps({'id': 2, 'method': 'ping'}) + '\n')
        sink = io.StringIO()
        serve(Companion(), source, sink)
        frames = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertIn('error', frames[0])
        self.assertEqual(frames[1]['id'], 2)
        self.assertEqual(frames[1]['result']['protocol'], 1)

    def test_ping_identifies_machine_and_protocol(self):
        proc = subprocess.run(
            [sys.executable, '-m', 'hermes_teleoperator.companion'],
            input=json.dumps({'id': 'one', 'method': 'ping', 'params': {}}) + '\n',
            capture_output=True, text=True, cwd=ROOT,
            env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['id'], 'one')
        self.assertEqual(result['result']['protocol'], 1)
        self.assertTrue(result['result']['hostname'])
        self.assertTrue(Path(result['result']['cwd']).is_absolute())


if __name__ == '__main__':
    unittest.main()
