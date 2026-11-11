"""Real OpenSSH + standalone Rust receiver; Python is gateway/test driver only.

Set TELEOPERATOR_RECEIVER to an absolute binary path, or build receiver/ with
--release. Root is used only for an isolated loopback sshd with temporary keys.
"""
import hashlib
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
from unittest.mock import patch

from hermes_teleoperator import guards
from hermes_teleoperator.controller import Controller
from hermes_teleoperator.transport import TransportError

ROOT = Path(__file__).resolve().parents[1]
RECEIVER = Path(os.environ.get('TELEOPERATOR_RECEIVER',
                              str(ROOT / 'receiver/target/release/hermes-teleoperator-receiver')))
PYTHON = os.environ.get('TELEOPERATOR_HERMES_PYTHON')
SOURCE = os.environ.get('TELEOPERATOR_HERMES_SOURCE')
MISSING_RECEIVER = ('standalone receiver unavailable: build receiver/ --release or set '
                    'TELEOPERATOR_RECEIVER to an absolute executable path')


@unittest.skipUnless(RECEIVER.is_absolute() and RECEIVER.is_file() and os.access(RECEIVER, os.X_OK),
                     MISSING_RECEIVER)
@unittest.skipUnless(shutil.which('sshd') and shutil.which('ssh') and
                     shutil.which('ssh-keygen') and os.geteuid() == 0,
                     'real SSH integration requires OpenSSH client/server and root for isolated sshd')
