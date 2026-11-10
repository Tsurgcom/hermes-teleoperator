"""Ask Hermes' own approval gate before any routed call leaves the gateway.

A remote call must never be *less* guarded than the same call on the gateway,
but the ``tool_execution`` middleware that performs the routing sits outside
``next_call``: Hermes runs dangerous-command approval, its guardrails and every
other plugin's ``pre_tool_call`` inside that callback, so a middleware that
answers a call itself inherits none of them. Until Hermes publishes an approval
seam for execution middleware (see the issue linked in the README), this module
asks the same gates core asks, through the same functions:

* ``tools.approval.check_all_command_guards`` — the dangerous-command gate the
  terminal tool calls (user deny rules, command floors, permanent/session
  allowlists, YOLO/approval-mode policy, CLI prompt, gateway card, smart
  approval, timeout). Called with ``env_type='ssh'``: the command runs with the
  SSH user's authority on another host, which is exactly what that backend name
  means to the gate, and it is *not* in the container fast-path set.
* ``tools.approval.request_tool_approval`` — the human-approval gate Hermes
  documents for plugin tool calls (session/permanent allowlist, once/session/
  always/deny, timeout fail-closed). Used for the remote mutations core's own
  host-side write/skill guards cannot see: a file write, a patch, a skill edit,
  a process control action, or an allowlisted MCP tool call.

Both are fail-closed here. A guard that cannot be reached (older/newer Hermes,
a raising call, a non-dict verdict) refuses the remote call instead of running
it unguarded — the plugin never trades a guard for availability.
"""
from dataclasses import dataclass

# The backend name the command gate evaluates: remote SSH authority. It is not a
# container backend, so the container fast path cannot approve a command for us.
REMOTE_ENV_TYPE = 'ssh'

# Mutations whose core guard lives on the *gateway* filesystem (sensitive-path
# denylist, protected instructions, stale-overwrite baselines, skill scan) and
# therefore cannot be evaluated by this plugin for a remote path. They take the
# human gate instead.
GATED_TOOLS = frozenset({'write_file', 'patch', 'skill_manage'})

# Read-style tools keep local parity: Hermes does not prompt for a local read
# either, so routing one adds no privilege. Secret-file redaction is a gateway
# concern documented in the README.
READ_TOOLS = frozenset({'read_file', 'search_files', 'skills_list', 'skill_view'})

# Process actions that change state. ``list``/``status``/``poll``/``wait`` are
# observation only and stay on local parity (core prompts for none of them).
MUTATING_PROCESS_ACTIONS = frozenset({'kill', 'write', 'submit'})
PROCESS_TOOLS = frozenset({'process', 'process_manage'})


@dataclass(frozen=True)
class Decision:
    """Outcome of one guard query. ``allowed`` is only ever True on a positive
    verdict from Hermes' own gate."""

    allowed: bool
    message: str = ''
    source: str = ''


def _blocked(message):
    return Decision(False, message, 'failed-closed')


def _verdict(verdict, source):
    """Normalise a Hermes approval verdict to a Decision.

    Anything that is not an explicit ``approved: True`` dict is a refusal: a
    missing, malformed or raising verdict must never read as consent.
    """
    if not isinstance(verdict, dict):
        return _blocked(f'Hermes {source} returned no verdict; refusing remote execution')
    if verdict.get('approved') is True:
        return Decision(True, source=source)
    message = verdict.get('message') or verdict.get('error') or verdict.get('description')
    if not isinstance(message, str) or not message:
        message = f'Hermes {source} did not approve this remote call'
    return Decision(False, message, source)


def target_key(tool_name, args):
    """Stable, human-meaningful grain for the ``[a]lways`` allowlist: one entry
    per remote directory (writes) or per tool (skills and MCP calls)."""
    if tool_name in {'write_file', 'patch'}:
        path = args.get('path') or args.get('file_path') or ''
        if not isinstance(path, str):
            path = ''
        directory = path.rsplit('/', 1)[0] if '/' in path else '.'
        return f'write:{directory or "/"}'
    if tool_name == 'skill_manage':
        name = args.get('name') or args.get('skill') or ''
        return f'skill:{name if isinstance(name, str) else ""}'
    return f'tool:{tool_name}'


