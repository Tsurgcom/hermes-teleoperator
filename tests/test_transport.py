import sys
import unittest
from hermes_teleoperator.transport import RpcProcess, ssh_argv, TransportError


class TransportTests(unittest.TestCase):
    def test_persistent_companion_connection(self):
        with RpcProcess([sys.executable, '-m', 'hermes_teleoperator.companion']) as peer:
            one = peer.call('ping')
            two = peer.call('ping')
            self.assertEqual(one['pid'], two['pid'])
            with self.assertRaisesRegex(TransportError, 'Unknown method'):
                peer.call('missing')
            self.assertEqual(peer.call('ping')['protocol'], 1)

    def test_ssh_is_noninteractive_strict_and_no_forwarding(self):
        cmd = ssh_argv({'host': 'workstation', 'python': '/opt/Hermes Python/bin/python',
                        'hermes_home': '/home/me/My Profile'})
        self.assertEqual(cmd[0], 'ssh')
        for flag in ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'ForwardAgent=no', 'ClearAllForwardings=yes']:
            self.assertIn(flag, cmd)
        self.assertIn("'/opt/Hermes Python/bin/python'", cmd[-1])
        self.assertIn('HERMES_HOME=', cmd[-1])
        with self.assertRaises(ValueError):
            ssh_argv({'host': '-oProxyCommand=bad'})

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
