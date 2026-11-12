#!/usr/bin/env python3
"""Keep the standalone installer's pinned digests in sync with Python source.

``hermes_teleoperator/version.py`` is the single source of truth. This script
renders the marked block in ``install.sh`` from that table (or, with
``--from-sums``, updates the table from a release build's SHA256SUMS). ``--check``
is for CI: it fails when the installer and source disagree.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_teleoperator.deploy import ASSETS  # noqa: E402
from hermes_teleoperator.version import RECEIVER_PINS_VERSION, RECEIVER_SHA256, VERSION  # noqa: E402

INSTALL_SH = ROOT / 'install.sh'
VERSION_PY = ROOT / 'hermes_teleoperator/version.py'
START = '# BEGIN PINNED RECEIVER DIGESTS'
END = '# END PINNED RECEIVER DIGESTS'
DIGEST = re.compile(r'[a-f0-9]{64}')


def validate(pins):
    if set(pins) != set(ASSETS):
        missing = sorted(set(ASSETS) - set(pins))
        extra = sorted(set(pins) - set(ASSETS))
        raise SystemExit(f'Pinned digest table does not match release assets; missing={missing} extra={extra}')
    bad = sorted(asset for asset, digest in pins.items() if not DIGEST.fullmatch(digest))
    if bad:
        raise SystemExit(f'Malformed SHA256 pinned for: {bad}')
    return pins


def render_block(pins):
    lines = [
        START,
        '# Generated from hermes_teleoperator/version.py by scripts/sync_receiver_digests.py.',
        '# Do not edit by hand. These digests are the trust root for release downloads;',
        '# SHA256SUMS is fetched only to detect a rewritten release.',
        'pinned_digest() {',
        '    case $1 in',
    ]
    for asset in sorted(pins):
        lines.append(f"        {asset}) printf '%s' '{pins[asset]}' ;;")
    lines.extend(['        *) return 1 ;;', '    esac', '}', END])
    return '\n'.join(lines)


def replace_block(text, block, path):
    pattern = re.compile(re.escape(START) + r'.*?' + re.escape(END), re.S)
    if not pattern.search(text):
        raise SystemExit(f'{path}: pinned digest markers not found')
    return pattern.sub(lambda _: block, text, count=1)


def render_version_table(pins):
    lines = ['RECEIVER_SHA256 = {']
    for asset in sorted(pins):
        lines.append(f"    '{asset}':")
        lines.append(f"        '{pins[asset]}',")
    lines.append('}')
    return '\n'.join(lines)


def read_sums(path):
    pins = {}
    text = Path(path).read_text(encoding='ascii')
    for line in text.splitlines():
        match = re.fullmatch(r'([a-f0-9]{64}) [ *]([A-Za-z0-9._-]+)', line)
        if not match:
            raise SystemExit(f'{path}: malformed SHA256SUMS line: {line!r}')
        if match[2] in pins:
            raise SystemExit(f'{path}: duplicate entry for {match[2]}')
        pins[match[2]] = match[1]
    if set(pins) != set(ASSETS):
        missing = sorted(set(ASSETS) - set(pins))
        extra = sorted(set(pins) - set(ASSETS))
        raise SystemExit(f'{path}: manifest does not describe the release; missing={missing} extra={extra}')
    return validate(pins)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail if install.sh or version.py is out of sync')
    parser.add_argument('--from-sums', metavar='SHA256SUMS', help='Update version.py from a release build manifest first')
    args = parser.parse_args(argv)

    pins = read_sums(args.from_sums) if args.from_sums else validate(dict(RECEIVER_SHA256))
    version_text = VERSION_PY.read_text(encoding='utf-8')
    install_text = INSTALL_SH.read_text(encoding='utf-8')
    if args.from_sums:
        if "RECEIVER_PINS_VERSION = " not in version_text:
            raise SystemExit(f'{VERSION_PY}: RECEIVER_PINS_VERSION not found')
        version_text = re.sub(r"RECEIVER_PINS_VERSION = '[^']+'",
                              f"RECEIVER_PINS_VERSION = '{VERSION}'", version_text, count=1)
        rendered = render_version_table(pins)
        pattern = re.compile(r'RECEIVER_SHA256 = \{.*?\n\}', re.S)
        if not pattern.search(version_text):
            raise SystemExit(f'{VERSION_PY}: RECEIVER_SHA256 table not found')
        version_text = pattern.sub(lambda _: rendered, version_text, count=1)

    block = render_block(pins)
    install_text = replace_block(install_text, block, INSTALL_SH)

    if args.check:
        changed = []
        if version_text != VERSION_PY.read_text(encoding='utf-8'):
            changed.append(str(VERSION_PY))
        if install_text != INSTALL_SH.read_text(encoding='utf-8'):
            changed.append(str(INSTALL_SH))
        if changed:
            raise SystemExit('Pinned receiver digests are out of sync: ' + ', '.join(changed) +
                             ' (run python scripts/sync_receiver_digests.py)')
        print(f'Pinned receiver digests are in sync for v{VERSION} '
              f'({len(pins)} assets attesting v{RECEIVER_PINS_VERSION})')
        return 0

    if version_text != VERSION_PY.read_text(encoding='utf-8'):
        VERSION_PY.write_text(version_text, encoding='utf-8')
        print(f'Updated {VERSION_PY}')
    if install_text != INSTALL_SH.read_text(encoding='utf-8'):
        INSTALL_SH.write_text(install_text, encoding='utf-8')
        print(f'Updated {INSTALL_SH}')
    print(f'Pinned receiver digests for v{VERSION}: {len(pins)} assets')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
