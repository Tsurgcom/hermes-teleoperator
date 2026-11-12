"""Remote calls must pass Hermes' own approval gate before they execute.

These tests stand in for the two core entry points the plugin calls
(``tools.approval.check_all_command_guards`` and
``tools.approval.request_tool_approval``) so the routing contract is provable
without a live Hermes install: a remote call executes only after a positive
verdict, a refusal or an unreachable gate never reaches the machine, and a local
conversation is untouched.
"""
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_teleoperator import guards
from hermes_teleoperator.controller import Controller
from hermes_teleoperator.files import file_operation
from hermes_teleoperator.version import VERSION


class Peer:
    def __init__(self, name):
        self.name, self.closed, self.calls = name, False, []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'ping':
            return {'protocol': 2, 'version': VERSION, 'hostname': self.name}
        if method == 'tool':
            return {'content': 'remote:' + params['name']}
        if method.startswith('file_'):
            return file_operation(method, params)
        raise RuntimeError('disconnected')

    def close(self):
        self.closed = True


UNSET = object()


class Gate:
    """Records the guard queries the plugin makes and returns a fixed verdict."""

    def __init__(self, approved=True, verdict=UNSET, raises=None):
        self.approved, self.verdict, self.raises = approved, verdict, raises
        self.commands, self.tools = [], []

    def check_all_command_guards(self, command, env_type, **kwargs):
        self.commands.append((command, env_type))
        if self.raises:
            raise self.raises
        return {'approved': self.approved} if self.verdict is UNSET else self.verdict

    def request_tool_approval(self, tool_name, reason, *, rule_key='', **kwargs):
        self.tools.append((tool_name, reason, rule_key))
        if self.raises:
            raise self.raises
        return {'approved': self.approved} if self.verdict is UNSET else self.verdict


def fake_approval(gate):
    tools = types.ModuleType('tools')
    approval = types.ModuleType('tools.approval')
    approval.check_all_command_guards = gate.check_all_command_guards
    approval.request_tool_approval = gate.request_tool_approval
    tools.approval = approval
    return {'tools': tools, 'tools.approval': approval}