def needs_gate(tool_name, args):
    """True when routing this call remotely requires a decision from Hermes."""
    if tool_name == 'terminal':
        return True
    if tool_name in GATED_TOOLS or tool_name.startswith('mcp__'):
        return True
    if tool_name in PROCESS_TOOLS:
        action = args.get('action')
        return isinstance(action, str) and action.lower() in MUTATING_PROCESS_ACTIONS
    return False


# Basenames Hermes treats as credential material on the gateway: reads of these
# are redacted locally (``agent.redact``), and the plugin cannot redact a remote
# receiver's bytes the same way. Refuse the direct read instead of shipping an
# unredacted credential into the conversation.
SECRET_BASENAMES = (
    '.env', '.netrc', '.pgpass', 'auth.json', 'credentials', 'id_rsa', 'id_dsa',
    'id_ecdsa', 'id_ed25519', 'config.yaml', '.bash_history', '.zsh_history',
)
SECRET_BASENAME_PREFIXES = ('.env.', 'config.yaml.good.', 'config.yaml.corrupt.')


def secret_read_refusal(tool_name, args):
    """Reason a routed read must not run, or '' when it may."""
    if tool_name not in {'read_file', 'search_files'}:
        return ''
    path = args.get('path') if isinstance(args, dict) else None
    if not isinstance(path, str) or not path:
        return ''
    name = path.rstrip('/').rsplit('/', 1)[-1].lower()
    if '/.ssh/' in path or path.rstrip('/').endswith('/.ssh'):
        return (f'Refusing to read {path!r} remotely: Hermes redacts SSH material on the '
                'gateway, and a remote read would return it unredacted. Use the gateway copy.')
    if name in SECRET_BASENAMES or name.startswith(SECRET_BASENAME_PREFIXES) or name.endswith(('.pem', '.key')):
        return (f'Refusing to read {path!r} remotely: Hermes redacts credential files on the '
                'gateway, and a remote read would return it unredacted. Run an approved '
                'terminal command if the remote value is genuinely needed.')
    return ''


def command_decision(command):
    """Ask the dangerous-command gate about one shell command."""
    if not isinstance(command, str) or not command.strip():
        return Decision(True, source='check_all_command_guards')
    try:
        from tools.approval import check_all_command_guards
        return _verdict(check_all_command_guards(command, REMOTE_ENV_TYPE),
                        'check_all_command_guards')
    except Exception as exc:  # fail closed: no guard, no remote execution
        return _blocked(f'Dangerous-command approval is unavailable ({exc}); '
                        'refusing to run this command remotely')


def human_decision(tool_name, args, machine):
    """Escalate a remote mutation to the human-approval gate.

    Session/turn/tool-call identity is already bound by Hermes around the tool
    call (``_approval_observability``), so the gate routes the prompt to the
    conversation that asked — no identity is inferred here.
    """
    reason = f'{tool_name} on machine {machine!r} ({target_key(tool_name, args)})'
    rule_key = f'teleoperator:{machine}:{target_key(tool_name, args)}'
    try:
        from tools.approval import request_tool_approval
        return _verdict(request_tool_approval(tool_name, reason, rule_key=rule_key),
                        'request_tool_approval')
    except Exception as exc:  # fail closed
        return _blocked(f'Hermes tool approval is unavailable ({exc}); '
                        'refusing to run this call remotely')


def decide(tool_name, args, *, machine):
    """Decide whether *tool_name* may execute on *machine*.

    Returns a :class:`Decision`; ``allowed`` is True only for an explicit
    positive verdict, or for a read/observation call that Hermes leaves ungated
    on the gateway too.
    """
    if not needs_gate(tool_name, args):
        if tool_name in READ_TOOLS:
            refusal = secret_read_refusal(tool_name, args)
            if refusal:
                return Decision(False, refusal, 'secret-file')
            return Decision(True, source='local-parity')
        if tool_name in PROCESS_TOOLS:
            return Decision(True, source='local-parity')
        return Decision(True, source='ungated')
    if tool_name == 'terminal':
        return command_decision(args.get('command'))
    return human_decision(tool_name, args, machine)
