"""Conversation-local machine selection. Never fall back to local execution."""
from dataclasses import dataclass, field
import json
import threading

from .transport import RpcProcess, ssh_argv

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
    def __init__(self, targets, factory=None):
        self.targets = targets
        self.factory = factory or (lambda target: RpcProcess(ssh_argv(target), timeout=target.get('timeout', 120)))
        self.sessions = {}
        self.lock = threading.RLock()

    def _session(self, key):
        if not key:
            raise ValueError('No conversation identity: refusing process-global machine selection')
        with self.lock:
            return self.sessions.setdefault(key, Session())

    def _peer(self, state, name):
        if name not in self.targets:
            raise ValueError(f'Unconfigured machine: {name}')
        if name in state.disconnected:
            raise RuntimeError('Disconnected; reconnect explicitly')
        peer = state.peers.get(name)
        if peer is None:
            peer = self.factory(self.targets[name])
            try:
                info = peer.call('ping')
                if info.get('protocol') != 1:
                    raise ValueError('Incompatible companion protocol')
            except Exception:
                peer.close()
                raise
            state.peers[name], state.identities[name] = peer, info
        if peer.closed:
            raise RuntimeError('Connection closed; use reconnect. No local fallback.')
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
                raise ValueError('Local has no connection')
            if name in state.peers:
                state.peers.pop(name).close()
            state.disconnected.discard(name)
            self._peer(state, name)
            return self.status(key)

    def disconnect(self, key, name=None):
        state = self._session(key)
        with state.lock:
            name = name or state.active
            if name in state.peers:
                state.peers.pop(name).close()
            state.disconnected.add(name)
            # Intentionally keep remote selected: a disconnect must not redirect writes locally.
            return self.status(key)

    def call(self, key, method, params=None, machine=None):
        if method == 'tool':
            name = (params or {}).get('name', '')
            if name not in NATIVE_TOOLS and not name.startswith('mcp__'):
                raise ValueError(f'Tool not exposed: {name}')
        state = self._session(key)
        with state.lock:
            name = machine or state.active
            if name == 'local':
                raise ValueError('This action needs a remote machine; native tools already work locally')
            # A disconnected entry cannot be silently recreated; explicit reconnect is required.
            if name not in state.peers and name == state.active:
                raise RuntimeError('Disconnected; reconnect explicitly')
            peer = self._peer(state, name)
            result = peer.call(method, params)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    result = {'content': result}
            if not isinstance(result, dict):
                result = {'result': result}
            return {**result, 'teleoperator': {'machine': name, 'hostname': state.identities[name]['hostname']}}

    def route(self, key, name, args, next_call):
        state = self._session(key)
        with state.lock:
            if state.active == 'local' or name in LOCAL_TOOLS:
                return next_call(args)
            try:
                if name not in NATIVE_TOOLS and not name.startswith('mcp__'):
                    raise ValueError(f'{name} is not routed. Use teleoperator search/describe/call for remote tools, or switch local.')
                return self.call(key, 'tool', {'name': name, 'arguments': args})
            except Exception as exc:
                return {'error': str(exc), 'teleoperator': {'machine': state.active, 'fallback': False}}

    def transfer(self, key, source_machine, source, destination_machine, destination, move=False):
        from .files import file_operation
        state = self._session(key)
        with state.lock:
            def endpoint(machine, method, params):
                if machine == 'local':
                    return file_operation(method, params)
                return self._peer(state, machine).call(method, params)
            content = endpoint(source_machine, 'file_get', {'path': source})
            result = endpoint(destination_machine, 'file_put', {'path': destination, **content})
            if result['sha256'] != content['sha256']:
                raise ValueError('Destination checksum mismatch; source retained')
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
