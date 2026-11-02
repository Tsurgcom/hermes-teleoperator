# Hermes Teleoperator

![Hermes Teleoperator: gold pixel-art gateway linking a workstation and server over SSH. The model and conversation stay on the gateway.](assets/teleoperator-cover.webp)

One conversation. Several computers. Native tools wherever you land.

Teleoperator has two parts:

1. A Hermes plugin on the computer running your conversation.
2. A small companion started on demand over SSH on each destination.

The model and conversation stay on the original computer. Terminal, files and skills execute through the destination's own Hermes installation. Remote MCP tools are discovered and called there, using its credentials. This is tool-environment teleportation, not a second agent or a conversation copied between machines.

## How it fits together

![Architecture: the gateway keeps the model and conversation; SSH connects it to native Hermes tools on each destination.](assets/architecture.svg)

Each destination uses its own Hermes profile, filesystem and permitted MCP servers. Nothing opens a new public port.

## Switch without losing your place

![Switching: move from local to workstation to server; back reuses a live connection. A disconnected remote stays selected until explicit recovery.](assets/switching.svg)

`/teleport back` returns to the previous machine. `/teleport local` returns home. A failed remote call never silently becomes a local command.

## Working today

- `/teleport workstation`, `/teleport server`, `/teleport local`, `/teleport back`.
- Persistent SSH channels: switching back reuses each computer's shell environment and working directory while its connection remains open.
- Conversation-scoped selection: another chat doesn't inherit your destination.
- Native remote `terminal`, `read_file`, `write_file`, `patch`, `search_files`, `skills_list`, `skill_view`, `skill_manage`, and process tools.
- Remote MCP search, schema inspection and calls; explicitly granted per server.
- Binary file copies/moves between named computers, including local, with SHA-256 verification and no destination overwrite.
- Machine labels on remote results and a location reminder on each conversation turn.
- Strict SSH host-key checks, noninteractive key authentication, no agent forwarding, no listener beyond SSH.
- No automatic command retries and no fallback to local execution when a remote connection fails.

## Requirements

Python 3.11+, OpenSSH client on the controller, an SSH server on destinations, and a current Hermes installation on each computer. The selected remote Python must be the interpreter containing Hermes and its dependencies, not an unrelated system Python. Linux/POSIX is the tested target; Windows is not yet supported.

This uses Hermes' documented tool-execution middleware on the controller. The companion adapts internal native tool/MCP dispatcher APIs; Hermes upgrades may require adapter changes. Plugin validation reports those imports as compatibility warnings. The test suite is the compatibility check, not a claim of a stable remote-runtime API.

## Install

Keep a copy of this repository on each computer. Do not install arbitrary network listeners or copy credentials between computers.

