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
from hermes_teleoperator.version import RECEIVER_SHA256, VERSION  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path, help='Directory containing release assets')
    args = parser.parse_args(argv)

    if set(RECEIVER_SHA256) != set(ASSETS):
        missing = sorted(set(ASSETS) - set(RECEIVER_SHA256))
        extra = sorted(set(RECEIVER_SHA256) - set(ASSETS))
        print(f'Pinned digest table does not match release assets; missing={missing} extra={extra}', file=sys.stderr)
        return 1

    seen = {}
    for path in sorted(args.artifacts.iterdir()):
        if path.name == 'SHA256SUMS':
            continue
        if path.name not in ASSETS:
            print(f'Unexpected release artifact: {path.name}', file=sys.stderr)
            return 1
        seen[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()

    missing = sorted(set(ASSETS) - set(seen))
    if missing:
        print(f'Release artifacts missing: {missing}', file=sys.stderr)
        return 1

    mismatched = sorted(asset for asset, digest in seen.items() if digest != RECEIVER_SHA256[asset])
    if mismatched:
        for asset in mismatched:
            print(f'{asset}: built {seen[asset]} != pinned {RECEIVER_SHA256[asset]}', file=sys.stderr)
        print(f'Refusing to publish v{VERSION}: {len(mismatched)} asset(s) do not match the source pins',
              file=sys.stderr)
        return 1

    manifest = args.artifacts / 'SHA256SUMS'
    if manifest.exists():
        expected = ''.join(f'{RECEIVER_SHA256[asset]}  {asset}\n' for asset in sorted(RECEIVER_SHA256))
        if manifest.read_text(encoding='ascii') != expected:
            print('SHA256SUMS does not exactly match the source-pinned digests', file=sys.stderr)
            return 1
    print(f'Verified {len(seen)} release assets against the v{VERSION} source pins')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