class RealSSHTests(unittest.TestCase):
    def test_two_homes_shell_state_mcp_transfer_fail_closed_and_forced_install(self):
        with tempfile.TemporaryDirectory(prefix='teleoperator-ssh-') as tmp:
            root = Path(tmp)
            for name in ('hostkey', 'clientkey'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                                '-f', str(root / name)], check=True, timeout=10)
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
            try:
                deadline = time.monotonic() + 5
                while True:
                    if server.poll() is not None:
                        self.fail(server.stderr.read().decode())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('isolated sshd did not become ready within five seconds')
                        time.sleep(.02)
                # This Python server is an MCP fixture's OWN dependency, not a
                # receiver dependency. The Rust receiver itself never imports it.
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
                    receiver_home = home / '.config/hermes-teleoperator'
                    skill = receiver_home / 'skills' / (name + '-skill')
                    skill.mkdir(parents=True)
                    (home / 'work').mkdir()
                    (skill / 'SKILL.md').write_text(
                        f'---\nname: {name}-skill\ndescription: Host-specific test.\n---\n{name.upper()}_SKILL\n')
                    (receiver_home / 'config.json').write_text(json.dumps({
                        'expose_mcp': ['fixture'],
                        'mcp_servers': {'fixture': {'command': '/usr/bin/python3', 'args': [str(mcp_server)]}}}))
                    wrapper = root / (name + '-receiver')
                    wrapper.write_text('#!/bin/sh\ncd ' + shlex.quote(str(home)) + '\nexec /usr/bin/env -i ' +
                        shlex.join(['HOME=' + str(home), 'TELEOPERATOR_HOME=' + str(receiver_home),
                                    'PATH=/usr/bin:/bin', str(RECEIVER)]) + '\n')
                    wrapper.chmod(0o700)
                    targets[name] = {'host': 'root@127.0.0.1', 'port': port,
                        'executable': str(wrapper), 'teleoperator_home': str(receiver_home),
                        'identity_file': str(root / 'clientkey'), 'known_hosts': str(known), 'timeout': 30}
                transfer_dir = root / 'transfer'
                transfer_dir.mkdir()
                # The SSH fixture may live under HERMES_HOME when TMPDIR does;
                # keep its transfer paths outside the protected roots.
                for target, value in (('_hermes_home', root / 'fake-hermes-home'),
                                      ('_gateway_home', root / 'fake-gateway-home')):
                    patcher = patch(f'hermes_teleoperator.files.{target}', return_value=value)
                    patcher.start()
                    self.addCleanup(patcher.stop)
                with Controller(targets, transfer_dir=transfer_dir, allow_local_delete=True,
                                guard=lambda name, args, *, machine: guards.Decision(True, source='test')) as c:
                    def local(_):
                        self.fail('Remote work leaked to local')

                    identity = c.switch('conversation', 'alpha')['identity']
                    self.assertEqual(identity['protocol'], 2)
                    self.assertEqual(identity['runtime'], 'standalone-rust')
                    self.assertEqual(identity['home'], str(root / 'alpha'))
                    result = c.route('conversation', 'skills_list', {}, local)
                    self.assertIn('alpha-skill', json.dumps(result))
                    self.assertNotIn('beta-skill', json.dumps(result))
                    result = c.route('conversation', 'terminal', {'command':
                        'test -z "${HERMES_HOME:-}${PYTHONPATH:-}${VIRTUAL_ENV:-}" && '
                        'export TELEOPERATOR_SSH_MARKER=alpha-kept; cd work; pwd'}, local)
                    self.assertEqual(result['exit_code'], 0, result)
                    self.assertIn(str(root / 'alpha/work'), result['output'])
                    c.switch('conversation', 'beta')
                    result = c.route('conversation', 'skills_list', {}, local)
                    self.assertIn('beta-skill', json.dumps(result))
                    self.assertNotIn('alpha-skill', json.dumps(result))
                    result = c.route('conversation', 'terminal', {'command':
                        'printf "%s:%s" "${TELEOPERATOR_SSH_MARKER-unset}" "$PWD"'}, local)
                    self.assertEqual(result['output'], 'unset:' + str(root / 'beta'))
                    c.route('conversation', 'terminal', {'command':
                        'export TELEOPERATOR_SSH_MARKER=beta-kept; cd work'}, local)
                    with self.subTest(feature='stdio MCP discovery'):
                        result = c.call('conversation', 'search', {'query': 'remote_marker'})
                        self.assertIn('mcp__fixture__remote_marker', json.dumps(result))
                    with self.subTest(feature='stdio MCP describe'):
                        description = c.call('conversation', 'describe', {'name': 'mcp__fixture__remote_marker'})
                        self.assertIn('remote_marker', json.dumps(description))
                    with self.subTest(feature='stdio MCP call'):
                        result = c.call('conversation', 'tool', {'name': 'mcp__fixture__remote_marker', 'arguments': {}})
                        self.assertIn('REAL_REMOTE_MCP_SENTINEL', json.dumps(result))
                    self.assertEqual(c.back('conversation')['active'], 'alpha')
                    result = c.route('conversation', 'terminal', {'command':
                        'printf "%s:%s" "$TELEOPERATOR_SSH_MARKER" "$PWD"'}, local)
                    self.assertEqual(result['output'], 'alpha-kept:' + str(root / 'alpha/work'))
                    c.switch('conversation', 'beta')
                    result = c.route('conversation', 'terminal', {'command':
                        'printf "%s:%s" "$TELEOPERATOR_SSH_MARKER" "$PWD"'}, local)
                    self.assertEqual(result['output'], 'beta-kept:' + str(root / 'beta/work'))
                    c.switch('conversation', 'alpha')
                    with self.subTest(feature='binary transfer no-overwrite and verified move'):
                        source = transfer_dir / 'local.bin'
                        payload = b'\x00secure-transfer\xff'
                        source.write_bytes(payload)
                        destination = root / 'alpha/arrived.bin'
                        destination.write_bytes(b'DO_NOT_OVERWRITE')
                        with self.assertRaises(TransportError):
                            c.transfer('conversation', 'local', str(source), 'alpha', str(destination), move=True)
                        self.assertEqual(destination.read_bytes(), b'DO_NOT_OVERWRITE')
                        self.assertEqual(source.read_bytes(), payload)
                        destination.unlink()
                        transfer = c.transfer('conversation', 'local', str(source), 'alpha', str(destination), move=True)
                        self.assertTrue(transfer['moved'])
                        self.assertEqual(transfer['sha256'], hashlib.sha256(payload).hexdigest())
                        self.assertEqual(transfer['size'], len(payload))
                        self.assertEqual(destination.read_bytes(), payload)
                        self.assertFalse(source.exists())
                        # Reverse move also exercises target-side checksum-guarded removal.
                        returned = transfer_dir / 'returned.bin'
                        c.transfer('conversation', 'alpha', str(destination), 'local', str(returned), move=True)
                        self.assertFalse(destination.exists())
                        self.assertEqual(returned.read_bytes(), payload)
                    c.disconnect('conversation')
                    result = c.route('conversation', 'terminal', {'command': 'pwd'}, local)
                    self.assertIn('error', result)
                    self.assertFalse(result['teleoperator']['fallback'])
                    self.assertEqual(c.status('conversation')['active'], 'alpha')
                    c.reconnect('conversation')
                    result = c.route('conversation', 'terminal', {'command':
                        'printf "%s:%s" "${TELEOPERATOR_SSH_MARKER-unset}" "$PWD"'}, local)
                    self.assertEqual(result['output'], 'unset:' + str(root / 'alpha'))
                    self.assertEqual(c.status('other-conversation')['active'], 'local')
                    c.switch('conversation', 'local')
                    self.assertEqual(c.route('conversation', 'terminal', {}, lambda _: 'LOCAL'), 'LOCAL')
                # A wrong pin must fail, without accepting/enrolling a key or
                # changing the conversation's active destination.
                bad_known = root / 'wrong_known_hosts'
                bad_known.write_text(f'[127.0.0.1]:{port} ' + (root / 'clientkey.pub').read_text())
                wrong_target = {**targets['alpha'], 'known_hosts': str(bad_known)}
                original_pin = bad_known.read_bytes()
                with Controller({'untrusted': wrong_target}) as c:
                    with self.assertRaises(TransportError) as failure:
                        c.switch('untrusted-session', 'untrusted')
                    self.assertIn('Host key changed', str(failure.exception))
                    self.assertIn('Verify the fingerprint', str(failure.exception))
                    self.assertEqual(c.status('untrusted-session')['active'], 'local')
                self.assertEqual(bad_known.read_bytes(), original_pin)
                # Optional gateway-native middleware only; no Hermes interpreter
                # or source is sent to any receiver process.
                if PYTHON and SOURCE:
                    self._check_gateway_middleware(root, targets)
                install_home = root / 'installed-home'
                install_home.mkdir(mode=0o700)
                installed = subprocess.run(['bash', str(ROOT / 'install.sh'),
                    '--binary', str(RECEIVER), '--sha256', hashlib.sha256(RECEIVER.read_bytes()).hexdigest(),
                    '--teleoperator-home', targets['alpha']['teleoperator_home'],
                    '--public-key-file', str(root / 'clientkey.pub'), '--allow-root'],
                    env={'PATH': '/usr/bin:/bin', 'HOME': str(install_home)},
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                launcher = install_home / '.local/bin/hermes-teleoperator-receiver'
                self.assertTrue(launcher.is_file(), installed.stdout)
                self.assertFalse((install_home / '.ssh').exists())
                entry = next(line for line in installed.stdout.splitlines()
                             if line.startswith('restrict,command='))
                self.assertIn(str(launcher), entry)
                # Explicit enrollment by this isolated test's administrator.
                authorized.write_text(entry + '\n')
                # Intentionally request an unusable executable: OpenSSH MUST run
                # the enrolled forced launcher instead of the requested command.
                forced = {**targets['alpha'], 'executable': '/bin/false'}
                with Controller({'installed': forced}) as c:
                    state = c.switch('installed-session', 'installed')
                    self.assertEqual(state['identity']['runtime'], 'standalone-rust')
                    output = c.call('installed-session', 'tool', {'name': 'terminal', 'arguments': {
                        'command': 'export INSTALLED_STATE=old; printf INSTALLED_OK'}})
                    self.assertEqual(output['output'], 'INSTALLED_OK')
                    c.disconnect('installed-session')
                    c.reconnect('installed-session')
                    self.assertEqual(c.status('installed-session')['active'], 'installed')
                    output = c.call('installed-session', 'tool', {'name': 'terminal', 'arguments': {
                        'command': 'printf "%s" "${INSTALLED_STATE-reset}"'}})
                    self.assertEqual(output['output'], 'reset')
            finally:
                if server.poll() is None:
                    server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                server.stderr.close()

    def _check_gateway_middleware(self, root, targets):
        controller_home = root / 'controller'
        (controller_home / 'plugins').mkdir(parents=True)
        (controller_home / 'plugins/hermes-teleoperator').symlink_to(ROOT, target_is_directory=True)
        (controller_home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['hermes-teleoperator']}}))
        (controller_home / 'teleoperator.json').write_text(json.dumps({'machines': targets}))
        script = '''import json
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from model_tools import handle_function_call
for name, args in [('teleoperator', {'action': 'switch', 'machine': 'beta'}), ('skills_list', {}), ('terminal', {'command': 'pwd'}), ('teleoperator', {'action': 'switch', 'machine': 'local'})]:
    print(handle_function_call(name, args, session_id='middleware-integration'))
'''
        run = subprocess.run([PYTHON, '-c', script], capture_output=True, text=True, cwd=root,
            env={**os.environ, 'HERMES_HOME': str(controller_home), 'PYTHONPATH': str(ROOT) + ':' + SOURCE},
            timeout=45)
        self.assertEqual(run.returncode, 0, run.stderr)
        frames = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(frames[0]['active'], 'beta')
        self.assertIn('beta-skill', json.dumps(frames[1]))
        self.assertEqual(frames[2]['teleoperator']['machine'], 'beta')
        self.assertIn(str(root / 'beta'), json.dumps(frames[2]))
        self.assertEqual(frames[3]['active'], 'local')


if __name__ == '__main__':
    unittest.main()
