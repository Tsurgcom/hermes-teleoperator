#!/usr/bin/env bash
# Standalone receiver install. No Python, Hermes, Bun or Rust toolchain on target.
set -euo pipefail
fail() { printf 'Installation failed: %s\n' "$*" >&2; exit 1; }
version='v1.2.0'
pinned_version=$version
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
        --help) printf '%s\n' 'Usage: bash install.sh [--version v1.2.0] [--public-key "ssh-ed25519 ..."] [--teleoperator-home /absolute/path] [--allow-root]' 'Release downloads are verified against SHA256 digests embedded in this script for the exact tag; SHA256SUMS is only cross-checked.' 'Offline: --binary /path/to/receiver --sha256 <trusted SHA256>'; exit ;;
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
# BEGIN PINNED RECEIVER DIGESTS
# Generated from hermes_teleoperator/version.py by scripts/sync_receiver_digests.py.
# Do not edit by hand. These digests are the trust root for release downloads;
# SHA256SUMS is fetched only to detect a rewritten release.
pinned_digest() {
    case $1 in
        hermes-teleoperator-receiver-aarch64-apple-darwin) printf '%s' '6ebdbc07ca7cc59c59c2cbeb59fc3ab5c0fe37e0f74e7750039b355f20e49ad5' ;;
        hermes-teleoperator-receiver-aarch64-unknown-linux-gnu) printf '%s' '486cb6589495af5c3ba05584deef6187da41f97e20fdcb6e95aeddd2211747f7' ;;
        hermes-teleoperator-receiver-aarch64-unknown-linux-musl) printf '%s' '9d1546d1754d75017d40542018167b765407f5ecc7d2b2ecf7071414ace1ec82' ;;
        hermes-teleoperator-receiver-armv7-unknown-linux-gnueabihf) printf '%s' '2a1d6c1e304a67d73c13a7762aee3b0bdc683a28595b88fcd9873bfaa10054fb' ;;
        hermes-teleoperator-receiver-armv7-unknown-linux-musleabihf) printf '%s' '130a39607f532e648471e3ae00ec8ca26972431aef71210f3b324065a44047ee' ;;
        hermes-teleoperator-receiver-i686-unknown-linux-gnu) printf '%s' 'a94134bf3a017dae6a5a58a612d2cf8f4fdb99cf5c298d6c9a3ad5ef1e12dfc8' ;;
        hermes-teleoperator-receiver-i686-unknown-linux-musl) printf '%s' 'ceb0e0c7154c97a6ad9de879d13f77f40c62eb3e6e845e91815f8de9b12f2100' ;;
        hermes-teleoperator-receiver-powerpc64le-unknown-linux-gnu) printf '%s' '2d0b155f09954f81a6706b4de80a0f97d97421239927b811f55138bd8d3c21fb' ;;
        hermes-teleoperator-receiver-riscv64gc-unknown-linux-musl) printf '%s' '540c333c328aa81685fdef0a9d685cd9ee887a07152b92bbd8d41863fc743211' ;;
        hermes-teleoperator-receiver-s390x-unknown-linux-gnu) printf '%s' '08dfd11de53e096ab088cc28da3b678cccf0cc64f8012040eee6ae398f761cc9' ;;
        hermes-teleoperator-receiver-x86_64-apple-darwin) printf '%s' 'fcc3542810d5fb2fdfa63b891f67016500901214c79db0536bfba8c93401edc7' ;;
        hermes-teleoperator-receiver-x86_64-unknown-linux-gnu) printf '%s' 'd1dbad5d26fd86ea91fa79c3f2327073a055737f3a26cadc4dcfa15719f8c359' ;;
        hermes-teleoperator-receiver-x86_64-unknown-linux-musl) printf '%s' 'd639b8f89309f0c362aefbc26a311d0f305fe5ed9de0cc636e84c1367cc16861' ;;
        *) return 1 ;;
    esac
}
# END PINNED RECEIVER DIGESTS
if [[ -z $binary ]]; then
    [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.-]+)?$ ]] || fail 'Use an exact release version'
    [[ $version == "$pinned_version" ]] || fail "This installer only authorizes release $pinned_version; refusing $version"
    case $(uname -m) in
        x86_64) arch=x86_64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *) fail 'Unsupported CPU; build from receiver/ source' ;;
    esac
    asset=hermes-teleoperator-receiver-$arch-unknown-linux-musl
    pinned=$(pinned_digest "$asset") || fail "No SHA256 is pinned in this installer for $asset; refusing a release download"
    if [[ -n $digest && $digest != "$pinned" ]]; then
        fail '--sha256 does not match the digest pinned in this installer; use --binary for independently verified bytes'
    fi
    digest=$pinned
    url=https://github.com/Tsurgcom/hermes-teleoperator/releases/download/$version
    curl -fsSL --proto '=https' --tlsv1.2 "$url/$asset" -o "$staging/receiver"
    curl -fsSL --proto '=https' --tlsv1.2 "$url/SHA256SUMS" -o "$staging/SHA256SUMS"
    # The downloaded manifest is not the trust root: it is only checked so a
    # release that rewrote both bytes and checksum is reported clearly.
    manifest_digest=''
    while read -r sum name; do
        if [[ $name == "$asset" || $name == "*$asset" ]]; then
            [[ -z $manifest_digest ]] || fail "Duplicate SHA256SUMS entry for $asset"
            manifest_digest=$sum
        fi
    done < "$staging/SHA256SUMS"
    [[ $manifest_digest =~ ^[a-f0-9]{64}$ ]] || fail "SHA256SUMS has no valid entry for $asset"
    [[ $manifest_digest == "$digest" ]] || fail 'SHA256SUMS disagrees with the digest pinned in this installer; refusing a rewritten release'
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