Controller, native directory-plugin option (use your active profile's HERMES_HOME):

```sh
mkdir -p "$HERMES_HOME/plugins"
ln -s /absolute/path/to/hermes-teleoperator "$HERMES_HOME/plugins/hermes-teleoperator"
hermes plugins enable hermes-teleoperator
hermes plugins doctor /absolute/path/to/hermes-teleoperator --ci
```

Restart the Hermes conversation after enabling it. No core files are patched. The directory plugin needs no extra Python packages.

On each destination, install the companion package into the SAME environment that already contains Hermes:

```sh
/path/to/hermes/venv/bin/python -m pip install /absolute/path/to/hermes-teleoperator
```

Alternatively install the Python package on the controller too; it provides the `hermes_agent.plugins` entry point. Choose either directory discovery or pip plugin installation, not both.

Configure normal SSH first. Verify the destination's host-key fingerprint through a trusted channel, connect once with ordinary `ssh workstation`, and confirm key authentication works. Teleoperator deliberately refuses unknown/changed host keys; it never uses `StrictHostKeyChecking=no` or auto-enrols keys. SSH config aliases and ProxyJump work. The transport assumes POSIX shell quoting on the destination.

Create `$HERMES_HOME/teleoperator.json` on the controller, adapting `teleoperator.example.json`:

```json
{
  "machines": {
    "workstation": {
      "host": "workstation",
      "python": "/home/me/hermes-agent/venv/bin/python",
      "hermes_home": "/home/me/.hermes",
      "timeout": 120
    },
    "server": {
      "host": "me@server",
      "python": "/opt/hermes/venv/bin/python",
      "hermes_home": "/home/me/.hermes/profiles/work"
    }
  }
}
```

Optional target keys: `port`, `identity_file`, `known_hosts`. Use absolute paths. Settings are read lazily on first use; restart/reload the plugin after editing. Keys stay in SSH's own files/agent, not this JSON. `local` is reserved.

On a destination, remote MCP access is OFF by default. To grant selected MCP servers already configured in that destination's Hermes profile, create its own `$HERMES_HOME/teleoperator.json`:

```json
{"expose_mcp": ["github", "filesystem"]}
```

Do not put tokens here. Existing profile credentials stay on that computer. MCP tool trust/approval behavior remains Hermes-owned; an approval requiring an interactive remote UI may be denied because the companion has no human input channel. This version does not forward interactive approval prompts.

Each exposed server must also explicitly set `sampling.enabled: false` in its existing remote Hermes MCP configuration. Teleoperator refuses discovery otherwise, so an MCP server cannot request extra model inference through Hermes sampling. For example, use `hermes config set mcp_servers.github.sampling.enabled false` on the destination. Portable-plugin-only MCP definitions are not exposed by this version; the server must be present in the profile's merged `mcp_servers` configuration.

## Quick destination setup

Generate a dedicated key on the gateway (do not overwrite an existing key):

```sh
ssh-keygen -t ed25519 -f "$HOME/.ssh/teleoperator-workstation" -C hermes-teleoperator
```

The private key stays on the gateway. Give the installer only the `.pub` contents. Use an SSH agent if the key has a passphrase; Teleoperator authentication is noninteractive.

Run this on the destination as the intended SSH user. Replace `COMMIT_SHA` with a reviewed full commit SHA from this repository, and the Python path with the destination's existing Hermes interpreter:

```sh
REF=COMMIT_SHA
curl -fsSL "https://raw.githubusercontent.com/tsurgcom/hermes-teleoperator/$REF/install.sh" |
  bash -s -- --ref "$REF" --python /absolute/hermes/venv/bin/python \
    --public-key 'ssh-ed25519 AAAA... gateway-key' \
    --hermes-home "$HOME/.hermes"
```

The script itself uses stdin, so the public key is an argument (or use `--public-key-file /path/to/gateway.pub`), not a second stream piped into bash. Never pass a private key. For inspection first, download `install.sh`, read it, then invoke `bash install.sh` with the same arguments. This bootstrap trusts GitHub HTTPS and the pinned revision; signed release verification is not implemented.

The installer copies the companion under `~/.local/lib/hermes-teleoperator`, checks the existing Hermes interpreter, and prints configuration fields. It does not install Hermes, change its packages, enable remote plugins, touch MCP configuration, or start a daemon. Re-running updates the companion; disconnect active sessions before upgrading.

It adds the dedicated public key to `~/.ssh/authorized_keys` with `restrict` and a forced companion command. Ordinary shell commands using this key always launch the companion instead; use your separate administrative SSH access for setup/repair. This restricts connection entry, not the commands the companion can execute with the SSH user's authority. A key already authorised differently is rejected rather than silently left unrestricted. OpenSSH 7.2+ is required for `restrict`.

Set the controller's machine `python` field to the emitted launcher path (`/home/me/.local/lib/hermes-teleoperator/python`), `identity_file` to the gateway private-key path, and `host` to the same target user. Verify its SSH host fingerprint separately before connecting.

For agent-assisted setup, use existing verified administrative SSH access to run the same installer remotely. Do not use the restricted Teleoperator key for installation. Confirm `id -un`, `$HOME`, the Hermes interpreter and profile before writing. Do not pipe through `sudo`: the installer rejects `SUDO_USER`; deliberate direct root installs require `--allow-root`. A normal-user install cannot be reused as a root install by changing only `user@host`.

### Restart and recovery

There is no systemd service to restart. `/teleport reconnect workstation` closes the old channel and starts a fresh companion through SSH. Shell state is reset; do not replay a failed mutation without checking whether it ran.

If reconnect fails, switch local and diagnose with the separate administrative SSH credential:

```sh
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes me@workstation \
  'id -un; printf "%s\\n" "$HOME"; test -x "$HOME/.local/lib/hermes-teleoperator/python"'
```

A protocol smoke test also works with the restricted key (the forced command reads the request):

```sh
printf '%s\n' '{"id":"health","method":"ping"}' |
  ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -i "$HOME/.ssh/teleoperator-workstation" me@workstation \
    '/home/me/.local/lib/hermes-teleoperator/python -m hermes_teleoperator.companion'
```

A ping checks transport/identity only. After reconnect, check a harmless native tool such as `pwd` to verify Hermes dependencies too. If the interpreter moved or Hermes changed, disconnect, rerun the installer through administrative SSH with the correct interpreter/profile, then reconnect. Never use broad `pkill python`, restart unrelated Hermes agents, or disable host-key checking.

To uninstall: disconnect, remove only this key's `hermes-teleoperator` line from `authorized_keys`, then delete `~/.local/lib/hermes-teleoperator` and its controller machine entry. Keep unrelated keys, Hermes and profiles intact.

## Use

```text
/teleport workstation
/teleport status
/teleport server
/teleport back
/teleport local
/teleport disconnect
/teleport reconnect workstation
```

The `teleoperator` model tool also exposes:

- `action=status|switch|back|reconnect|disconnect`
- `action=search, query="github issue"` — discover the selected machine's exposed native/MCP tools.
- `action=describe, name="mcp__github__get_issue"` — inspect its exact schema.
- `action=call, name="...", arguments={...}` — execute it remotely.
- Optional `machine` selects a named remote for search/describe/call without changing the active destination.

Native file/terminal/skill calls follow the active computer automatically. Tools a remote machine has but the local model doesn't know about use the stable search/describe/call gateway; tool schemas and past conversation context are not swapped mid-session.

Transfer example:

```json
{
  "action": "transfer",
  "source_machine": "workstation",
  "source": "/home/me/report.csv",
  "destination_machine": "local",
  "destination": "/home/me/downloads/report.csv",
  "move": false
}
```

Parent directories must exist. A destination is created atomically and must not already exist. `move=true` removes the source only after a verified copy and a second source-hash check. A failed/ambiguous call is never automatically repeated. Transfers currently support regular files up to 8 MiB, not directories or symlinks; use scp/sftp for larger trees. No metadata/permission preservation. Transfers are not atomic across two computers, and files changing concurrently are not supported: stop writers before a move.

## Location and boundaries

- `status` reports the selected machine, previous machine, configured/connected machines, and connection identity. The identity's cwd is a connection-time snapshot; terminal results report subsequent cwd changes.
- Every remote response includes `teleoperator.machine` and its hostname.
- Disconnect does NOT select local. It leaves the remote selected and returns errors until explicit reconnect or `/teleport local`.
- Reconnect creates a new companion and shell; it does not restore the previous connection's in-memory environment.
- The initial model prompt and injected local skill index are not rewritten. `skills_list`/`skill_view` read the remote catalog after switching; retrieve relevant remote instructions instead of relying on the initial local index. Remote AGENTS.md is not automatically injected.
- Local memory, browser sessions, calendar/email connectors, cron, delegation, and model execution are not migrated.
- Web search/extract and human clarification remain local. Unsupported tools (including `execute_code` and browser/computer tools) are blocked while remote rather than silently run on the controller. Switch local to use them.
- Native tool guards remain in the execution paths, but this is not a sandbox. SSH grants the destination user's full OS authority, and arbitrary terminal commands can access its data. Use an appropriately scoped SSH account.
- The companion itself exposes only native file/shell/skill/process tools and explicitly permitted MCP servers. It does not launch a conversation agent. Importing Hermes' native dispatcher also loads plugins enabled in the destination profile: those plugins can run code or register hooks, so use a dedicated remote profile if that is undesirable. Exposed MCP servers must have sampling disabled. Skill loading retains native preprocessing/dependency behavior and is not guaranteed read-only.
- Restarts begin local; machine selection lives only in the conversation's current process. The controller's `back` history is one previous machine, not a navigation stack.

## Verify

No test dependencies are needed:

```sh
python3 -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

For the full integration suite, point at a real Hermes environment/source checkout:

```sh
TELEOPERATOR_HERMES_PYTHON=/path/to/hermes/venv/bin/python \
TELEOPERATOR_HERMES_SOURCE=/path/to/hermes/source \
python3 -m unittest discover -s tests -v
```

The SSH integration test needs root and OpenSSH to start an isolated loopback-only sshd on a random port. It creates temporary host/client keys and two isolated remote profiles, invokes the real Hermes dispatcher and a deliberately tiny test MCP server over SSH, verifies binary transfer and wrong-key rejection, then removes the fixture and stops sshd. It does not modify production SSH keys/config, real Hermes profiles, or existing MCP configuration. Two profiles on one loopback host test isolation and transport; a second physical computer has not yet been tested.

## Layout

- `hermes_teleoperator/plugin.py`: native plugin, slash command and middleware.
- `controller.py`: per-conversation destinations and transfer orchestration.
- `transport.py`: strict SSH persistent framed RPC.
- `companion.py`: remote stdio service.
- `runtime.py`: native Hermes integration boundary.
- `files.py`: verified small-file transfer.
- `tests/`: unit, subprocess, native runtime and real SSH/MCP integration tests.
