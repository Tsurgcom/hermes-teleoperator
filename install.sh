#!/usr/bin/env bash
# Standalone receiver install. No Python, Hermes, Bun or Rust toolchain on target.
set -euo pipefail
fail() { printf 'Installation failed: %s\n' "$*" >&2; exit 1; }
version='v1.1.2'
binary=''
digest=''
public_key=''
public_key_file=''
allow_root=false
home=${HOME:?HOME must identify the intended SSH user}
config_home=${TELEOPERATOR_HOME:-$home/.config/hermes-teleoperator}
while (($#)); do
    case "$1" in
        --version) version=${2:?}; shift 2 ;;
        --binary) binary=${2:?}; shift 2 ;;
        --sha256) digest=${2:?}; shift 2 ;;
        --public-key) public_key=${2:?}; shift 2 ;;
        --public-key-file) public_key_file=${2:?}; shift 2 ;;
        --teleoperator-home) config_home=${2:?}; shift 2 ;;
        --allow-root) allow_root=true; shift ;;
        --help) printf '%s\n' 'Usage: bash install.sh [--version v1.1.2] [--public-key "ssh-ed25519 ..."] [--teleoperator-home /absolute/path] [--allow-root]' 'Offline: --binary /path/to/receiver --sha256 <trusted SHA256>'; exit ;;
        *) fail "Unknown option: $1 (legacy --python/--hermes-home are no longer supported)" ;;
    esac
done
[[ -z ${SUDO_USER:-} ]] || fail 'Do not run via sudo; log in as the intended SSH user'
uid=$(id -u)
[[ $uid != 0 || $allow_root == true ]] || fail 'Root install requires --allow-root'
[[ $home == /* && $config_home == /* ]] || fail 'HOME and config directory must be absolute'
[[ $home != *$'\n'* && $config_home != *$'\n'* ]] || fail 'Newlines in paths are unsupported'
[[ $(uname -s) == Linux ]] || fail 'This release supports Linux only'
# Validate leaf and every existing ancestor, before writing/executing installed code.
secure_path() {
    local current=$1 leaf=$1 owner mode bits
    while :; do
        [[ ! -L $current ]] || fail "Refusing symlink: $current"
        if [[ -e $current ]]; then
            owner=$(stat -c %u -- "$current")
            mode=$(stat -c %a -- "$current")
            bits=$((8#$mode))
            [[ $owner == "$uid" || ( $current != "$leaf" && $owner == 0 ) ]] || fail "Unsafe owner: $current"
            if (( bits & 0022 )); then
                [[ $current != "$leaf" && $owner == 0 && -d $current ]] && (( bits & 01000 )) || fail "Group/other writable path: $current"
            fi
        fi
        [[ $current != / ]] || break
        current=$(dirname -- "$current")
    done
}
base=$home/.local/lib/hermes-teleoperator
launcher=$home/.local/bin/hermes-teleoperator-receiver
secure_path "$base"
secure_path "$launcher"
secure_path "$config_home"
if [[ -n $public_key_file ]]; then
    [[ -z $public_key ]] || fail 'Choose public-key OR public-key-file'
    public_key=$(< "$public_key_file")
    [[ -n $public_key ]] || fail 'Expected one ed25519 public key, never an empty file'
fi
if [[ -n $public_key ]]; then
    [[ $public_key != *$'\n'* && $public_key != *$'\r'* && $public_key =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || fail 'Expected one ed25519 public key, never a private key'
fi
umask 077
mkdir -p -- "$base" "$(dirname -- "$launcher")" "$config_home"
chmod 700 -- "$base"
staging=$(mktemp -d "$base/.install.XXXXXXXX")
launcher_tmp=''
trap 'rm -rf -- "$staging"; [[ -z $launcher_tmp ]] || rm -f -- "$launcher_tmp"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -z $binary ]]; then
    [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.-]+)?$ ]] || fail 'Use an exact release version'
    case $(uname -m) in
        x86_64) arch=x86_64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *) fail 'Unsupported CPU; build from receiver/ source' ;;
    esac
    asset=hermes-teleoperator-receiver-$arch-unknown-linux-musl
    url=https://github.com/Tsurgcom/hermes-teleoperator/releases/download/$version
    curl -fsSL --proto '=https' --tlsv1.2 "$url/$asset" -o "$staging/receiver"
    curl -fsSL --proto '=https' --tlsv1.2 "$url/SHA256SUMS" -o "$staging/SHA256SUMS"
    if [[ -z $digest ]]; then
        while read -r sum name; do
            if [[ $name == "$asset" || $name == "*$asset" ]]; then digest=$sum; break; fi
        done < "$staging/SHA256SUMS"
    fi
else
    [[ $binary == /* ]] || fail '--binary must be an absolute path'
    [[ -n $digest ]] || fail 'Offline binary requires a trusted --sha256'
    cp -- "$binary" "$staging/receiver"
fi
[[ $digest =~ ^[a-f0-9]{64}$ ]] || fail 'Missing/invalid checksum'
# `sha256sum <` reads stdin so no filename reaches the output: `sha256sum -- file` prints
# `\<hex>` for a path holding a backslash, which then never matches the pinned digest.
actual=$(sha256sum < "$staging/receiver")
[[ ${actual%% *} == "$digest" ]] || fail 'Binary checksum mismatch'
chmod 700 -- "$staging/receiver"
quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
command="$(quote "$launcher")"
entry=''
if [[ -n $public_key ]]; then
    printf '%s\n' "$public_key" > "$staging/key.pub"
    ssh-keygen -l -f "$staging/key.pub" >/dev/null || fail 'Invalid public key'
    read -r key_type key_data _ <<< "$public_key"
    # sshd dequotes a forced command TWICE: once for the config option, then again when the
    # user's shell parses it. A path that needs shell quoting loses one layer of backslashes
    # in between, so the entry either fails to start or executes injected text after the `;`.
    # Only a path that survives both passes byte-for-byte may be suggested; refuse the rest
    # instead of printing an unsafe entry for manual installation.
    [[ $launcher =~ ^[A-Za-z0-9/._+-]+$ ]] || fail "Refusing to generate a restricted key entry: launcher '$launcher' needs shell quoting, and sshd dequotes command= twice, so it cannot be written safely. Install into a home matching [A-Za-z0-9/._+-], or have an administrator configure a safe forced-command wrapper."
    entry="restrict,command=\"$command\" $key_type $key_data hermes-teleoperator"
fi
# Never replace the executable referenced by an existing launcher. Publish a
# checksum-named binary first, then atomically switch the launcher to it.
installed_binary=$base/receiver-$digest
mv -fT -- "$staging/receiver" "$installed_binary"
launcher_tmp=$(mktemp "$(dirname -- "$launcher")/.hermes-teleoperator-launcher.XXXXXXXX")
{
    printf '#!/bin/sh\nexport TELEOPERATOR_HOME=%s\nexec %s "$@"\n' "$(quote "$config_home")" "$(quote "$installed_binary")"
} > "$launcher_tmp"
chmod 700 -- "$launcher_tmp"
mv -fT -- "$launcher_tmp" "$launcher"
launcher_tmp=''
if [[ -n $entry ]]; then
    printf 'Manual administrator action required: install this restricted authorized_keys entry for the intended SSH user. No SSH authorization files were read or changed.\n%s\n' "$entry"
fi
printf 'Installed standalone receiver.\nSSH user: %s\nexecutable: %s\nteleoperator_home: %s\n' "$(id -un)" "$launcher" "$config_home"
printf 'No Hermes installation or language runtime is required on this computer.\n'
