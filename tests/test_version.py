"""A release must identify the gateway, receiver and installer consistently."""
from pathlib import Path
import re
import tomllib
import unittest
import hermes_teleoperator
from hermes_teleoperator.version import VERSION

ROOT = Path(__file__).resolve().parents[1]

class VersionParityTests(unittest.TestCase):
    def test_all_release_metadata_matches_gateway(self):
        self.assertEqual(tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'], VERSION)
        self.assertEqual(tomllib.loads((ROOT / 'receiver/Cargo.toml').read_text())['package']['version'], VERSION)
        lock = tomllib.loads((ROOT / 'receiver/Cargo.lock').read_text())
        crate = next(p for p in lock['package'] if p['name'] == 'hermes-teleoperator-receiver')
        self.assertEqual(crate['version'], VERSION)
        self.assertIn(f'version: {VERSION}\n', (ROOT / 'plugin.yaml').read_text())
        script = (ROOT / 'install.sh').read_text()
        match = re.search(r"^version='v([^']+)'", script, re.M)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), VERSION)
        self.assertEqual(hermes_teleoperator.__version__, VERSION)
        self.assertIn(f'version: {VERSION}\n', (ROOT / 'skills/hermes-teleoperator/SKILL.md').read_text())
