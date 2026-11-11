import io
import os
import sys
import unittest
from unittest.mock import patch
from hermes_teleoperator.transport import RpcProcess, ssh_argv, ssh_environment, describe_ssh_failure, TransportError


class SpyProcess:
    """Records Popen arguments and behaves like an exited child with empty pipes."""

    def __init__(self, calls, argv, **kwargs):
        calls.append((argv, kwargs))
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9


class TransportTests(unittest.TestCase):
    def test_persistent_receiver_connection(self):
        import os
        from pathlib import Path
        binary = Path(os.environ.get('TELEOPERATOR_RECEIVER', str(Path(__file__).resolve().parents[1] / 'receiver/target/release/hermes-teleoperator-receiver')))
        if not binary.is_file():
            self.skipTest('build standalone receiver first')
        with RpcProcess([str(binary)]) as peer:
            first = peer.call('ping')
            second = peer.call('ping')
            self.assertEqual(first['pid'], second['pid'])
            with self.assertRaisesRegex(TransportError, 'Unknown|unknown'):
                peer.call('no_such_method')
            self.assertEqual(peer.call('ping')['protocol'], 2)

    def test_ssh_is_noninteractive_strict_and_no_forwarding(self):
        cmd = ssh_argv({'host': 'workstation', 'executable': '/opt/My Tools/receiver',
                        'teleoperator_home': '/home/me/My Profile'})
        self.assertEqual(cmd[0], 'ssh')
        for flag in ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'ForwardAgent=no', 'ClearAllForwardings=yes']:
            self.assertIn(flag, cmd)
        self.assertIn("'/opt/My Tools/receiver'", cmd[-1])
        self.assertIn('TELEOPERATOR_HOME=', cmd[-1])
        with self.assertRaises(ValueError):
            ssh_argv({'host': '-oProxyCommand=bad'})

    def test_ssh_launches_standalone_receiver_not_python(self):
        cmd = ssh_argv({'host': 'workstation', 'executable': '/home/me/My Tools/teleoperator',
                        'teleoperator_home': '/home/me/My Profile'})
        self.assertIn("'/home/me/My Tools/teleoperator'", cmd[-1])
        self.assertIn('TELEOPERATOR_HOME=', cmd[-1])
        self.assertNotIn('python', cmd[-1])
        self.assertNotIn('hermes_teleoperator.companion', cmd[-1])
        with self.assertRaisesRegex(ValueError, 'Legacy Python receiver'):
            ssh_argv({'host': 'workstation', 'python': '/old/python'})

    def test_close_allows_remote_cleanup_on_eof(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'cleanup'
            code = 'import sys,pathlib; sys.stdin.read(); pathlib.Path(sys.argv[1]).write_text("cleaned")'
            peer = RpcProcess([sys.executable, '-c', code, str(marker)])
            peer.close()
            self.assertEqual(marker.read_text(), 'cleaned')

    def test_timeout_poisoned_connection_does_not_retry(self):
        with RpcProcess([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.1) as peer:
            with self.assertRaises(TransportError):
                peer.call('ping')
            with self.assertRaisesRegex(TransportError, 'closed'):
                peer.call('ping')

    def test_stderr_after_dispatched_request_does_not_hide_unknown_outcome(self):
        code = '''import sys
sys.stdin.readline()
sys.stderr.write("ssh: connect to host host port 22: Connection timed out\\n")
sys.stderr.flush()
'''
        with RpcProcess([sys.executable, '-c', code]) as peer:
            with self.assertRaises(TransportError) as caught:
                peer.call('call', {'mutation': True})
            self.assertRegex(str(caught.exception), r'(?i)outcome.*unknown')
            self.assertIn('No answer from the host', str(caught.exception))
            self.assertIn('not retried', str(caught.exception))

    def test_malformed_receiver_error_closes_connection(self):
        code = '''import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"id": request["id"], "error": "not an error object"}), flush=True)
sys.stdin.read()
'''
        with RpcProcess([sys.executable, '-c', code]) as peer:
            with self.assertRaisesRegex(TransportError, 'not retried'):
                peer.call('ping')
            self.assertTrue(peer.closed)


POLLUTED_ENV = {
    'PATH': '/usr/local/bin:/usr/bin', 'HOME': '/home/alice', 'USER': 'alice',
    'LOGNAME': 'alice', 'LANG': 'en_GB.UTF-8', 'LC_ALL': 'C.UTF-8',
    'LC_MESSAGES': 'C', 'SSH_AUTH_SOCK': '/run/user/1000/ssh-agent.sock',
    'TERM': 'xterm-256color', 'OPENAI_API_KEY': 'gateway-placeholder',
    'ANTHROPIC_API_KEY': 'anthropic-placeholder',
    'AWS_SECRET_ACCESS_KEY': 'aws-gateway-secret', 'HERMES_HOME': '/gateway/private',
    'SSH_ASKPASS': '/gateway/askpass', 'TERM_PROGRAM': 'WezTerm',
}
ALLOWED_ENV = {key: POLLUTED_ENV[key] for key in (
    'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_MESSAGES',
    'SSH_AUTH_SOCK', 'TERM')}


class SshEnvironmentTests(unittest.TestCase):
    def test_rpc_ssh_spawns_with_exactly_the_allowlisted_environment(self):
        calls = []
        target = {'host': 'workstation-alias', 'identity_file': '/keys/teleoperator',
                  'known_hosts': '/keys/known_hosts'}
        with patch.dict(os.environ, POLLUTED_ENV, clear=True), \
                patch('hermes_teleoperator.transport.subprocess.Popen',
                      side_effect=lambda argv, **kwargs: SpyProcess(calls, argv, **kwargs)):
            RpcProcess(ssh_argv(target)).close()
        self.assertEqual(len(calls), 1)
        argv, options = calls[0]
        self.assertEqual(options['env'], ALLOWED_ENV)
        # The allowlist must not disturb identity, known_hosts or config alias handling.
        self.assertEqual(argv[argv.index('-i') + 1], '/keys/teleoperator')
        self.assertIn('UserKnownHostsFile=/keys/known_hosts', argv)
        self.assertEqual(argv[-2], 'workstation-alias')

    def test_ssh_environment_omits_variables_the_gateway_did_not_set(self):
        env = ssh_environment({'PATH': '/usr/bin', 'HOME': '/home/alice',
                               'LC_CTYPE': 'C.UTF-8', 'GITHUB_TOKEN': 'secret'})
        self.assertEqual(env, {'PATH': '/usr/bin', 'HOME': '/home/alice',
                               'LC_CTYPE': 'C.UTF-8'})


class SshFailureMessageTests(unittest.TestCase):
    """Unreadable ssh stderr must reach the user as one actionable sentence."""

    def test_unknown_host_key_names_the_host_and_the_fix(self):
        message = describe_ssh_failure(
            'No ED25519 host key is known for 127.0.0.1 and you have requested strict checking.\n'
            'Host key verification failed.') or ''
        self.assertEqual(message, 'Host key for 127.0.0.1 is not trusted. Connect once with ssh to trust it.')

    def test_changed_host_key_asks_for_verification_not_trust(self):
        message = describe_ssh_failure('@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@') or ''
        self.assertEqual(message, 'Host key changed. Verify the fingerprint, then update known-hosts.')

    def test_refused_connection_reports_the_port(self):
        message = describe_ssh_failure('ssh: connect to host 10.0.0.5 port 2222: Connection refused') or ''
        self.assertEqual(message, 'SSH refused on port 2222. Check that sshd is running.')

    def test_unresolvable_host_drops_the_trailing_colon(self):
        message = describe_ssh_failure('ssh: Could not resolve hostname nope.example: Name or service not known') or ''
        self.assertEqual(message, 'Could not resolve nope.example. Check the machine address.')

    def test_rejected_key_is_not_reported_as_a_retryable_mistake(self):
        message = describe_ssh_failure('me@host: Permission denied (publickey,password).') or ''
        self.assertEqual(message, 'SSH key rejected. Authorize this key on the remote host.')

    def test_unrecognised_stderr_is_not_guessed_at(self):
        self.assertIsNone(describe_ssh_failure('kex_exchange_identification: read: Connection reset by peer'))
        self.assertIsNone(describe_ssh_failure(''))