class RoutingGuardTests(unittest.TestCase):
    def setUp(self):
        self.peers = []

        def factory(target):
            peer = Peer(target['host'])
            self.peers.append(peer)
            return peer

        self.controller = Controller({'alpha': {'host': 'alpha'}}, factory)
        self.addCleanup(self.controller.close)
        self.controller.switch('s', 'alpha')
        self.local = []
        self.peer = self.peers[0]

    def route(self, name, args, gate, **kw):
        with patch.dict(sys.modules, fake_approval(gate)):
            return self.controller.route('s', name, args, self.next_call)

    def next_call(self, args):
        self.local.append(args)
        return 'LOCAL'

    # --- terminal: the dangerous-command gate ---------------------------------
    def test_terminal_asks_the_core_dangerous_command_gate(self):
        gate = Gate()
        result = self.route('terminal', {'command': 'rm -rf /tmp/build'}, gate)
        self.assertEqual(gate.commands, [('rm -rf /tmp/build', 'ssh')])
        self.assertEqual(result['content'], 'remote:terminal')
        self.assertEqual(self.local, [])

    def test_denied_command_never_reaches_the_machine(self):
        gate = Gate(approved=False, verdict={'approved': False, 'message': 'BLOCKED: rm -rf /'})
        result = self.route('terminal', {'command': 'rm -rf /'}, gate)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('BLOCKED', result['error'])
        self.assertIn('alpha', result['teleoperator']['machine'])
        self.assertFalse(result['teleoperator']['fallback'])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])
        self.assertEqual(self.local, [])
        self.assertEqual(self.controller.status('s')['active'], 'alpha')

    def test_gate_that_raises_denies_instead_of_running_unguarded(self):
        gate = Gate(raises=RuntimeError('no approval transport'))
        result = self.route('terminal', {'command': 'echo hi'}, gate)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('unavailable', result['error'])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_verdict_must_be_an_explicit_true(self):
        for verdict in ({}, {'approved': 'yes'}, {'approved': 1}, None, []):
            with self.subTest(verdict=verdict):
                gate = Gate(verdict=verdict)
                result = self.route('terminal', {'command': 'echo hi'}, gate)
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse(result['teleoperator']['fallback'])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_missing_hermes_approval_fails_closed(self):
        with patch.dict(sys.modules, {'tools': None, 'tools.approval': None}):
            result = self.controller.route('s', 'terminal', {'command': 'echo hi'}, self.next_call)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])
        self.assertEqual(self.local, [])

    # --- writes, skills, MCP: the human gate ---------------------------------
    def test_remote_mutations_use_the_human_gate(self):
        cases = [
            ('write_file', {'path': '/srv/app/main.py', 'content': 'x'}, 'write:/srv/app'),
            ('patch', {'path': '/srv/app/deep/mod.py'}, 'write:/srv/app/deep'),
            ('skill_manage', {'operations': [{'action': 'create', 'name': 'deploy'}]}, 'skills:["deploy"]'),
            ('mcp__gitea__create_issue', {'title': 'x'}, 'tool:mcp__gitea__create_issue'),
        ]
        for name, args, grain in cases:
            with self.subTest(tool=name):
                gate = Gate()
                self.route(name, args, gate)
                self.assertEqual([t[0] for t in gate.tools], [name])
                self.assertIn('alpha', gate.tools[0][1])
                self.assertIn(grain, gate.tools[0][2])
        self.assertEqual(len([c for c in self.peer.calls if c[0] == 'tool']), 4)

    def test_skill_operation_batches_have_distinct_approval_scopes(self):
        keys = []
        for names in (['alpha'], ['beta'], ['alpha', 'beta'], ['beta', 'alpha']):
            gate = Gate()
            self.route('skill_manage', {'operations': [
                {'action': 'patch', 'name': name, 'old_string': 'a', 'new_string': 'b'}
                for name in names]}, gate)
            keys.append(gate.tools[0][2])
        self.assertNotEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])
        self.assertNotEqual(keys[1], keys[2])
        self.assertEqual(keys[2], keys[3])

    def test_skill_manage_with_unidentifiable_targets_is_refused(self):
        for args in ({}, {'operations': []}, {'operations': [{}]},
                     {'operations': [{'name': 'safe'}, {}]},
                     {'operations': [{'name': 123}]},
                     {'operations': 'not-an-array'}):
            with self.subTest(args=args):
                gate = Gate()
                result = self.route('skill_manage', args, gate)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(gate.tools, [])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_denied_write_never_reaches_the_machine(self):
        gate = Gate(approved=False, verdict={'approved': False, 'message': 'BLOCKED: no human present'})
        result = self.route('write_file', {'path': '/srv/app/main.py', 'content': 'x'}, gate)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_process_control_is_gated_and_observation_is_not(self):
        gate = Gate()
        self.route('process_manage', {'action': 'list'}, gate)
        self.route('process_manage', {'action': 'status', 'session_id': 'j1'}, gate)
        self.assertEqual(gate.tools, [])
        self.route('process_manage', {'action': 'kill', 'session_id': 'j1'}, gate)
        self.assertEqual([t[0] for t in gate.tools], ['process_manage'])
        self.assertEqual(gate.commands, [])

    def test_process_stdin_is_command_gated_per_payload(self):
        payload = 'rm -rf /srv/build'
        digest = hashlib.sha256(payload.encode()).hexdigest()
        gate = Gate()
        result = self.route('process', {'action': 'submit', 'session_id': 'j1', 'text': payload}, gate)
        self.assertEqual(result['content'], 'remote:process')
        self.assertEqual(gate.commands, [(payload, 'ssh')])
        self.assertEqual(len(gate.tools), 1)
        self.assertIn(payload, gate.tools[0][1])
        self.assertIn(digest, gate.tools[0][2])
        self.assertIn('process:submit:', gate.tools[0][2])
        other = Gate()
        self.route('process_manage', {'action': 'write', 'session_id': 'j1', 'data': 'echo kept'}, other)
        self.assertNotEqual(gate.tools[0][2], other.tools[0][2])
        blank = Gate()
        self.route('process', {'action': 'submit', 'session_id': 'j1', 'text': '   '}, blank)
        self.assertEqual(blank.commands, [])
        self.assertEqual(len(blank.tools), 1)

    def test_denied_process_stdin_never_asks_for_approval_or_reaches_the_machine(self):
        gate = Gate(approved=False, verdict={'approved': False, 'message': 'BLOCKED: no'})
        result = self.route('process_manage',
                            {'action': 'submit', 'session_id': 'j1', 'text': 'rm -rf /'}, gate)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(gate.commands, [('rm -rf /', 'ssh')])
        self.assertEqual(gate.tools, [])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])
        both = Gate()
        blocked = self.route('process', {'action': 'write', 'session_id': 'j1',
                                         'data': 'echo a', 'text': 'echo b'}, both)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(both.commands, [])
        self.assertEqual(both.tools, [])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    # --- reads ---------------------------------------------------------------
    def test_reads_keep_local_parity_and_are_not_prompted(self):
        gate = Gate()
        self.route('read_file', {'path': '/srv/app/main.py'}, gate)
        self.route('search_files', {'path': '/srv/app', 'pattern': '*.py', 'target': 'files'}, gate)
        self.assertEqual(gate.commands, [])
        self.assertEqual(gate.tools, [])

    def test_content_search_cannot_recurse_into_credentials(self):
        for args in ({'path': '/srv/app', 'pattern': 'secret'},
                     {'pattern': 'secret'},
                     {'path': '/srv/app', 'pattern': 'secret', 'target': 'content',
                      'file_glob': '*.txt', 'output_mode': 'count'},
                     {'path': '/srv/app', 'pattern': 'secret', 'target': 'unexpected'}):
            with self.subTest(args=args):
                result = self.route('search_files', args, Gate())
                self.assertEqual(result.get('status'), 'blocked')
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_credential_reads_are_refused_remotely(self):
        for path in ('/home/u/.ssh/id_ed25519', '/srv/app/.env', '/srv/app/.env.local',
                     '/home/u/.hermes/auth.json', '/srv/certs/server.pem'):
            with self.subTest(path=path):
                gate = Gate()
                result = self.route('read_file', {'path': path}, gate)
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('redact', result['error'])
                self.assertEqual(gate.commands, [])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    # --- local + explicit ----------------------------------------------------
    def test_local_conversation_is_untouched(self):
        self.controller.switch('s', 'local')
        gate = Gate(approved=False)
        with patch.dict(sys.modules, fake_approval(gate)):
            result = self.controller.route('s', 'terminal', {'command': 'rm -rf /'}, self.next_call)
            self.assertEqual(result, 'LOCAL')
        self.assertEqual(gate.commands, [])
        self.assertEqual([c for c in self.peer.calls if c[0] == 'tool'], [])

    def test_explicit_tool_call_is_gated_like_the_routed_path(self):
        gate = Gate(approved=False, verdict={'approved': False, 'message': 'BLOCKED: denied'})
        with patch.dict(sys.modules, fake_approval(gate)):
            result = self.controller.run_explicit('s', 'mcp__gitea__create_issue', {'title': 'x'})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([t[0] for t in gate.tools], ['mcp__gitea__create_issue'])
        gate_ok = Gate()
        with patch.dict(sys.modules, fake_approval(gate_ok)):
            allowed = self.controller.run_explicit('s', 'mcp__gitea__list_repos', {})
        self.assertEqual(allowed['content'], 'remote:mcp__gitea__list_repos')
        self.assertEqual(len([c for c in self.peer.calls if c[0] == 'tool']), 1)

    # --- guard unit surface --------------------------------------------------
    def test_tool_policy_table(self):
        self.assertTrue(guards.needs_gate('terminal', {}))
        for name in ('write_file', 'patch', 'skill_manage', 'mcp__x__y'):
            self.assertTrue(guards.needs_gate(name, {}), name)
        for name in ('read_file', 'search_files', 'skills_list', 'skill_view'):
            self.assertFalse(guards.needs_gate(name, {'path': '/a/b'}), name)
        self.assertFalse(guards.needs_gate('process_manage', {'action': 'poll'}))
        self.assertTrue(guards.needs_gate('process_manage', {'action': 'kill'}))


