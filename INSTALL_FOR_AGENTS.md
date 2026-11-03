# Hermes Teleoperator: installation guide for agents

You are the agent receiving a user's request to set up Teleoperator. Read this guide before changing anything. Install the gateway plugin first, load its accompanying `hermes-teleoperator` skill, then guide the user through connecting each destination. Do not install Hermes on a destination just to receive connections.

## What goes where

- **Gateway:** the computer running the Hermes process that owns this conversation. Install the Python plugin and accompanying skill here, in the user's intended Hermes profile. The model, conversation and credentials remain here. A Desktop window may be connected to a different computer: its backend, not necessarily the laptop displaying the window, is the gateway.
- **Destination / receiver:** the computer whose shell and files the user wants to access. It needs an SSH server, Bash, ordinary OS utilities and the standalone receiver binary. It does not need Hermes, Python, Node, Bun, a model account or an API subscription. Explicitly configured MCP servers may need their own dependencies.
- **Transport:** verified SSH stdio. There is no Teleoperator server port, web UI, pairing code or receiver daemon to expose/start.

The receiver has the authority of its SSH user; a restricted SSH entry key does not sandbox commands executed through it. Windows receivers are not supported. Linux and macOS support depends on the detected CPU/userland ABI and OS minimums; use the probe rather than guessing from a device name.

## 1. Establish scope and ask only what is missing

Confirm:

1. Which gateway/backend and Hermes profile should own the plugin? Resolve the active profile's actual `HERMES_HOME`; do not assume `~/.hermes` or another bot's home.
2. Which destination(s), SSH username/alias and port, and what short machine names should appear in `/teleport`? `local` is reserved.
3. Does separate administrative SSH access already work from the gateway? If not, guide the user through enabling SSH/key authentication and verifying the host fingerprint through a trusted channel. Do not disable host-key checking or treat an unverified `ssh-keyscan` result as identity verification.
4. Does the user want you to deploy over existing administrative SSH, or need commands to run themselves? Explain which computer each command runs on. Do not request private keys, passwords or passphrases in chat; use the environment's approved credential/interactive flow if needed.

A non-root destination account is the default. Direct root installation needs explicit user intent and `--allow-root`. Do not run receiver installation through sudo or silently change the destination user. Ask before installing missing OS packages or enabling services.

## 2. Install the gateway plugin first

Read the repository README and the installed Hermes CLI's help. Load the `hermes-agent` skill if available. Check `hermes --version`, `hermes plugins install --help` and existing plugins before installing: do not create a second discovery path for an already-installed copy.

For the corrected **v1.1.2 gateway/installer source** (safe install scan), the immutable commit is:

```text
c3ae955c9888a8b4397fff3eda8672aaafe83c2f
```

Run on the **gateway**, with the intended profile selected (use `hermes -p <profile> ...` when appropriate):

```sh
hermes plugins install Tsurgcom/hermes-teleoperator \
  --ref c3ae955c9888a8b4397fff3eda8672aaafe83c2f --enable
hermes plugins list
```

This guide's source pin includes the scan fixes; receiver source/version is unchanged and the corresponding binary assets exist in release `v1.1.2`. Do not use the original v1.1.2 source tag for the gateway or installer: it predates the scan fixes. Do not mix `main` source, an arbitrary receiver release and an older installed gateway. For a future release, verify the published tag's full commit SHA, version metadata and assets together before choosing a new pin. Do not move tags or use `latest` to conceal a mismatch.

Use the plugin directory reported by installation/listing as `PLUGIN_DIR`. Validate it with:

```sh
hermes plugins doctor /absolute/path/to/installed/hermes-teleoperator --ci
```

Do not patch Hermes core, install a second pip entry-point copy, or globally install into the system Python just to make imports work. The README's directory/symlink method is an alternative if the installed Hermes lacks Git plugin installation, not an extra step to stack on top. Confirm middleware support with `doctor`; the manifest requires Hermes >=0.21.3, but a version number alone is not proof the runtime contracts work.

### Make the accompanying skill available

