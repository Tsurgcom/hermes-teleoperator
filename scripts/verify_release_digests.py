#!/usr/bin/env python3
"""Refuse to publish release assets that differ from the digests committed in source.

Run in the release workflow after downloading the build artifacts. Every asset in
``deploy.ASSETS`` must be present exactly once and hash to its pinned digest.
This ties the release tag's bytes to the source tree the tag points at.
"""
import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_teleoperator.deploy import ASSETS  # noqa: E402
from hermes_teleoperator.version import RECEIVER_PINS_VERSION, RECEIVER_SHA256, VERSION  # noqa: E402


def pin_version_error():
    """None when the committed digests attest the package version."""
    if RECEIVER_PINS_VERSION == VERSION:
        return None
    return (f'Refusing to publish v{VERSION}: source pins attest v{RECEIVER_PINS_VERSION} artifacts. '
            f'Build a v{VERSION} candidate and run scripts/sync_receiver_digests.py --from-sums '
            'before tagging or publishing.')


def check_artifacts(artifacts, pins):
    """Return error lines for a build directory, ignoring which release the pins attest."""
    if set(pins) != set(ASSETS):
        missing = sorted(set(ASSETS) - set(pins))
        extra = sorted(set(pins) - set(ASSETS))
        return [f'Pinned digest table does not match release assets; missing={missing} extra={extra}']
    seen = {}
    for path in sorted(Path(artifacts).iterdir()):
        if path.name == 'SHA256SUMS':
            continue
        if path.name not in ASSETS:
            return [f'Unexpected release artifact: {path.name}']
        seen[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    missing = sorted(set(ASSETS) - set(seen))
    if missing:
        return [f'Release artifacts missing: {missing}']
    mismatched = sorted(asset for asset, digest in seen.items() if digest != pins[asset])
    if mismatched:
        lines = [f'{asset}: built {seen[asset]} != pinned {pins[asset]}' for asset in mismatched]
        lines.append(f'Refusing to publish v{VERSION}: {len(mismatched)} asset(s) do not match the source pins')
        return lines
    manifest = Path(artifacts) / 'SHA256SUMS'
    if manifest.exists():
        expected = ''.join(f'{pins[asset]}  {asset}\n' for asset in sorted(pins))
        if manifest.read_text(encoding='ascii') != expected:
            return ['SHA256SUMS does not exactly match the source-pinned digests']
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path, help='Directory containing release assets')
    args = parser.parse_args(argv)

    stale = pin_version_error()
    if stale:
        print(stale, file=sys.stderr)
        return 1

    errors = check_artifacts(args.artifacts, RECEIVER_SHA256)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Verified {len(ASSETS)} release assets against the v{VERSION} source pins')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
