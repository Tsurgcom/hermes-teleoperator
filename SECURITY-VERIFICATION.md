# Security and install validation

## Changes

- Gateway-local file transfers now require a configured `transfer_dir`; every resolved source/destination must stay inside it, `$HERMES_HOME` and `~/.ssh` are refused (including through symlinks), and deleting a gateway-local source with `move=true` requires `allow_local_delete: true`. Remote receiver endpoints and limits are unchanged.
- Receiver downloads are verified against per-asset SHA256 digests committed in `hermes_teleoperator/version.py` (gateway) and embedded in `install.sh` (standalone installer) for the exact release tag. `SHA256SUMS` is still downloaded and cross-checked, but it is no longer the trust root, so a compromised release cannot rewrite both bytes and checksum and pass.
- Release assets are built on GitHub-hosted runners and attested with `actions/attest-build-provenance`; the publish job refuses any asset that differs from the source pins and attests the published `SHA256SUMS`. `RELEASING.md` documents the catalog `sha:` / tag / digest relationship.
- Both installers now leave SSH authorization state entirely untouched. Supplying a dedicated public key generates a restricted forced-command entry for explicit administrator review and installation; it does not authorize the key.
- Installer regressions verify absent/populated SSH directories, nested entries, symlinks, FIFO files, metadata preservation, invalid keys and unsafe forced-command paths.
- The unprivileged sudo-rejection test uses subprocess user/group settings rather than privilege-changing callbacks, and verifies the child UID.
- Plugin conversation identity uses documented callback keywords only. No private Hermes runtime imports or core patches remain.
- CI runs the official Hermes scanner at a pinned upstream revision against the full clean source tree before compiling anything. Scanner rules are not changed or suppressed.

## Executed locally

- `hermes plugins validate <clean tracked-source copy> --install-deps --json`: `ok: true`, `security scan: safe`, `warnings: []`. Includes all tracked tests, installer, source and documentation, not a curated runtime subset.
- Python: 105 tests passed with ResourceWarning treated as errors, 15 skipped (receiver binary and opt-in live-release checks). Includes loopback OpenSSH, gateway-led offline deployment, installer release-pin failure modes and clean-chroot receiver execution.
- Live release checks: the published `v1.1.2` `SHA256SUMS` matches the committed pin table for all 13 assets; `deploy.download_release` and a real `install.sh` release install both fetched and verified the published `x86_64-unknown-linux-musl` asset against the embedded pin (`d639b8f8...`).
- Rust: 22 tests passed (12 unit, 10 protocol integration).
- Additional real OpenSSH run with actual installed Hermes bootstrapped interpreter: plugin discovery, registered tool, remote middleware, skills and terminal routing passed.
- `bash -n install.sh`, `python scripts/sync_receiver_digests.py --check` and `git diff --check` passed.

## Distribution and limits

Gateway/receiver protocol and version remain 1.1.2. The receiver source and existing v1.1.2 binary assets are unchanged; this is a source-only installer/gateway security correction. Use the reviewed corrected source commit for gateway installation, not the original release tag's installer. `v1.1.2` was tagged before source-pinned digests existed; its published manifest matches the pins now committed here. The catalog must pin the commit containing this table, and the next release cut with this process must tag that same commit. Do not move the existing tag.

A development tree containing ignored Rust build output can still trigger binary-file warnings: validate a fresh clone or `git archive` tree, exactly as catalog admission does. Never ship build output in the plugin repository.

Linux real execution was tested; no new macOS hardware verification is claimed. The receiver still has the SSH user's OS authority. Gateway-side, every routed call now passes Hermes' dangerous-command gate (`terminal`) or human-approval gate (writes, skill edits, state-changing process actions, `mcp__*`) and fails closed when the gate is refused or unreachable; other plugins' `pre_tool_call` hooks and the agent's tool guardrails remain out of reach of execution middleware (hermes-agent#152963). The marketplace is human-reviewed; passing validation is not admission or maintainer approval.
