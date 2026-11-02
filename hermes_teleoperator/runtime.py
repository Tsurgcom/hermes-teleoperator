"""Native Hermes dispatcher adapter. No model calls, no tool reimplementations."""
import json
import os
from pathlib import Path
import uuid

from .controller import NATIVE_TOOLS


def validate_mcp_config(servers, names):
    for name in names:
        server = servers.get(name)
        if not isinstance(server, dict):
            raise ValueError(f'MCP server must be configured in this profile: {name}')
        if (server.get('sampling') or {}).get('enabled') is not False:
            raise ValueError(f'MCP {name}: set sampling.enabled=false for LLM-free companion operation')
    return list(names)


class HermesRuntime:
    def __init__(self):
        os.environ['HERMES_TELEOPERATOR_COMPANION'] = '1'
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
        from model_tools import handle_function_call, registry
        self.dispatch_tool = handle_function_call
        self.registry = registry
        self.session_id = 'teleoperator-' + uuid.uuid4().hex
        from hermes_constants import get_hermes_home
        path = Path(get_hermes_home()) / 'teleoperator.json'
        config = json.loads(path.read_text()) if path.exists() else {}
        self.expose_mcp = config.get('expose_mcp', [])
        if not isinstance(self.expose_mcp, list) or not all(isinstance(x, str) for x in self.expose_mcp):
            raise ValueError('expose_mcp must list allowed remote MCP server names')
        self.mcp_loaded = False
        self.mcp_names = set()

    def _mcp(self):
        if not self.mcp_loaded:
            if self.expose_mcp:
                from hermes_cli.config import load_config
                validate_mcp_config(load_config().get('mcp_servers', {}), self.expose_mcp)
                from tools.mcp_tool_discovery import discover_mcp_tools
                self.mcp_names = set(discover_mcp_tools(allowed_mcp_names=self.expose_mcp))
            self.mcp_loaded = True

    def permitted(self, name):
        if name in NATIVE_TOOLS:
            return True
        if name.startswith('mcp__'):
            self._mcp()
            entry = self.registry.get_entry(name)
            # Compare against actual registered toolsets, not ambiguous prefix parsing.
            return entry is not None and name in self.mcp_names
        return False

    def tool(self, name, arguments):
        if not self.permitted(name):
            raise PermissionError(f'Tool not exposed by this companion: {name}')
        if not isinstance(arguments, dict):
            raise ValueError('arguments must be an object')
        raw = self.dispatch_tool(name, arguments, task_id=self.session_id, session_id=self.session_id)
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return {'content': raw}

    def catalog(self):
        self._mcp()
        return [self.registry.get_schema(name) for name in self.registry.get_all_tool_names()
                if self.permitted(name) and self.registry.get_schema(name)]

    def describe(self, name):
        if not self.permitted(name):
            raise PermissionError(f'Tool not exposed: {name}')
        schema = self.registry.get_schema(name)
        if not schema:
            raise ValueError(f'Unknown tool: {name}')
        return schema

    def search(self, query):
        words = str(query).lower().split()
        return {'tools': [s for s in self.catalog() if all(w in json.dumps(s).lower() for w in words)][:30]}

    def close(self):
        try:
            if self.mcp_loaded and self.expose_mcp:
                from tools.mcp_tool_lifecycle import shutdown_mcp_servers
                shutdown_mcp_servers()
        finally:
            try:
                from tools.terminal_tool_lifecycle import cleanup_all_environments
                cleanup_all_environments()
            finally:
                from hermes_cli.plugins import get_plugin_manager
                get_plugin_manager().unload()
