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
fi
if [[ -n $public_key ]]; then
    [[ $public_key != *$'\n'* && $public_key != *$'\r'* && $public_key =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || fail 'Expected one ed25519 public key, never a private key'
    secure_path "$home/.ssh"
    secure_path "$home/.ssh/authorized_keys"
fi
umask 077
mkdir -p -- "$base" "$(dirname -- "$launcher")" "$config_home"
chmod 700 -- "$base"
staging=$(mktemp -d "$base/.install.XXXXXXXX")
auth_tmp=''
auth_lock=''
trap 'rm -rf -- "$staging"; [[ -z $auth_tmp ]] || rm -f -- "$auth_tmp"; [[ -z $auth_lock ]] || rmdir -- "$auth_lock"' EXIT
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
    # Only a path that survives both passes byte-for-byte may be enrolled; refuse the rest
    # instead of writing an entry that looks installed and is not.
    [[ $launcher =~ ^[A-Za-z0-9/._+-]+$ ]] || fail "Refusing to enrol a key: launcher '$launcher' needs shell quoting, and sshd dequotes command= twice, so it cannot be written safely. Install into a home matching [A-Za-z0-9/._+-], or add the authorized_keys line by hand."
    entry="restrict,command=\"$command\" $key_type $key_data hermes-teleoperator"
    mkdir -p -- "$home/.ssh"
    chmod 700 -- "$home/.ssh"
    auth=$home/.ssh/authorized_keys
    # mkdir is already required; no flock/runtime dependency. Hold through duplicate checking
    # and rename so concurrent installers cannot overwrite each other's dedicated keys.
    for ((attempt=0; ; attempt++)); do
        if mkdir -- "$home/.ssh/.authorized_keys.lock" 2>/dev/null; then
            auth_lock=$home/.ssh/.authorized_keys.lock
            break
        fi
        (( attempt < 100 )) || fail "Timed out waiting for $home/.ssh/.authorized_keys.lock; if stale, remove it only after confirming no installer is running"
        sleep 0.1
    done
    secure_path "$auth"
    [[ ! -e $auth || -f $auth ]] || fail "Not a regular file: $auth"
    if [[ -f $auth ]]; then
        while IFS= read -r line || [[ -n $line ]]; do
            [[ $line != *"$key_data"* || $line == "$entry" ]] || fail 'Key already authorised differently; use a dedicated key'
        done < "$auth"
    fi
fi
mv -f -- "$staging/receiver" "$base/receiver"
{
    printf '#!/bin/sh\nexport TELEOPERATOR_HOME=%s\nexec %s "$@"\n' "$(quote "$config_home")" "$(quote "$base/receiver")"
} > "$staging/launcher"
chmod 700 -- "$staging/launcher"
mv -f -- "$staging/launcher" "$launcher"
if [[ -n $entry ]]; then
    if [[ ! -f $auth ]] || ! grep -Fxq -- "$entry" "$auth"; then
        auth_tmp=$(mktemp "$home/.ssh/.authorized_keys.XXXXXXXX")
        if [[ -f $auth ]]; then
            cat -- "$auth" > "$auth_tmp"
        fi
        # Separate an existing unterminated line without altering other keys.
        [[ ! -s $auth_tmp ]] || printf '\n' >> "$auth_tmp"
        printf '%s\n' "$entry" >> "$auth_tmp"
        [[ ! -f $auth ]] || chmod --reference="$auth" -- "$auth_tmp"
        mv -f -- "$auth_tmp" "$auth"
        auth_tmp=''
    fi
fi
printf 'Installed standalone receiver.\nSSH user: %s\nexecutable: %s\nteleoperator_home: %s\n' "$(id -un)" "$launcher" "$config_home"
printf 'No Hermes installation or language runtime is required on this computer.\n'
