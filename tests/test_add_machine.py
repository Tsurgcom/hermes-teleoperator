import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_teleoperator.plugin import register
from test_plugin import Context


class AddMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'teleoperator.json'
        self.path.write_text(json.dumps({'other': {'keep': True}, 'machines': {'old': {'host': 'old'}}}))
        self.patch = patch('hermes_teleoperator.plugin.config_path', return_value=self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ctx = Context()
        register(self.ctx)
        self.addCleanup(self.ctx.cleanup)

    def test_add_is_offline_preserves_config_and_updates_status(self):
        status = self.ctx.tools['teleoperator']['handler']
        self.assertEqual(json.loads(status({'action': 'status'}, session_id='s'))['configured'], ['local', 'old'])
        command = self.ctx.commands['teleoperator-add']
        with patch('hermes_teleoperator.controller.RpcProcess', side_effect=AssertionError('network')):
            result = json.loads(command('{"name":"new","host":"me@example.org","port":2222}', session_id='s'))
        self.assertEqual(result['active'], 'local')
        self.assertEqual(result['configured'], ['local', 'old', 'new'])
        self.assertEqual(json.loads(self.path.read_text()), {'other': {'keep': True}, 'machines': {
            'old': {'host': 'old'}, 'new': {'host': 'me@example.org', 'port': 2222}}})

    def test_rejects_invalid_inputs_without_writing(self):
        command = self.ctx.commands['teleoperator-add']
        before = self.path.read_bytes()
        cases = ['not json', '[]', '{}', '{"name":"new","name":"x","host":"h"}']
        for entry in [
            {'name': 'local', 'host': 'h'}, {'name': 'status', 'host': 'h'},
            {'name': 'OLD', 'host': 'h'}, {'name': '-o', 'host': 'h'},
            {'name': 'new', 'host': '-oProxyCommand=bad'}, {'name': 'new', 'host': 'a b'},
            {'name': 'new', 'host': 'h', 'port': True}, {'name': 'new', 'host': 'h', 'port': 65536},
            {'name': 'new', 'host': 'h', 'extra': 'x'},
            *({'name': 'new', 'host': 'h', key: path} for key in
              ('executable', 'identity_file', 'known_hosts', 'teleoperator_home')
              for path in ('-option', '../escape', '/tmp/../escape', 'foo\nbar', '%h', '')),
        ]:
            cases.append(json.dumps(entry))
        for raw in cases:
            with self.subTest(raw=raw):
                result = json.loads(command(raw, session_id='s'))
                self.assertIn('error', result)
                self.assertFalse(result['teleoperator']['fallback'])
                self.assertEqual(self.path.read_bytes(), before)

    def test_unbracketed_ipv6_is_accepted_but_bracketed_is_rejected(self):
        from hermes_teleoperator.configuration import validate_machine
        for host in ('::1', 'user@2001:db8::1'):
            with self.subTest(host=host):
                self.assertEqual(validate_machine(json.dumps({'name': 'ipv6', 'host': host}))[1]['host'], host)
        for host in ('[::1]', 'user@[::1]', 'user@::::'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_machine(json.dumps({'name': 'ipv6', 'host': host}))

    def test_missing_identity_or_changed_profile_fails_closed(self):
        command = self.ctx.commands['teleoperator-add']
        raw = '{"name":"new","host":"host"}'
        self.assertIn('error', json.loads(command(raw)))
        self.ctx.tools['teleoperator']['handler']({'action': 'status'}, session_id='s')
        other = Path(self.tmp.name) / 'other.json'
        with patch('hermes_teleoperator.plugin.config_path', return_value=other):
            self.assertIn('error', json.loads(command(raw, session_id='s')))
        self.assertFalse(other.exists())
        self.assertNotIn('new', json.loads(self.path.read_text())['machines'])

    def test_rejects_mismatched_session_profile_before_writing(self):
        with patch('hermes_teleoperator.plugin.session_profile', return_value='different'), patch('hermes_teleoperator.plugin.profile_for_home', return_value='default'):
            result = json.loads(self.ctx.commands['teleoperator-add']('{"name":"new","host":"host"}', session_id='s'))
        self.assertIn('error', result)
        self.assertNotIn('new', json.loads(self.path.read_text())['machines'])

    def test_existing_connection_and_other_session_survive_add(self):
        from test_controller import Peer
        peer = Peer('old')
        with patch('hermes_teleoperator.controller.RpcProcess', return_value=peer):
            handler = self.ctx.tools['teleoperator']['handler']
            self.assertEqual(json.loads(handler({'action': 'switch', 'machine': 'old'}, session_id='s'))['active'], 'old')
            result = json.loads(self.ctx.commands['teleoperator-add']('{"name":"new","host":"h"}', session_id='s'))
            self.assertEqual(result['active'], 'old')
            self.assertEqual(result['connected'], ['old'])
            self.assertFalse(peer.closed)
            self.assertEqual(json.loads(handler({'action': 'status'}, session_id='other'))['active'], 'local')

    def test_external_change_to_existing_target_fails_closed(self):
        handler = self.ctx.tools['teleoperator']['handler']
        handler({'action': 'status'}, session_id='s')
        self.path.write_text('{"machines":{"old":{"host":"changed"}}}')
        before = self.path.read_bytes()
        result = json.loads(self.ctx.commands['teleoperator-add']('{"name":"new","host":"h"}', session_id='s'))
        self.assertIn('error', result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_config_is_not_replaced(self):
        actual = self.path.with_name('actual.json')
        self.path.rename(actual)
        self.path.symlink_to(actual)
        before = actual.read_bytes()
        result = json.loads(self.ctx.commands['teleoperator-add']('{"name":"new","host":"h"}', session_id='s'))
        self.assertIn('error', result)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(actual.read_bytes(), before)

    def test_concurrent_independent_controllers_keep_both_additions(self):
        from concurrent.futures import ThreadPoolExecutor
        contexts = [Context(), Context()]
        for ctx in contexts:
            register(ctx)
            self.addCleanup(ctx.cleanup)
            ctx.tools['teleoperator']['handler']({'action': 'status'}, session_id='s')
        def add(i):
            return json.loads(contexts[i].commands['teleoperator-add'](
                json.dumps({'name': f'new{i}', 'host': 'h'}), session_id='s'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(add, range(2)))
        self.assertTrue(all('error' not in result for result in results), results)
        self.assertEqual(set(json.loads(self.path.read_text())['machines']), {'old', 'new0', 'new1'})

    def test_replace_failure_preserves_original_and_cleans_temporary(self):
        before = self.path.read_bytes()
        with patch('hermes_teleoperator.configuration.os.replace', side_effect=OSError('replace failed')):
            result = json.loads(self.ctx.commands['teleoperator-add']('{"name":"new","host":"h"}', session_id='s'))
        self.assertIn('error', result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.teleoperator.json.*')), [])
