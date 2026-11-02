---
name: hermes-teleoperator
description: Use when moving between hosts; route native tools safely.
version: 0.1.0
author: Hermes Teleoperator contributors
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [remote, ssh, machines, teleoperator]
---

# Hermes Teleoperator

Use one Hermes conversation across several computers. The conversation stays on the original host; native terminal, file and skill tools follow the selected machine. Remote MCP calls use that machine's configured servers and credentials.

## When to use

- A task needs repeated commands, file edits and remote skills/MCPs on another computer.
- You need to move between several computers without losing each one's shell state.
- You need explicit machine identity on results and conversation-scoped destinations.

## When not to use

- One or two remote commands: use ordinary SSH through `terminal`; no companion needed.
- Only copying files, large files or directories: use scp, sftp or rsync through `terminal`. Teleoperator transfers are regular files only, up to 8 MiB.
- An interactive remote shell: use SSH with a PTY.
- Another independent agent should own the work: use delegation or agent messaging instead.
- You need remote browser/desktop control, migrated memory, or Windows support: this version does not provide those.

## Prerequisites

The plugin must be enabled on the controller. Install the standalone Rust receiver on the destination; no Hermes/Python/Bun/Node is required there. Gateway machine entries use executable and teleoperator_home, not legacy python/hermes_home. Destinations are configured in the active profile's `$HERMES_HOME/teleoperator.json`. SSH key authentication and verified known-host entries must already work. Never disable host-key checks to make a connection succeed.

Remote MCP access requires explicit expose_mcp and mcp_servers entries in $TELEOPERATOR_HOME/config.json (default ~/.config/hermes-teleoperator). Only stdio MCP is supported; sampling/model/interactive callbacks are rejected. MCP executables may require their own language runtimes. If the tool is unavailable, use SSH rather than assuming teleportation is active. Read the project's README for installation; don't invent machine names or paths.

## How to use

1. Call `teleoperator(action="status")` to see configured machines and the current destination.
2. Call `teleoperator(action="switch", machine="<configured-name>")`. Check the returned active machine and hostname before doing work.
3. Use ordinary `terminal`, `read_file`, `write_file`, `patch` and `search_files`; they now run remotely. Call `skills_list` and `skill_view` to load that host's instructions rather than relying on the original local skill index.
4. Discover remote MCP tools with `teleoperator(action="search", query="<capability>")`, inspect with `action="describe", name="<returned-name>"`, then invoke with `action="call", name="<returned-name>", arguments={...}`.
5. Use `action="back"` to return to the previous machine, or `action="switch", machine="local"` to return home. Confirm with status when unsure.

Human shortcuts: `/teleport <machine>`, `/teleport local`, `/teleport back`, `/teleport status`, `/teleport disconnect`, `/teleport reconnect <machine>`.

For a small file, use `action="transfer"` with explicit `source_machine`, `source`, `destination_machine`, `destination`, and `move=false`. Destinations must not exist; parent directories must exist. Use `move=true` only when source deletion is intended, and stop concurrent writers first.

## Install and recover over SSH

- Prefer setup from the gateway's plugin Python environment: `python -m hermes_teleoperator.deploy user@host --probe`, then the same without `--probe` plus `--identity-file <admin-key>` and optional `--public-key-file <dedicated-key.pub>`. The helper probes the client's actual ABI/OS/libc, downloads the exact gateway-matching version onto the gateway, verifies checksums, transfers over administrative SSH and installs offline. It validates a staged handshake before switching the launcher. No client downloads or runtimes. Gateway/receiver versions must match exactly; upgrade/reload the gateway, deploy, then reconnect. Never use latest or an architecture guess. The repository's target-side install.sh remains a Linux-only alternative. Pass only a dedicated gateway public key with `--public-key` or `--public-key-file`; never send the private key. The installer verifies a standalone release binary SHA256 and prints launcher/config paths. It requires Bash, curl/coreutils and optional ssh-keygen, not Hermes or a language runtime. Published checksums are not publisher signatures; a trusted --sha256 can pin exact bytes. Do not suggest a release install until that release has binaries.
- Install as the intended SSH user, not via sudo. Check id -un, HOME and intended executable/config paths first. Direct root installation requires explicit intent and `--allow-root`; do not silently change user or reuse another user's profile. The installer rejects foreign-owned or group/other-writable installation/SSH ancestors; diagnose ownership and permissions rather than bypassing the guard.
- The enrolled key is forced to the companion and cannot run normal SSH repair commands. Keep separate administrative SSH access. Host-key verification is still required.
- There is no daemon/service to restart: `teleoperator(action="reconnect", machine="<name>")` starts a fresh companion over SSH. This resets its shell state; inspect ambiguous mutations before retrying.
- If reconnect fails, switch local, then use administrative SSH with `BatchMode=yes` and `StrictHostKeyChecking=yes` to check the target user and launcher. Re-run the standalone installer if the executable moved or needs upgrading. Do not use broad `pkill`, restart unrelated agents or relax host-key checks.
- To smoke-test transport using the dedicated key: pipe `{"id":"health","method":"ping"}` plus a newline into `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -i <gateway-key> <user@host> '<absolute-launcher>'`. Expect protocol 2 and runtime standalone-rust. Check identity, then reconnect and run a harmless pwd; never launch the removed Python companion.

For marketplace submission or re-pinning, load [catalog submission](references/catalog-submission.md).

## Pitfalls and verification

- A failed/disconnected remote remains selected. Never retry a possibly executed mutation blindly; inspect the outcome, then explicitly reconnect or switch local. There is no local fallback.
- Switching back preserves shell state only while the connection lives; reconnect creates a fresh shell. Identity cwd is a connection-time snapshot; terminal results show later changes.
- Browser tools, `execute_code`, cron and delegation are not teleported and are blocked while remote. Web search/extract and human clarification remain local. Switch local before using unsupported tools.
- The initial prompt, memory and project instructions are not migrated. Read relevant remote instructions explicitly. The standalone receiver does not load Hermes plugins, auth stores or native approval guards. SSH-user authority is not a sandbox; native Hermes feature parity is not implied. Inspect remote schemas; use ordinary SSH for unsupported options.
- Check `teleoperator.machine` on remote results. After a switch, verify with a harmless `pwd`/`hostname` command if needed; after a transfer, check its checksum/size result. Finish on the machine the task requires and state where any deliverable lives.
