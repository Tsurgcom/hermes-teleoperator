#!/usr/bin/env bash
# Standalone receiver install. No Python, Hermes, Bun or Rust toolchain on target.
set -euo pipefail
fail() { printf 'Installation failed: %s\n' "$*" >&2; exit 1; }
version='v1.2.1'
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
        --help) printf '%s\n' 'Usage: bash install.sh [--version v1.2.1] [--public-key "ssh-ed25519 ..."] [--teleoperator-home /absolute/path] [--allow-root]' 'Release downloads are verified against SHA256 digests embedded in this script for the exact tag; SHA256SUMS is only cross-checked.' 'Offline: --binary /path/to/receiver --sha256 <trusted SHA256>'; exit ;;
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
        hermes-teleoperator-receiver-aarch64-apple-darwin) printf '%s' '6cb7f126fc9d08a5457b868534267c7cac1098ac0fcea50224e2db1948d9fa41' ;;
        hermes-teleoperator-receiver-aarch64-unknown-linux-gnu) printf '%s' 'f4f298474b491a587c4be3e201318be8c23c12effb3baa3949b084b281c4e704' ;;
        hermes-teleoperator-receiver-aarch64-unknown-linux-musl) printf '%s' '1483f57d4e54410d6b13964a62d9563cee8efffc2b5d08dc0fe29f3643e2bfdd' ;;
        hermes-teleoperator-receiver-armv7-unknown-linux-gnueabihf) printf '%s' '63ba98b87292f992b8ac2fca9b83494f5073bcc2234dd798da9d1338e8e914a6' ;;
        hermes-teleoperator-receiver-armv7-unknown-linux-musleabihf) printf '%s' '3230e864e7be4e1d938619d607953e1d02fa44a873c204af4a99149fb3cd3b38' ;;
        hermes-teleoperator-receiver-i686-unknown-linux-gnu) printf '%s' '34b4d5297340adf95560797b9bb853efc09f1f4420cdcf3a1b4c6ac54606173b' ;;
        hermes-teleoperator-receiver-i686-unknown-linux-musl) printf '%s' 'b4f6b37aa8a6e5c547651db441346bb0a00aafa2f502f97b85db2321c5ea93b6' ;;
        hermes-teleoperator-receiver-powerpc64le-unknown-linux-gnu) printf '%s' 'dbf82b36e5cad55bbd696a7e0086bfe66360e081ab737eaf6a7ec5ea3a888a9a' ;;
        hermes-teleoperator-receiver-riscv64gc-unknown-linux-musl) printf '%s' 'b8e9518acc40987ef998786652571189fb6ed84cbb718291962f519ddf5b696c' ;;
        hermes-teleoperator-receiver-s390x-unknown-linux-gnu) printf '%s' '731b57110263d3b0e9a4db00108e2dec51bd315bdc79d8d1af9203499f6a16fd' ;;
        hermes-teleoperator-receiver-x86_64-apple-darwin) printf '%s' '8f3180c6f3f03533c079e556d39531158a2a1c965fe0d8889fea25bb0c930e6a' ;;
        hermes-teleoperator-receiver-x86_64-unknown-linux-gnu) printf '%s' '7223d86340447fb7d378021a10e324c95db79ade2be87e4dce2f937cb1b34786' ;;
        hermes-teleoperator-receiver-x86_64-unknown-linux-musl) printf '%s' '2013038cc40dd0068a149a02bf53ba5240bfab9e6a086a9e62717136e6919110' ;;
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