Load `hermes-teleoperator` with `skill_view`. If it is not discoverable, inspect the installed plugin's `skills/hermes-teleoperator/SKILL.md` and its `references/` directory. Install that whole skill directory into the **intended profile's** skill directory using the supported local skill workflow (or copy it there if that is how this installation manages local skills). Do not overwrite a pre-existing modified skill or another profile's skill without asking. Read the bundled skill directly to continue setup if a fresh skill index requires a new conversation.

Skill discovery and plugin activation are separate checks: seeing the skill does not prove the `teleoperator` tool or its middleware is active.

### Restart checkpoint

Tell the user what has been installed and **prompt them to restart the gateway when needed**, then reopen/resume the conversation so the plugin, middleware and tool/skill index load together. Do not silently restart the process hosting this conversation or unrelated bots. Use the actual installation's lifecycle (service, container or Desktop-managed gateway); inspect `hermes gateway --help`/status rather than inventing a service name. A Desktop window reload alone may not restart its backend.

Before the restart, give the user a short continuation note: active profile, plugin path/version, completed steps and next action. Afterward verify plugin health and the actual `teleoperator` tool's availability. If the tool is absent, resolve activation before claiming installation is complete.

## 3. Prepare SSH and a dedicated Teleoperator key

Keep separate administrative SSH access for setup and repair. The dedicated Teleoperator key is enrolled with a forced receiver command; it cannot run a normal deployment shell.

Generate a new dedicated ED25519 key **on the gateway** if needed, using a unique path and without overwriting an existing key:

```sh
ssh-keygen -t ed25519 -f "$HOME/.ssh/teleoperator-workstation"
```

Choose passphrase/SSH-agent handling with the user; runtime connections use `BatchMode=yes`, so an unattended passphrase prompt will not work. Only `teleoperator-workstation.pub` is sent to the destination. The private key stays on the gateway. Do not enroll an existing general-purpose administrative key as a forced Teleoperator key.

## 4. Probe and deploy from the gateway

Use the **gateway's Hermes Python environment** and installed plugin source. Do not assume the bare `python` command belongs to Hermes. Locate/verify the interpreter used by the running Hermes installation; below, `HERMES_PYTHON` means its absolute interpreter path and `PLUGIN_DIR` means the installed plugin directory, not literal placeholders to execute unchanged.

Running from `PLUGIN_DIR` makes directory-installed plugin imports available without installing a duplicate package:

```sh
cd "$PLUGIN_DIR"
"$HERMES_PYTHON" -c 'from hermes_teleoperator.version import VERSION; print(VERSION)'
"$HERMES_PYTHON" -m hermes_teleoperator.deploy --help
"$HERMES_PYTHON" -m hermes_teleoperator.deploy me@workstation \
  --identity-file "$HOME/.ssh/workstation-admin" --probe
```

The probe is read-only: confirm its destination identity, home, exact version and selected asset with the user's intended machine. Use `--port N` and `--known-hosts /absolute/path` when needed; pass the actual port explicitly, including when using an SSH alias. Pass the same connection options during deployment. Root also requires `--allow-root` for the probe.

Then deploy, still **on the gateway**:

```sh
"$HERMES_PYTHON" -m hermes_teleoperator.deploy me@workstation \
  --identity-file "$HOME/.ssh/workstation-admin" \
  --public-key-file "$HOME/.ssh/teleoperator-workstation.pub"
```

The helper chooses the package from the **destination's** OS/userland ABI, prefers static musl on supported Linux architectures, and checks dynamic-only glibc floors/macOS minimums. It downloads the exact gateway-matching release and `SHA256SUMS` **onto the gateway**, verifies bytes, transfers them over SSH, and installs offline on the destination. It checks staged version/protocol/identity before switching the live launcher, and checks the installed launcher afterward. No destination network fetch or language runtime is required. Release checksums are not independent publisher signatures.

Read and retain the returned JSON: `version`, `asset`, `identity`, `executable`, `teleoperator_home`, `handshake` and optional `ssh_authorization_entry`. Deployment does **not** read or modify SSH authorization files or write the gateway's machine configuration. Review `ssh_authorization_entry` with the user; a destination administrator must explicitly authorize that exact dedicated-key entry before the dedicated SSH connection can work. Never replace an existing key or silently enroll it. Ambiguous ABI, missing assets and unsafe installation permissions are errors to investigate, not reasons to bypass checks. If entry generation rejects a launcher path that needs shell quoting, choose a supported account/path with the user rather than weakening forced-command quoting.

