# Receiver language decision

Gateway: keep Python. It runs inside Hermes already; rewriting it cannot remove a runtime that is already required there.

Receiver: Rust. The requirement is a small on-demand process on arbitrary SSH destinations without installing another agent or managing a language environment.

| Choice | Target requirements | Operational trade-off |
|---|---|---|
| Python stdlib | Python interpreter | Simplest maintenance next to gateway; Hermes is not inherently needed. Still depends on interpreter/version availability. Frozen executables bundle an interpreter and require per-platform builds. |
| Bun standalone | Bundled JS runtime in executable | Convenient TS and subprocess/JSON APIs, but the runtime still ships with every receiver. No benefit here from a web stack or JS ecosystem. |
| Rust musl | Binary, Bash for shell tools | No interpreter on destination; build complexity and explicit process/MCP implementation move into development/CI. |

This is not a claim that Python caused the original Hermes dependency: importing Hermes' dispatcher was the architectural mistake. Any of these languages could implement a genuinely standalone receiver. Rust is selected for distribution and low idle overhead, not because a different language automatically removes coupling.

No three-way performance benchmark is claimed. Measurements of the actual Rust receiver must name the build, host, included child processes and warm/cold conditions. A receiver is on demand, not an always-on daemon; keep live connections only when state reuse matters. MCP server memory is separate and can dwarf the receiver.

Scope: Linux x86_64 and aarch64 static musl release binaries, gateway Python plugin, direct stdio MCP, local skills. Unsupported native Hermes behaviors return explicit errors instead of importing its runtime as a fallback.
