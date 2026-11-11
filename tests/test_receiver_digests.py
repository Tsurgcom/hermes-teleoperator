"""Release provenance: source pins, installer parity and hosted-runner attestation."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from hermes_teleoperator import deploy
from hermes_teleoperator.version import RECEIVER_SHA256, VERSION

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/release.yml'


def job_block(text, name):
    match = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)', text, re.M | re.S)
    if not match:
        raise AssertionError(f'job {name} not found')
    return match.group(1)


class PinnedDigestTests(unittest.TestCase):
    def test_every_release_asset_has_one_well_formed_digest(self):
        self.assertEqual(set(RECEIVER_SHA256), set(deploy.ASSETS))
        for asset, digest in RECEIVER_SHA256.items():
            with self.subTest(asset=asset):
                self.assertRegex(digest, r'^[a-f0-9]{64}$')

    def test_installer_block_is_generated_from_the_same_table(self):
        result = subprocess.run(
            [sys.executable, 'scripts/sync_receiver_digests.py', '--check'],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'v{VERSION}', result.stdout)

    def test_release_assets_are_built_on_hosted_runners_and_attested(self):
        text = WORKFLOW.read_text()
        for name in ('binary-linux', 'binary-apple', 'publish'):
            with self.subTest(job=name):
                block = job_block(text, name)
                self.assertNotIn('self-hosted', block)
                self.assertIn('actions/attest-build-provenance@', block)
        self.assertIn('runs-on: ubuntu-24.04', job_block(text, 'binary-linux'))
        self.assertIn('runs-on: macos-15', job_block(text, 'binary-apple'))
        self.assertIn('attestations: write', job_block(text, 'publish'))
        self.assertIn('id-token: write', job_block(text, 'publish'))
        self.assertIn('scripts/verify_release_digests.py artifacts', job_block(text, 'publish'))

    def test_release_workflow_refuses_tag_and_version_drift(self):
        text = WORKFLOW.read_text()
        publish = job_block(text, 'publish')
        self.assertIn("tag != expected", publish)
        self.assertIn('git rev-parse HEAD', publish)

    def test_release_verifier_rejects_a_mismatched_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            for asset in deploy.ASSETS:
                (artifacts / asset).write_bytes(asset.encode())
            (artifacts / sorted(deploy.ASSETS)[0]).write_bytes(b'tampered')
            result = subprocess.run(
                [sys.executable, 'scripts/verify_release_digests.py', str(artifacts)],
                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('do not match the source pins', result.stderr)

    def test_release_verifier_rejects_a_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, 'scripts/verify_release_digests.py', tmp],
                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Release artifacts missing', result.stderr)

    @unittest.skipUnless(os.environ.get('TELEOPERATOR_VERIFY_PUBLISHED_RELEASE') == VERSION,
                         'opt-in: set TELEOPERATOR_VERIFY_PUBLISHED_RELEASE=<version> to check the live release')
    def test_published_manifest_matches_source_pins(self):
        url = f'https://github.com/Tsurgcom/hermes-teleoperator/releases/download/v{VERSION}/SHA256SUMS'
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode('ascii')
        entries = {}
        for line in text.splitlines():
            digest, name = line.split()
            entries[name] = digest
        self.assertEqual(entries, RECEIVER_SHA256)


if __name__ == '__main__':
    unittest.main()
