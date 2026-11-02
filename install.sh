#!/usr/bin/env bash
# Download a pinned revision; only the public SSH key belongs in arguments.
set -euo pipefail
ref=''
python=''
args=()
while (($#)); do
    case "$1" in
        --ref) ref="${2:?--ref needs a full commit SHA}"; shift 2 ;;
        --python) python="${2:?--python needs the Hermes interpreter path}"; args+=(--python "$python"); shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
if [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]] || [[ "$python" != /* ]]; then
    printf '%s\n' 'Usage: bash install.sh --ref <40-character commit SHA> --python /absolute/hermes/venv/bin/python [--public-key "ssh-ed25519 ..."] [--hermes-home /profile] [--allow-root]' >&2
    exit 2
fi
cache="${TMPDIR:-$HOME/.cache}"
mkdir -p "$cache"
staging=$(mktemp -d "$cache/teleoperator-install.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
    "https://codeload.github.com/tsurgcom/hermes-teleoperator/tar.gz/$ref" -o "$staging/source.tar.gz"
"$python" - "$staging" <<'PY'
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
with tarfile.open(root / 'source.tar.gz') as archive:
    for member in archive:
        parts = pathlib.PurePosixPath(member.name).parts
        if len(parts) < 2 or '..' in parts or member.name.startswith('/'):
            continue
        relative = pathlib.Path(*parts[1:])
        if not (relative.parts[0] == 'hermes_teleoperator' or str(relative) == 'scripts/install.py'):
            continue
        if not member.isfile():
            continue
        target = root / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.extractfile(member).read())
PY
"$python" "$staging/source/scripts/install.py" "${args[@]}"
