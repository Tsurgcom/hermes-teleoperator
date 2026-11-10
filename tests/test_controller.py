import os
import unittest
from unittest.mock import patch
from hermes_teleoperator.version import VERSION
from hermes_teleoperator import guards
from hermes_teleoperator.controller import Controller


def allow(name, args, *, machine):
    """Stand-in for a positive Hermes verdict: these tests cover transport, and
    the approval contract itself is covered in tests/test_guards.py."""
    return guards.Decision(True, source='test-allow')


class Peer:
    def __init__(self, name):
        self.name, self.closed, self.calls = name, False, []
    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'ping':
            return {'protocol': 2, 'version': VERSION, 'hostname': self.name, 'cwd': '/work/' + self.name}
        if method == 'tool':
            return {'content': self.name}
        raise RuntimeError('disconnected')
    def close(self):
        self.closed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.peers = []
        def factory(target):
            peer = Peer(target['host'])
            self.peers.append(peer)
            return peer
        self.controller = Controller({'alpha': {'host': 'alpha'}, 'beta': {'host': 'beta'}}, factory,
                                     guard=allow)
        self.addCleanup(self.controller.close)

    def test_switch_back_reuses_connection_and_is_session_scoped(self):
        c = self.controller
        self.assertEqual(c.switch('s1', 'alpha')['active'], 'alpha')
        self.assertEqual(c.switch('s1', 'beta')['active'], 'beta')
        self.assertEqual(c.back('s1')['active'], 'alpha')
        self.assertEqual(len(self.peers), 2)
        self.assertEqual(c.status('s2')['active'], 'local')
        self.assertEqual(c.status('s1')['active'], 'alpha')
        self.assertEqual(c.switch('s1', 'local')['active'], 'local')

    def test_failed_switch_keeps_previous_destination(self):
        c = self.controller
        c.switch('s', 'alpha')
        with self.assertRaises(ValueError):
            c.switch('s', 'unconfigured')
        self.assertEqual(c.status('s')['active'], 'alpha')

    def test_remote_failure_never_calls_local_next(self):
        c = self.controller
        c.switch('s', 'alpha')
        local = []
        def next_call(args):
            local.append(args)
            return 'LOCAL'
        result = c.route('s', 'read_file', {'path': '/file'}, next_call)
        self.assertEqual(result['teleoperator']['machine'], 'alpha')
        self.peers[0].closed = True
        result = c.route('s', 'terminal', {'command': 'hostname'}, next_call)
        self.assertIn('error', result)
        self.assertEqual(local, [])
        self.assertEqual(c.status('s')['active'], 'alpha')

    def test_local_routes_unchanged_and_remote_code_cannot_escape(self):
        c = self.controller
        self.assertEqual(c.route('s', 'read_file', {}, lambda _: 'local'), 'local')
        c.switch('s', 'alpha')
        result = c.route('s', 'execute_code', {}, lambda _: self.fail('local execution'))
        self.assertIn('error', result)

    def test_transfer_copies_before_deleting_and_preserves_selection(self):
        c = self.controller
        c.switch('s', 'alpha')
        import tempfile
        from pathlib import Path
        from hermes_teleoperator.files import file_operation
        class FilePeer(Peer):
            def call(self, method, params=None):
                if method.startswith('file_'):
                    return file_operation(method, params)
                return super().call(method, params)
        c.factory = lambda target: FilePeer(target['host'])
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / 'in', Path(tmp) / 'out'
            source.write_bytes(b'payload')
            result = c.transfer('s', 'local', str(source), 'beta', str(dest), move=True)
            self.assertTrue(result['moved'])
            self.assertEqual(dest.read_bytes(), b'payload')
            self.assertFalse(source.exists())
            self.assertEqual(c.status('s')['active'], 'alpha')

    def test_disconnect_does_not_reconnect_on_transfer(self):
        c = self.controller
        c.switch('s', 'alpha')
        c.disconnect('s')
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.write_text('keep')
            with self.assertRaisesRegex(RuntimeError, 'Disconnected'):
                c.transfer('s', 'local', str(source), 'alpha', '/destination')
            self.assertTrue(source.exists())
        self.assertEqual(len(self.peers), 1)

    def test_disconnect_rejects_local_and_unconfigured_machines(self):
        c = self.controller
        with self.assertRaisesRegex(ValueError, 'Local has no connection'):
            c.disconnect('s')
        with self.assertRaisesRegex(ValueError, 'Unconfigured machine'):
            c.disconnect('s', 'not-configured')
        self.assertEqual(c.status('s')['active'], 'local')

    def test_remote_call_rejects_unsupported_tool_even_when_explicit(self):
        c = self.controller
        c.switch('s', 'alpha')
        with self.assertRaisesRegex(ValueError, 'not exposed'):
            c.call('s', 'tool', {'name': 'execute_code', 'arguments': {}})

    def test_receiver_version_must_match_gateway_before_selection(self):
        from hermes_teleoperator.version import VERSION
        for reported in (None, '0.0.0', VERSION + '-other', 123, {'bad': True}):
            with self.subTest(reported=reported):
                class VersionPeer(Peer):
                    def call(self, method, params=None):
                        result = super().call(method, params)
                        if method == 'ping':
                            result['version'] = reported
                        return result
                peer = VersionPeer('alpha')
                controller = Controller({'alpha': {'host': 'alpha'}}, lambda _: peer)
                self.addCleanup(controller.close)
                with self.assertRaisesRegex(ValueError, 'version mismatch'):
                    controller.switch('version-test', 'alpha')
                self.assertTrue(peer.closed)
                self.assertEqual(controller.status('version-test')['active'], 'local')

    def test_malformed_ping_is_rejected_and_peer_closed(self):
        for reported in (None, [], 'not identity'):
            with self.subTest(reported=reported):
                class MalformedPeer(Peer):
                    def call(self, method, params=None):
                        return reported
                peer = MalformedPeer('alpha')
                controller = Controller({'alpha': {'host': 'alpha'}}, lambda _: peer)
                self.addCleanup(controller.close)
                with self.assertRaisesRegex(ValueError, 'identity'):
                    controller.switch('bad-ping', 'alpha')
                self.assertTrue(peer.closed)

    def test_no_shared_implicit_session(self):
        with self.assertRaises(ValueError):
            self.controller.switch('', 'alpha')

    def test_default_guard_refuses_remote_work_when_the_gate_is_unreachable(self):
        """The shipped controller must never execute remotely without a verdict
        from Hermes' gate: when the gate cannot be reached, the call is refused."""
        import sys
        c = Controller({'alpha': {'host': 'alpha'}}, lambda _: Peer('alpha'))
        self.addCleanup(c.close)
        c.switch('s', 'alpha')
        local = []
        with patch.dict(sys.modules, {'tools': None, 'tools.approval': None}):
            result = c.route('s', 'terminal', {'command': 'echo hi'},
                             lambda args: local.append(args) or 'LOCAL')
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['teleoperator']['fallback'])
        self.assertEqual(local, [])

    def test_local_conversation_never_asks_the_guard(self):
        def guard(name, args, *, machine):
            self.fail('the approval gate must not be consulted while local')
        c = Controller({'alpha': {'host': 'alpha'}}, self.controller.factory, guard=guard)
        self.addCleanup(c.close)
        self.assertEqual(c.route('s', 'terminal', {'command': 'rm -rf /'}, lambda args: 'LOCAL'), 'LOCAL')

    def test_denial_keeps_the_connection_untouched(self):
        def deny(name, args, *, machine):
            return guards.Decision(False, 'BLOCKED: denied by test', 'test-deny')
        c = Controller({'alpha': {'host': 'alpha'}}, self.controller.factory, guard=deny)
        self.addCleanup(c.close)
        c.switch('s', 'alpha')
        remote = self.peers[-1]
        result = c.route('s', 'write_file', {'path': '/tmp/x', 'content': 'y'}, lambda args: 'LOCAL')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([call for call in remote.calls if call[0] == 'tool'], [])
        self.assertEqual(c.status('s')['active'], 'alpha')


class ControllerSshEnvironmentTests(unittest.TestCase):
    def test_default_factory_gives_ssh_only_the_allowlisted_environment(self):
        parent = {'PATH': '/usr/bin', 'HOME': '/home/alice', 'TERM': 'xterm',
                  'OPENAI_API_KEY': 'gateway-placeholder', 'AWS_SECRET_ACCESS_KEY': 'secret'}
        with patch.dict(os.environ, parent, clear=True), \
                patch('hermes_teleoperator.controller.RpcProcess') as rpc:
            Controller({'alpha': {'host': 'alpha'}}).factory({'host': 'alpha', 'timeout': 5})
        self.assertEqual(rpc.call_args.kwargs['env'],
                         {'PATH': '/usr/bin', 'HOME': '/home/alice', 'TERM': 'xterm'})