If the user cannot provide administrative SSH, stop here and explain what access is missing. The README's target-side `install.sh` is a Linux-only alternative, not this preferred gateway-download/offline-transfer flow. Do not quietly substitute a client-side `curl | bash` command. An offline/manual handoff requires an explicit user-agreed procedure; the deployment helper itself needs administrative SSH.

## 5. Register the destination in the active gateway profile

Read any existing `$HERMES_HOME/teleoperator.json` first. Preserve unrelated machines and settings; add/merge only the agreed entry. Use **actual absolute paths** returned by deployment and the dedicated key's gateway path. Example only:

```json
{
  "machines": {
    "workstation": {
      "host": "me@workstation",
      "port": 22,
      "executable": "/home/me/.local/bin/hermes-teleoperator-receiver",
      "teleoperator_home": "/home/me/.config/hermes-teleoperator",
      "identity_file": "/home/gateway/.ssh/teleoperator-workstation",
      "timeout": 120
    }
  }
}
```

Add `known_hosts` if using a non-default verified file. Store paths, not key contents or passwords, in this JSON. Use `executable`/`teleoperator_home`, never the removed `python`/`hermes_home` receiver configuration. Repeat probe/deployment per destination; never reuse another machine's returned identity or paths blindly.

Configuration is cached once the plugin's controller is first used. If you queried Teleoperator before adding machines, request the necessary gateway restart/new runtime and conversation reload before testing; a receiver reconnect alone does not reload that configuration. Preserve a continuation note as above.

## 6. Verify end to end, then explain normal use

1. On the gateway, smoke-test SSH using the **dedicated** key and installed launcher. Reuse the verified port/known-hosts settings. Send a newline-terminated `{"id":"health","method":"ping"}` request. Expect `protocol: 2`, `runtime: "standalone-rust"`, and a `version` exactly equal to the gateway's version. Confirm hostname/home/user identity against the probe. Do not call a checksum-only success a working connection.
2. Call `teleoperator(action="status")`, then `teleoperator(action="switch", machine="workstation")`. Confirm the returned machine and hostname before running anything remotely.
3. Use a harmless remote terminal command such as `pwd; hostname; id -un`. Check the result's machine attribution. Run `skills_list` to see the receiver's actual skill catalog; an empty catalog is valid on a fresh installation.
4. Return with `teleoperator(action="switch", machine="local")` and verify status. For multiple destinations, test each separately. Tell the user where you left the conversation.

Human commands are `/teleport workstation`, `/teleport back`, `/teleport local`, `/teleport status` and `/teleport reconnect workstation`. Switching preserves each live shell; reconnect loses that shell's state. A disconnected remote remains selected and fails closed: **never assume a failed remote command ran locally**.

Receiver skills live in `<teleoperator_home>/skills/<name>/SKILL.md`. Remote MCP is off by default; only configure explicitly requested stdio servers/allowlists in the receiver's `config.json`. Do not copy gateway secrets, profiles or all MCP servers to the destination. The receiver does not automatically load a destination Hermes installation's skills, plugins, credentials or approval guards.

The model, memory and initial instructions stay on the gateway; load remote project/skill instructions explicitly. Browser/desktop tools, delegation, cron and connectors are not teleported. Switch local before unsupported operations. For large files/directories use scp/sftp/rsync; Teleoperator file transfers are regular files up to 8 MiB without overwriting existing destinations.

## Recovery and completion

There is no receiver service to restart. Reconnect launches it through SSH. Use separate administrative SSH for repair; never disable strict host-key checks, broadly kill processes, or automatically replay a mutation after an ambiguous disconnect. Inspect any staging path named by a cleanup warning before removing it.

For upgrades, disconnect live receiver sessions, upgrade/reload the gateway, deploy from that same plugin environment, then reconnect. Missing or mismatched receiver versions are rejected before selection. Do not bypass parity or silently downgrade to an available old asset.

Finish with the installed gateway version/profile, destination machine names and verified identities, configuration/launcher paths, tests actually run, remaining blockers, restart requirements and current selection. If a restart/access step is pending, say so plainly: “installed” is not “active and verified.”
