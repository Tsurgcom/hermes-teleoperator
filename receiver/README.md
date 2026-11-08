# Standalone Rust receiver

SSH stdio companion for Hermes Teleoperator. The remote machine needs only an
SSH account, `/bin/bash` for `terminal`, and this downloaded binary. No Python,
Hermes installation, model runtime, or network listener is used.

## Build

```sh
# Toolchain: Rust 1.74+ (isolated CARGO_HOME/RUSTUP_HOME supported)
rustup target add x86_64-unknown-linux-musl
cargo test --locked
cargo build --release --target x86_64-unknown-linux-musl --locked
# -> target/x86_64-unknown-linux-musl/release/hermes-teleoperator-receiver
```

The musl build is statically linked (`static-pie`), so the target needs no libc
runtime beyond the kernel. A glibc build (`cargo build --release`) also works but
requires a compatible target libc.

## Protocol

Newline-delimited JSON over stdin/stdout. One request per line, one response per
line. Maximum frame is 16 MiB (request or response); oversized requests produce
`{"id":null,"error":{"type":"FrameTooLarge",...}}` and terminate the session.

Request: `{"id":<any>,"method":"<name>","params":{...}}`
Response: `{"id":<same>,"result":<value>}` or
`{"id":<same>,"error":{"type":"ReceiverError","message":"..."}}`.

Unknown methods, unknown arguments, wrong argument types, unsupported enum
values, and missing required arguments are errors; nothing is ignored silently.

Methods: `ping`, `tool`, `search`, `describe`, `file_get`, `file_put`,
`file_remove`.

- `ping` reports `protocol: 2`, `runtime: "standalone-rust"`, hostname, home,
  cwd, pid, platform, `teleoperator_home`, and explicit capabilities:
  `terminal`, `files`, `skills`, `mcp_stdio`, `background_processes` are true;
  `pty`, `document_extraction`, `native_hermes_guards` are false. `authority`
  states that the SSH user is the authority and native Hermes approval/policy
  guards are absent.
- `tool` takes `{name, arguments}`. `arguments` must be an object and is
  validated against the published schema.
- `search` takes `{query}` and returns `{"tools":[...]}` function schemas.
- `describe` takes `{name}` and returns one `{type:"function",function:{...}}`
  schema.

## Tools

| Tool | Behaviour |
| --- | --- |
| `read_file` | UTF-8 text with `N\|` line numbers, `offset` (1-indexed), `limit` ≤ 2000, 100 KB content cap, `truncated`/`next_offset`. NUL bytes or invalid UTF-8 return an error: no PDF/Office/image extraction. |
| `write_file` | Creates or atomically replaces a regular UTF-8 file (≤ 8 MiB). Refuses symlinks and non-regular files. |
| `patch` | Literal exact replacement; refuses zero or multiple matches unless `replace_all: true`; nothing written on failure. |
| `search_files` | Rust regex content search or `*`/`?` filename globs. `target`, `path`, `file_glob`, `limit` ≤ 1000, `offset`, `order=discovery`, `output_mode` (`content`/`files_only`/`count`), `context` ≤ 50. Skips binary files (reported under `skipped`), does not follow symlinks, bounded to 100k entries / 512 MiB / 100k matches. |
| `terminal` | Persistent real `bash --noprofile --norc`. cwd and exported shell variables persist between calls on the connection. Commands run non-interactively with stdin from `/dev/null`. Foreground `workdir` is rejected before running the command: use an explicit `cd` in the command (which changes the persistent shell), or `background: true` with `workdir`. Background `workdir` is job-scoped; relative paths resolve against the receiver's cwd, not the persistent shell cwd. The background response reports the physical starting cwd, not later changes made by the command. `timeout` 1..=1800s kills the whole process group and resets the shell on expiry; output capped at 1 MiB per call with `truncated`. `background: true` starts a detached pipe job. `pty: true` is rejected (unsupported). |
| `process` / `process_manage` | `list`, `status` (no output consumption), `poll`, `wait` (`timeout`), `kill` (whole process group), `write` (raw stdin), `submit` (stdin + newline). Jobs inherit the shell's exported environment and, unless `workdir` is specified, its cwd at start; they have piped stdin. |
| `skills_list` | Reads `$TELEOPERATOR_HOME/skills` plus `~/.hermes/skills` when it exists. Optional `category` prefix filter. No Hermes required. |
| `skill_view` | `SKILL.md` or one supporting file under `references/`, `templates/`, `scripts/`, `assets/`; no symlinks. |
| `skill_manage` | `{operations:[{action:"create"|"patch"|"write_file"|"remove_file",...}]}`. Only `$TELEOPERATOR_HOME/skills` is writable; `~/.hermes/skills` fallback is read-only. All operations are validated and staged first, then applied atomically with rollback on failure. `delete` and full replacement are unsupported. |

Binary transfer (`file_get`/`file_put`/`file_remove`) keeps the Python `files.py`
protocol: base64 + SHA-256, 8 MiB limit, atomic no-overwrite publish, regular
non-symlink files only, and checksum-guarded removal.

## MCP

`mcp__*` tools come from `src/mcp.rs` (`McpManager::new(config_home)`), reading
`$TELEOPERATOR_HOME/config.json` (`expose_mcp` allowlist + `mcp_servers`). With no
config, no MCP tools are exposed. The module owns its own tests.

## Process lifetime

EOF on stdin drops the receiver, which kills the persistent shell's process
group, every background job's process group, and lets `McpManager::close()`
reap MCP servers. No listener is opened.

## Deliberate limitations

Not full Hermes parity: no PTY, no binary/PDF/Office document extraction, no
image handling, no notification hooks, no Hermes approval/policy guards, no
skill deletion, and `search_files` only supports deterministic discovery order.
