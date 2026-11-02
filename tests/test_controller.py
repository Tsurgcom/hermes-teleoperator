import unittest
from hermes_teleoperator.controller import Controller


class Peer:
    def __init__(self, name):
        self.name, self.closed, self.calls = name, False, []
    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'ping':
            return {'protocol': 1, 'hostname': self.name, 'cwd': '/work/' + self.name}
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
        self.controller = Controller({'alpha': {'host': 'alpha'}, 'beta': {'host': 'beta'}}, factory)
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

    def test_remote_call_rejects_unsupported_tool_even_when_explicit(self):
        c = self.controller
        c.switch('s', 'alpha')
        with self.assertRaisesRegex(ValueError, 'not exposed'):
            c.call('s', 'tool', {'name': 'execute_code', 'arguments': {}})

    def test_no_shared_implicit_session(self):
        with self.assertRaises(ValueError):
            self.controller.switch('', 'alpha')
