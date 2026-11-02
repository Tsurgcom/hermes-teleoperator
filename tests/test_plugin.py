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

    def test_middleware_returns_error_even_if_controller_raises(self):
        ctx = Context()
        register(ctx)
        self.addCleanup(ctx.cleanup)
        result = ctx.middleware['tool_execution'](tool_name='terminal', args={},
                   next_call=lambda _: self.fail('fallback'), session_id=[])
        self.assertIn('error', json.loads(result))
