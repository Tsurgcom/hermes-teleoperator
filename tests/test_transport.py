import sys
import unittest
from hermes_teleoperator.transport import RpcProcess, ssh_argv, TransportError


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
        with self.assertRaisesRegex(ValueError, 'legacy'):
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
