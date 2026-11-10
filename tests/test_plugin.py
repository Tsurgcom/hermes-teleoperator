import json
from pathlib import Path
import tempfile
import unittest
from hermes_teleoperator.plugin import register


class Context:
    def __init__(self):
        self.tools, self.middleware, self.hooks, self.commands = {}, {}, {}, {}
    def register_tool(self, **kw): self.tools[kw['name']] = kw
    def register_middleware(self, name, fn): self.middleware[name] = fn
    def register_hook(self, name, fn): self.hooks[name] = fn
    def register_command(self, name, fn, **kw): self.commands[name] = fn
    def register_skill(self, *args): pass
    def on_unload(self, fn): self.cleanup = fn


class PluginTests(unittest.TestCase):
    def test_registers_without_network_and_handles_missing_identity_closed(self):
        ctx = Context()
        register(ctx)
        self.addCleanup(ctx.cleanup)
        self.assertIn('teleoperator', ctx.tools)
        self.assertIn('teleport', ctx.commands)
        middleware = ctx.middleware['tool_execution']
        result = middleware(tool_name='terminal', args={'command': 'pwd'},
                            next_call=lambda args: self.fail('unsafe implicit local fallback'))
        self.assertIn('error', json.loads(result))
        result = middleware(tool_name='terminal', args={}, session_id='abc', next_call=lambda _: 'LOCAL')
        self.assertEqual(result, 'LOCAL')
        result = ctx.tools['teleoperator']['handler']({'action': 'status'}, session_id='abc')
        self.assertEqual(json.loads(result)['active'], 'local')
        self.assertIn('local', ctx.commands['teleport']('status', session_id='abc'))

    def test_identity_uses_only_public_callback_context(self):
        import builtins
        from unittest.mock import patch
        from hermes_teleoperator.plugin import identity
        original = builtins.__import__

        def public_only(name, *args, **kwargs):
            if name == 'hermes_cli.plugin_session':
                self.fail('private session import must not be used')
            return original(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=public_only):
            self.assertEqual(identity({'task_id': 'task-a'}), 'task-a')
            self.assertEqual(identity({'session_id': 'session-a', 'task_id': 'task-a'}), 'session-a')
            self.assertEqual(identity({'session_key': 'chat-a'}), 'chat-a')
            with self.assertRaisesRegex(ValueError, 'No stable conversation identity'):
                identity({})

    def test_middleware_returns_error_even_if_controller_raises(self):
        ctx = Context()
        register(ctx)
        self.addCleanup(ctx.cleanup)
        result = ctx.middleware['tool_execution'](tool_name='terminal', args={},
                   next_call=lambda _: self.fail('fallback'), session_id=[])
        self.assertIn('error', json.loads(result))

    def test_settings_validation(self):
        from hermes_teleoperator.configuration import load_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'teleoperator.json'
            self.assertEqual(load_settings(path), {'machines': {}})
            for value in ('relative/path', '', 5, None):
                with self.subTest(transfer_dir=value):
                    path.write_text(json.dumps({'transfer_dir': value}))
                    with self.assertRaisesRegex(ValueError, 'transfer_dir'):
                        load_settings(path)
            path.write_text(json.dumps({'allow_local_delete': 'yes'}))
            with self.assertRaisesRegex(ValueError, 'allow_local_delete'):
                load_settings(path)
            path.write_text(json.dumps({'machines': []}))
            with self.assertRaisesRegex(ValueError, 'machines'):
                load_settings(path)
            path.write_text(json.dumps({'machines': {}, 'transfer_dir': '/tmp/transfer',
                                        'allow_local_delete': True}))
            settings = load_settings(path)
            self.assertEqual(settings['transfer_dir'], '/tmp/transfer')
            self.assertTrue(settings['allow_local_delete'])

    def test_transfer_config_is_wired_and_moves_are_gated(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            transfer = base / 'transfer'
            transfer.mkdir()
            config = base / 'teleoperator.json'
            config.write_text(json.dumps({'machines': {}, 'transfer_dir': str(transfer)}))
            with patch('hermes_teleoperator.plugin.config_path', return_value=config), \
                 patch('hermes_teleoperator.files._hermes_home',
                       return_value=Path('/nonexistent-test-hermes')), \
                 patch('hermes_teleoperator.files._gateway_home',
                       return_value=Path('/nonexistent-test-home')):
                ctx = Context()
                register(ctx)
                self.addCleanup(ctx.cleanup)
                handler = ctx.tools['teleoperator']['handler']
                source = transfer / 'source.bin'
                source.write_bytes(b'gateway payload')
                destination = transfer / 'destination.bin'
                args = {'action': 'transfer', 'source_machine': 'local', 'source': str(source),
                        'destination_machine': 'local', 'destination': str(destination),
                        'move': True}
                result = json.loads(handler(args, session_id='s'))
                self.assertIn('error', result)
                self.assertIn('allow_local_delete', result['error'])
                self.assertTrue(source.exists())
                self.assertFalse(destination.exists())
                result = json.loads(handler({**args, 'move': False}, session_id='s'))
                self.assertEqual(result['size'], len(b'gateway payload'))
                self.assertEqual(destination.read_bytes(), b'gateway payload')
                outside = base / 'outside.bin'
                result = json.loads(handler({**args, 'move': False, 'destination': str(outside)},
                                            session_id='s'))
                self.assertIn('error', result)
                self.assertIn('outside', result['error'])
                self.assertFalse(outside.exists())
            config.write_text(json.dumps({'machines': {}, 'transfer_dir': str(transfer),
                                          'allow_local_delete': True}))
            with patch('hermes_teleoperator.plugin.config_path', return_value=config), \
                 patch('hermes_teleoperator.files._hermes_home',
                       return_value=Path('/nonexistent-test-hermes')), \
                 patch('hermes_teleoperator.files._gateway_home',
                       return_value=Path('/nonexistent-test-home')):
                ctx = Context()
                register(ctx)
                self.addCleanup(ctx.cleanup)
                handler = ctx.tools['teleoperator']['handler']
                source = transfer / 'moved-source.bin'
                source.write_bytes(b'move me')
                destination = transfer / 'moved-destination.bin'
                result = json.loads(handler({'action': 'transfer', 'source_machine': 'local',
                                             'source': str(source), 'destination_machine': 'local',
                                             'destination': str(destination), 'move': True},
                                            session_id='s'))
                self.assertTrue(result['moved'])
                self.assertFalse(source.exists())
                self.assertEqual(destination.read_bytes(), b'move me')
