"""Real loopback OpenSSH tunnel, native Hermes tools and a real stdio MCP server."""
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from hermes_teleoperator.controller import Controller

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('TELEOPERATOR_HERMES_PYTHON')
SOURCE = os.environ.get('TELEOPERATOR_HERMES_SOURCE')


@unittest.skipUnless(PYTHON and SOURCE and shutil.which('sshd') and os.geteuid() == 0,
                     'requires opt-in Hermes interpreter/source, OpenSSH and root for isolated sshd')
class RealSSHTests(unittest.TestCase):
    def test_two_profiles_switch_native_tools_mcp_transfer_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('hostkey', 'clientkey'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / name)], check=True)
            authorized = root / 'authorized_keys'
            authorized.write_text((root / 'clientkey.pub').read_text())
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            config = root / 'sshd_config'
            config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {root / 'hostkey'}
PidFile {root / 'sshd.pid'}
AuthorizedKeysFile {authorized}
StrictModes no
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AllowUsers root
AllowTcpForwarding no
X11Forwarding no
''')
            known = root / 'known_hosts'
            pub = (root / 'hostkey.pub').read_text().split()
            known.write_text(f'[127.0.0.1]:{port} {pub[0]} {pub[1]}\n')
            server = subprocess.Popen([shutil.which('sshd'), '-D', '-e', '-f', str(config)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.addCleanup(lambda: server.poll() is None and server.kill())
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail(server.stderr.read().decode())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1): break
                    except OSError: time.sleep(.02)
                wrapper = root / 'remote-python'
                wrapper.write_text('#!/bin/sh\nexport PYTHONPATH=' + shlex.quote(str(ROOT) + ':' + SOURCE) +
                                   '\nexec ' + shlex.quote(PYTHON) + ' "$@"\n')
                wrapper.chmod(0o700)
                mcp_server = root / 'test_mcp.py'
                mcp_server.write_text('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test-fixture', 'version': '1.0'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'remote_marker', 'description': 'Integration fixture marker', 'inputSchema': {'type': 'object', 'properties': {}}, 'annotations': {'readOnlyHint': True}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': 'REAL_REMOTE_MCP_SENTINEL'}]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
''')
                targets = {}
                for name in ('alpha', 'beta'):
                    home = root / name
                    skill = home / 'skills' / (name + '-skill')
                    skill.mkdir(parents=True)
                    (skill / 'SKILL.md').write_text(f'---\nname: {name}-skill\ndescription: Host-specific test.\n---\n{name.upper()}_SKILL\n')
                    (home / 'config.yaml').write_text(json.dumps({'terminal': {'backend': 'local', 'cwd': str(home)},
                        'mcp_servers': {'fixture': {'command': PYTHON, 'args': [str(mcp_server)], 'sampling': {'enabled': False}}}}))
                    (home / 'teleoperator.json').write_text(json.dumps({'expose_mcp': ['fixture']}))
                    targets[name] = {'host': 'root@127.0.0.1', 'port': port,
                        'python': str(wrapper), 'hermes_home': str(home),
                        'identity_file': str(root / 'clientkey'), 'known_hosts': str(known), 'timeout': 45}
                with Controller(targets) as c:
                    local = lambda args: self.fail('Remote work leaked to local')
                    c.switch('conversation', 'alpha')
                    result = c.route('conversation', 'skills_list', {}, local)
                    self.assertIn('alpha-skill', json.dumps(result))
                    result = c.route('conversation', 'terminal', {'command': 'pwd'}, local)
                    self.assertIn(str(root / 'alpha'), json.dumps(result))
                    c.switch('conversation', 'beta')
                    result = c.route('conversation', 'skills_list', {}, local)
                    self.assertIn('beta-skill', json.dumps(result))
                    self.assertNotIn('alpha-skill', json.dumps(result))
                    result = c.call('conversation', 'search', {'query': 'remote_marker'})
                    self.assertIn('mcp__fixture__remote_marker', json.dumps(result), c.sessions['conversation'].peers['beta']._stderr.decode(errors='replace'))
                    result = c.call('conversation', 'tool', {'name': 'mcp__fixture__remote_marker', 'arguments': {}})
                    self.assertIn('REAL_REMOTE_MCP_SENTINEL', json.dumps(result))
                    self.assertEqual(c.back('conversation')['active'], 'alpha')
                    source = root / 'local.bin'
                    source.write_bytes(b'\x00secure-transfer\xff')
                    destination = root / 'alpha' / 'arrived.bin'
                    transfer = c.transfer('conversation', 'local', str(source), 'alpha', str(destination), move=True)
                    self.assertTrue(transfer['moved'])
                    self.assertEqual(destination.read_bytes(), b'\x00secure-transfer\xff')
                    self.assertFalse(source.exists())
                    c.disconnect('conversation')
                    result = c.route('conversation', 'terminal', {'command': 'pwd'}, local)
                    self.assertIn('error', result)
                    self.assertEqual(c.status('conversation')['active'], 'alpha')
                    c.reconnect('conversation')
                    self.assertEqual(c.status('other-conversation')['active'], 'local')
                    c.switch('conversation', 'local')
                    self.assertEqual(c.route('conversation', 'terminal', {}, lambda _: 'LOCAL'), 'LOCAL')
                # Exercise actual plugin discovery + middleware, not just Controller directly.
                controller_home = root / 'controller'
                (controller_home / 'plugins').mkdir(parents=True)
                (controller_home / 'plugins' / 'hermes-teleoperator').symlink_to(ROOT, target_is_directory=True)
                (controller_home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['hermes-teleoperator']}}))
                (controller_home / 'teleoperator.json').write_text(json.dumps({'machines': targets}))
                script = '''import json
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from model_tools import handle_function_call
for name, args in [('teleoperator', {'action': 'switch', 'machine': 'beta'}), ('skills_list', {}), ('terminal', {'command': 'pwd'}), ('teleoperator', {'action': 'switch', 'machine': 'local'})]:
    print(handle_function_call(name, args, session_id='middleware-integration'))
'''
                run = subprocess.run([PYTHON, '-c', script], capture_output=True, text=True, cwd=tmp,
                    env={**os.environ, 'HERMES_HOME': str(controller_home), 'PYTHONPATH': str(ROOT) + ':' + SOURCE}, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
                frames = [json.loads(line) for line in run.stdout.splitlines()]
                self.assertEqual(frames[0]['active'], 'beta')
                self.assertIn('beta-skill', json.dumps(frames[1]))
                self.assertEqual(frames[2]['teleoperator']['machine'], 'beta')
                self.assertIn(str(root / 'beta'), json.dumps(frames[2]))
                self.assertEqual(frames[3]['active'], 'local')
                # A different, pinned host key must fail rather than enroll or prompt.
                known.write_text(f'[127.0.0.1]:{port} ' + (root / 'clientkey.pub').read_text())
                with Controller(targets) as c:
                    with self.assertRaises(Exception): c.switch('untrusted', 'alpha')
                    self.assertEqual(c.status('untrusted')['active'], 'local')
            finally:
                server.terminate()
                try: server.wait(timeout=5)
                except subprocess.TimeoutExpired: server.kill(); server.wait()
                server.stderr.close()
