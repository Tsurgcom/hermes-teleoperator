"""Conversation-local machine selection. Never fall back to local execution."""
from dataclasses import dataclass, field
import json
import threading

from . import guards
from .transport import RpcProcess, ssh_argv, ssh_environment
from .version import VERSION

NATIVE_TOOLS = {
    'terminal', 'read_file', 'write_file', 'patch', 'search_files',
    'skills_list', 'skill_view', 'skill_manage', 'process', 'process_manage',
}
LOCAL_TOOLS = {'teleoperator', 'clarify', 'web_search', 'web_extract'}


@dataclass
class Session:
    active: str = 'local'
    previous: str = 'local'
    peers: dict = field(default_factory=dict)
    disconnected: set = field(default_factory=set)
    identities: dict = field(default_factory=dict)
    lock: object = field(default_factory=threading.RLock)


class Controller:
    def __init__(self, targets, factory=None, guard=None, transfer_dir=None, allow_local_delete=False):
        self.targets = targets
        self.factory = factory or (
            lambda target: RpcProcess(ssh_argv(target), timeout=target.get('timeout', 120),
                                      env=ssh_environment()))
        # Hermes' approval gate. Injectable for tests; never injectable to "allow
        # everything" in production code (see guards.decide).
        self.guard = guard or guards.decide
        self.transfer_dir = transfer_dir
        self.allow_local_delete = allow_local_delete is True
        self.sessions = {}
        self.lock = threading.RLock()

    def _session(self, key):
        if not key:
            raise ValueError('No conversation identity. Cannot choose a machine.')
        with self.lock:
            return self.sessions.setdefault(key, Session())

    def _peer(self, state, name):
        if name not in self.targets:
            raise ValueError(f'Unconfigured machine: {name}.')
        if name in state.disconnected:
            raise RuntimeError('Disconnected. Reconnect this machine.')
        peer = state.peers.get(name)
        if peer is None:
            peer = None
            try:
                peer = self.factory(self.targets[name])
                info = peer.call('ping')
                if not isinstance(info, dict):
                    raise ValueError('Invalid receiver identity. Expected an object.')
                if info.get('protocol') != 2:
                    raise ValueError('Receiver protocol mismatch. Redeploy the receiver.')
                if info.get('version') != VERSION:
                    raise ValueError(f'Receiver version mismatch: gateway {VERSION}, receiver {info.get("version")!r}. Redeploy the receiver.')
            except Exception:
                # A factory that fails before returning a peer (bad config, unavailable
                # executable) is a connection error too, not a raw ValueError/TypeError.
                if peer is not None:
                    peer.close()
                raise
            state.peers[name], state.identities[name] = peer, info
        if peer.closed:
            raise RuntimeError('Connection closed. Reconnect this machine.')
        return peer

    def status(self, key):
        state = self._session(key)
        with state.lock:
            return {'active': state.active, 'previous': state.previous,
                    'configured': ['local', *self.targets],
                    'connected': [k for k, v in state.peers.items() if not v.closed],
                    'identity': state.identities.get(state.active),
                    'scope': 'conversation', 'local_tools': sorted(LOCAL_TOOLS)}

    def switch(self, key, name):
        state = self._session(key)
        with state.lock:
            if name != 'local':
                self._peer(state, name)
            if name != state.active:
                state.previous, state.active = state.active, name
            return self.status(key)

    def back(self, key):
        state = self._session(key)
        with state.lock:
            return self.switch(key, state.previous)

    def reconnect(self, key, name=None):
        state = self._session(key)
        with state.lock:
            name = name or state.active
            if name == 'local':
                raise ValueError('Local has no connection.')
            if name in state.peers:
                state.peers.pop(name).close()
            state.disconnected.discard(name)
            self._peer(state, name)
            return self.status(key)

    def disconnect(self, key, name=None):
        state = self._session(key)
        with state.lock:
            name = name or state.active
            if name == 'local':
                raise ValueError('Local has no connection.')
            if name not in self.targets:
                raise ValueError(f'Unconfigured machine: {name}.')
            if name in state.peers:
                state.peers.pop(name).close()
            state.disconnected.add(name)
            # Intentionally keep remote selected: a disconnect must not redirect writes locally.
            return self.status(key)

    def call(self, key, method, params=None, machine=None):
        if method == 'tool':
            name = (params or {}).get('name', '')
            if name not in NATIVE_TOOLS and not name.startswith('mcp__'):
                raise ValueError(f'Tool not exposed: {name}.')
        state = self._session(key)
        with state.lock:
            name = machine or state.active
            if name == 'local':
                raise ValueError('This action needs a remote machine. Native tools already work locally.')
            # A disconnected entry cannot be silently recreated; explicit reconnect is required.
            if name not in state.peers and name == state.active:
                raise RuntimeError('Disconnected. Reconnect this machine.')
            peer = self._peer(state, name)
            result = peer.call(method, params)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    result = {'content': result}
            if not isinstance(result, dict):
                result = {'result': result}
            # A ping that reports protocol 2 but omits hostname must not crash the call path.
            identity = state.identities.get(name) or {}
            return {**result, 'teleoperator': {'machine': name,
                                               'hostname': identity.get('hostname') or name}}

    def _decision(self, name, args, machine):
        """Hermes' verdict for routing *name* to *machine*; failures deny."""
        try:
            return self.guard(name, args, machine=machine)
        except Exception as exc:  # a guard that raises never becomes consent
            return guards.Decision(False, f'Hermes approval gate failed ({exc}); '
                                          'refusing remote execution', 'failed-closed')

    def _blocked(self, machine, decision):
        result = {**decision.details,
                  'error': decision.message or 'Blocked by Hermes approval',
                  'status': decision.status or 'blocked',
                  'teleoperator': {'machine': machine, 'fallback': False,
                                   'guard': decision.source or 'hermes'}}
        if decision.status == 'pending_approval':
            result['approval_pending'] = True
        return result

    def route(self, key, name, args, next_call):
        state = self._session(key)
        with state.lock:
            if state.active == 'local' or name in LOCAL_TOOLS:
                return next_call(args)
            machine = state.active
        try:
            if name not in NATIVE_TOOLS and not name.startswith('mcp__'):
                raise ValueError(f'{name} is not routed. Use teleoperator search/describe/call for remote tools, or switch local.')
            # A routed call is only allowed to leave the gateway after Hermes'
            # own approval gate approved it. Denial (or an unreachable gate)
            # returns a blocked result: it never executes remotely and never
            # falls back to local.
            decision = self._decision(name, args, machine)
            if not decision.allowed:
                return self._blocked(machine, decision)
            return self.call(key, 'tool', {'name': name, 'arguments': args}, machine)
        except Exception as exc:
            return {'error': str(exc), 'teleoperator': {'machine': machine, 'fallback': False}}

    def run_explicit(self, key, name, args, machine=None):
        """Execute a tool the model asked for by name (``teleoperator call``).

        Same rules as the routed path: gated by Hermes, remote only, no local
        fallback. Unlike ``route`` this is never a native-tool interception, so
        an MCP tool reaches the receiver only through this explicit call.
        """
        state = self._session(key)
        with state.lock:
            target = machine or state.active
            if target == 'local':
                raise ValueError('This action needs a remote machine. Native tools already work locally.')
            if name not in NATIVE_TOOLS and not name.startswith('mcp__'):
                raise ValueError(f'Tool not exposed: {name}.')
        decision = self._decision(name, args, target)
        if not decision.allowed:
            return self._blocked(target, decision)
        return self.call(key, 'tool', {'name': name, 'arguments': args}, target)

    def transfer(self, key, source_machine, source, destination_machine, destination, move=False):
        from .files import local_file_operation
        state = self._session(key)
        if move and source_machine == 'local' and self.allow_local_delete is not True:
            raise ValueError('Local moves are disabled. Set allow_local_delete in teleoperator.json.')
        # One approval covers the complete operation, including disclosure of the
        # source, destination write and optional deletion. Include exact endpoints
        # in the gate's reason and rule key; approval must not generalize to a
        # different path, machine or operation.
        operation = {'source_machine': source_machine, 'source': source,
                     'destination_machine': destination_machine, 'destination': destination,
                     'move': bool(move)}
        binding = json.dumps(operation, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        decision = self._decision('mcp__teleoperator__transfer', operation, binding)
        if not isinstance(decision, guards.Decision) or decision.allowed is not True:
            if not isinstance(decision, guards.Decision):
                decision = guards.Decision(False, 'Invalid approval verdict; transfer refused', 'failed-closed')
            return self._blocked(destination_machine, decision)
        with state.lock:
            def endpoint(machine, method, params):
                if machine == 'local':
                    return local_file_operation(method, params, self.transfer_dir,
                                                self.allow_local_delete)
                return self._peer(state, machine).call(method, params)
            content = endpoint(source_machine, 'file_get', {'path': source})
            result = endpoint(destination_machine, 'file_put', {
                'path': destination, 'data': content['data'], 'sha256': content['sha256']})
            if result['sha256'] != content['sha256']:
                raise ValueError('Checksum mismatch. Source retained.')
            if move:
                endpoint(source_machine, 'file_remove', {'path': source, 'sha256': content['sha256']})
            return {'source_machine': source_machine, 'source': source,
                    'destination_machine': destination_machine, 'destination': destination,
                    'sha256': content['sha256'], 'size': content['size'], 'moved': bool(move)}

    def close_session(self, key):
        with self.lock:
            state = self.sessions.pop(key, None)
        if state:
            with state.lock:
                for peer in state.peers.values():
                    peer.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for key in list(self.sessions):
            self.close_session(key)