class TransferGuardTests(unittest.TestCase):
    def test_transfer_requires_exactly_bound_human_approval(self):
        """Approval includes source, destination and move before file effects."""
        peers = []

        def factory(target):
            peer = Peer(target['host'])
            peers.append(peer)
            return peer

        controller = Controller({'beta': {'host': 'beta'}}, factory)
        self.addCleanup(controller.close)
        controller.switch('s', 'beta')
        with tempfile.TemporaryDirectory() as tmp, \
                patch('hermes_teleoperator.files._hermes_home', return_value=Path('/nonexistent-test-hermes')), \
                patch('hermes_teleoperator.files._gateway_home', return_value=Path('/nonexistent-test-home')):
            controller.transfer_dir = tmp
            source, dest = Path(tmp) / 'in', Path(tmp) / 'out'
            source.write_bytes(b'payload')
            with patch.dict(sys.modules, {'tools': None, 'tools.approval': None}):
                blocked = controller.transfer('s', 'local', str(source), 'beta', str(dest))
            self.assertEqual(blocked['status'], 'blocked')
            self.assertFalse(dest.exists())
            gate = Gate()
            with patch.dict(sys.modules, fake_approval(gate)):
                result = controller.transfer('s', 'local', str(source), 'beta', str(dest))
            self.assertEqual(len(gate.tools), 1)
            for value in (str(source), str(dest), 'local', 'beta', 'move'):
                self.assertIn(value, gate.tools[0][1])
                self.assertIn(value, gate.tools[0][2])
            self.assertEqual(dest.read_bytes(), b'payload')
            self.assertFalse(result['moved'])


if __name__ == '__main__':
    unittest.main()
