"""Release provenance: source pins, installer parity and hosted-runner attestation."""
import hashlib
import os
import re
import shutil
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
    def test_candidate_generation_and_verification_in_isolated_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'hermes_teleoperator', root / 'hermes_teleoperator',
                            ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT / 'scripts', root / 'scripts')
            shutil.copytree(ROOT / 'tests', root / 'tests',
                            ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT / '.github', root / '.github')
            shutil.copy2(ROOT / 'install.sh', root / 'install.sh')
            artifacts = root / 'artifacts'
            artifacts.mkdir()
            pins = {}
            for asset in deploy.ASSETS:
                data = f'candidate fixture {asset}'.encode()
                (artifacts / asset).write_bytes(data)
                pins[asset] = hashlib.sha256(data).hexdigest()
            sums = artifacts / 'SHA256SUMS'
            sums.write_text(''.join(f'{pins[a]}  {a}\n' for a in sorted(pins)))
            def run(*args):
                result = subprocess.run([sys.executable, *args], cwd=root,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            run('scripts/sync_receiver_digests.py', '--from-sums', str(sums))
            run('scripts/sync_receiver_digests.py', '--check')
            run('scripts/verify_release_digests.py', str(artifacts))
            run('-m', 'unittest',
                'tests.test_receiver_digests.PinnedDigestTests.test_release_verifier_rejects_a_mismatched_artifact',
                'tests.test_receiver_digests.PinnedDigestTests.test_release_verifier_rejects_a_missing_artifact',
                'tests.test_standalone_install.ReleasePinTests')
            self.assertIn(f"pinned_version='v{VERSION}'", (root / 'install.sh').read_text())
            self.assertIn(f"RECEIVER_PINS_VERSION = '{VERSION}'",
                          (root / 'hermes_teleoperator/version.py').read_text())

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
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'verify_release_digests', ROOT / 'scripts/verify_release_digests.py')
            verifier = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(verifier)
            pins = {asset: hashlib.sha256(asset.encode()).hexdigest() for asset in deploy.ASSETS}
            errors = verifier.check_artifacts(artifacts, pins)
            self.assertTrue(any('do not match the source pins' in line for line in errors), errors)

    def test_release_verifier_rejects_a_missing_artifact(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'verify_release_digests', ROOT / 'scripts/verify_release_digests.py')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        with tempfile.TemporaryDirectory() as tmp:
            errors = verifier.check_artifacts(tmp, RECEIVER_SHA256)
        self.assertIn('Release artifacts missing', errors[0])

    def test_release_verifier_pin_version_gate(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'verify_release_digests', ROOT / 'scripts/verify_release_digests.py')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        verifier.VERSION = '9.9.9'
        verifier.RECEIVER_PINS_VERSION = '9.9.8'
        self.assertIn('source pins attest v9.9.8', verifier.pin_version_error())
        verifier.RECEIVER_PINS_VERSION = '9.9.9'
        self.assertIsNone(verifier.pin_version_error())

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
