import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_teleoperator.runtime import HermesRuntime


class RuntimeCleanupTests(unittest.TestCase):
    def test_cleanup_runs_all_lifecycles_even_after_mcp_shutdown_error(self):
        events = []
        mcp = ModuleType('tools.mcp_tool_lifecycle')
        terminal = ModuleType('tools.terminal_tool_lifecycle')
        plugins = ModuleType('hermes_cli.plugins')
        def shutdown():
            events.append('mcp')
            raise RuntimeError('MCP cleanup failed')
        mcp.shutdown_mcp_servers = shutdown
        terminal.cleanup_all_environments = lambda: events.append('terminal')
        plugins.get_plugin_manager = lambda: SimpleNamespace(unload=lambda: events.append('plugins'))
        runtime = HermesRuntime.__new__(HermesRuntime)
        runtime.mcp_loaded, runtime.expose_mcp = True, ['fixture']
        with patch.dict(sys.modules, {'tools.mcp_tool_lifecycle': mcp,
                'tools.terminal_tool_lifecycle': terminal, 'hermes_cli.plugins': plugins}):
            with self.assertRaisesRegex(RuntimeError, 'MCP cleanup failed'):
                runtime.close()
        self.assertEqual(events, ['mcp', 'terminal', 'plugins'])
