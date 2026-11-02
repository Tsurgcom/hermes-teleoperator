# Hermes Teleoperator

One conversation. Several computers. Native tools wherever you land.

Teleoperator has two parts:

1. A Hermes plugin on the computer running your conversation.
2. A small companion started on demand over SSH on each destination.

The model and conversation stay on the original computer. Terminal, files and skills execute through the destination's own Hermes installation. Remote MCP tools are discovered and called there, using its credentials. This is tool-environment teleportation, not a second agent or a conversation copied between machines.

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
- The companion itself exposes only native file/shell/skill/process tools and explicitly permitted MCP servers. It does not spawn an LLM or load arbitrary user plugins.
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
