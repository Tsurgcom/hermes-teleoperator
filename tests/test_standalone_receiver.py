"""Black-box protocol tests for the standalone binary, never a Python companion.

Build receiver/ in release mode or set TELEOPERATOR_RECEIVER to an absolute
binary path. The chroot test additionally requires Linux root and chroot/ldd.
Python is the test driver only; it is deliberately absent inside the chroot.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from hermes_teleoperator.transport import RpcProcess, TransportError

ROOT = Path(__file__).resolve().parents[1]
RECEIVER = Path(os.environ.get('TELEOPERATOR_RECEIVER',
                              str(ROOT / 'receiver/target/release/hermes-teleoperator-receiver')))
MISSING_RECEIVER = ('standalone receiver unavailable: build receiver/ --release or set '
                    'TELEOPERATOR_RECEIVER to an absolute executable path')


def receiver_available():
    return RECEIVER.is_absolute() and RECEIVER.is_file() and os.access(RECEIVER, os.X_OK)


@unittest.skipUnless(receiver_available(), MISSING_RECEIVER)
class StandaloneReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='teleoperator-standalone-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.config = self.root / 'config'
        self.home.mkdir()
        self.config.mkdir()
        # No PYTHONPATH, HERMES_HOME, virtualenv, or inherited gateway settings.
        self.env = {'HOME': str(self.home), 'TELEOPERATOR_HOME': str(self.config),
                    'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        self.rpc = RpcProcess([str(RECEIVER)], env=self.env, cwd=str(self.home), timeout=15)
        self.addCleanup(self.rpc.close)

    def tool(self, tool_name, **arguments):
        return self.rpc.call('tool', {'name': tool_name, 'arguments': arguments})

    def test_protocol_identity_and_discoverable_core_schemas(self):
        ping = self.rpc.call('ping')
        self.assertEqual(ping['protocol'], 2)
        self.assertEqual(ping['runtime'], 'standalone-rust')
        from hermes_teleoperator.version import VERSION
        self.assertEqual(ping.get('version'), VERSION)
        self.assertEqual(ping['home'], str(self.home))
        self.assertEqual(ping['cwd'], str(self.home))
        self.assertFalse(ping['capabilities']['native_hermes_guards'])
        for name, required in [('read_file', ['path']), ('write_file', ['path', 'content']),
                               ('patch', ['path', 'old_string', 'new_string']),
                               ('skill_manage', ['operations'])]:
            with self.subTest(name=name):
                description = self.rpc.call('describe', {'name': name})
                function = description['function']
                self.assertEqual(function['name'], name)
                self.assertEqual(function['parameters']['required'], required)
                self.assertFalse(function['parameters']['additionalProperties'])
                self.assertIn(name, json.dumps(self.rpc.call('search', {'query': name})))

    def test_text_write_read_patch_and_regex_search(self):
        path = self.home / 'notes.txt'
        result = self.tool('write_file', path=str(path), content='first\nneedle one\nlast\n')
        self.assertTrue(result['verified'])
        read = self.tool('read_file', path=str(path), offset=2, limit=1)
        self.assertEqual(read['content'], '2|needle one')
        self.assertEqual(read['total_lines'], 3)
        self.tool('patch', path=str(path), old_string='needle one', new_string='needle two')
        self.assertEqual(path.read_text(), 'first\nneedle two\nlast\n')
        found = self.tool('search_files', path=str(self.home), pattern='needle (one|two)',
                          file_glob='*.txt', context=1)
        self.assertEqual(found['total_count'], 1)
        self.assertEqual(found['matches'][0]['line'], 2)
        self.assertEqual(found['matches'][0]['content'], 'needle two')
        names = self.tool('search_files', path=str(self.home), pattern='*.txt', target='files')
        self.assertEqual(names['files'], [str(path)])

    def test_ambiguous_patch_symlink_and_unknown_arguments_fail_without_mutation(self):
        path = self.home / 'repeated.txt'
        path.write_text('same same')
        with self.assertRaises(TransportError):
            self.tool('patch', path=str(path), old_string='same', new_string='changed')
        self.assertEqual(path.read_text(), 'same same')
        self.tool('patch', path=str(path), old_string='same', new_string='changed', replace_all=True)
        self.assertEqual(path.read_text(), 'changed changed')
        link = self.home / 'link.txt'
        link.symlink_to(path)
        with self.assertRaises(TransportError):
            self.tool('write_file', path=str(link), content='must not write')
        self.assertEqual(path.read_text(), 'changed changed')
        with self.assertRaises(TransportError):
            self.tool('read_file', path=str(path), invented_option=True)
        self.assertEqual(self.rpc.call('ping')['protocol'], 2)

    def test_skill_create_patch_list_and_view_are_config_home_scoped(self):
        content = ('---\nname: fixture-skill\ndescription: Integration fixture.\n---\n'
                   '# Fixture\nORIGINAL_MARKER\n')
        self.tool('skill_manage', operations=[{'action': 'create', 'name': 'fixture-skill',
                                               'content': content}])
        skill = self.config / 'skills/fixture-skill/SKILL.md'
        self.assertEqual(skill.read_text(), content)
        self.assertIn('fixture-skill', json.dumps(self.tool('skills_list')))
        self.assertIn('ORIGINAL_MARKER', json.dumps(self.tool('skill_view', name='fixture-skill')))
        self.tool('skill_manage', operations=[{'action': 'patch', 'name': 'fixture-skill',
            'old_string': 'ORIGINAL_MARKER', 'new_string': 'PATCHED_MARKER'}])
        self.assertIn('PATCHED_MARKER', skill.read_text())
        self.assertNotIn('ORIGINAL_MARKER', skill.read_text())
        self.tool('skill_manage', operations=[{'action': 'write_file', 'name': 'fixture-skill',
            'file_path': 'references/details.md', 'file_content': 'SUPPORTING_FILE'}])
        self.assertIn('SUPPORTING_FILE', json.dumps(self.tool('skill_view', name='fixture-skill',
                                                             file_path='references/details.md')))
        with self.assertRaises(TransportError):
            self.tool('skill_manage', operations=[{'action': 'create', 'name': '../escape',
                                                   'content': content}])
        self.assertFalse((self.config / 'escape').exists())
        self.assertFalse((self.home / 'skills').exists())

    def test_binary_transfer_checksums_no_overwrite_and_guarded_delete(self):
        payload = b'\x00standalone\xff\n' * 31
        digest = hashlib.sha256(payload).hexdigest()
        path = self.home / 'payload.bin'
        arguments = {'path': str(path), 'data': base64.b64encode(payload).decode(), 'sha256': digest}
        self.rpc.call('file_put', arguments)
        fetched = self.rpc.call('file_get', {'path': str(path)})
        self.assertEqual(base64.b64decode(fetched['data']), payload)
        self.assertEqual(fetched['sha256'], digest)
        self.assertEqual(fetched['size'], len(payload))
        with self.assertRaises(TransportError):
            self.rpc.call('file_put', arguments)
        with self.assertRaises(TransportError):
            self.rpc.call('file_remove', {'path': str(path), 'sha256': '0' * 64})
        self.assertEqual(path.read_bytes(), payload)
        self.rpc.call('file_remove', {'path': str(path), 'sha256': digest})
        self.assertFalse(path.exists())
        bad = {**arguments, 'sha256': '0' * 64}
        with self.assertRaises(TransportError):
            self.rpc.call('file_put', bad)
        self.assertFalse(path.exists())

    def test_shell_state_timeout_kills_descendants_and_recovers(self):
        directory = self.home / 'work'
        directory.mkdir()
        self.tool('terminal', command=f'export TELEOPERATOR_TEST_TOKEN=kept; cd {shlex.quote(str(directory))}')
        output = self.tool('terminal', command='printf "%s:%s" "$TELEOPERATOR_TEST_TOKEN" "$PWD"')
        self.assertEqual(output['output'], 'kept:' + str(directory))
        pidfile = self.home / 'descendant.pid'
        result = self.tool('terminal', command=f'sleep 30 & echo $! > {shlex.quote(str(pidfile))}; wait', timeout=1)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['shell_reset'])
        child = int(pidfile.read_text())
        # A killed orphan may briefly remain a zombie until PID 1 reaps it.
        stat = Path(f'/proc/{child}/stat')
        if stat.exists():
            self.assertEqual(stat.read_text().split(') ', 1)[1].split()[0], 'Z',
                             'timed-out descendant is still running')
        after = self.tool('terminal', command='printf "%s:%s" "$TELEOPERATOR_TEST_TOKEN" "$PWD"')
        self.assertEqual(after['output'], 'kept:' + str(directory))
        self.assertEqual(after['exit_code'], 0)

    def test_background_job_management_and_receiver_exit_cleanup(self):
        job = self.tool('terminal', command='sleep 30', background=True)
        self.assertEqual(job['status'], 'running')
        listed = self.tool('process_manage', action='list')
        self.assertIn(job['session_id'], json.dumps(listed))
        killed = self.tool('process_manage', action='kill', session_id=job['session_id'])
        self.assertEqual(killed['status'], 'exited')
        other = self.tool('terminal', command='sleep 30', background=True)
        self.rpc.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(other['pid'], 0)

    def test_malformed_frames_return_errors_then_valid_request_recovers(self):
        frames = [b'{not json}\n', b'{"id":"bad","method":12}\n',
                  b'{"id":"unknown","method":"not_a_method"}\n',
                  b'{"id":"good","method":"ping","params":{}}\n']
        result = subprocess.run([str(RECEIVER)], input=b''.join(frames), env=self.env,
                                cwd=self.home, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertIsNone(responses[0]['id'])
        for frame in responses[:3]:
            self.assertIn('error', frame)
        self.assertEqual(responses[1]['id'], 'bad')
        self.assertEqual(responses[3]['id'], 'good')
        self.assertEqual(responses[3]['result']['runtime'], 'standalone-rust')

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which('chroot') and shutil.which('ldd'),
                         'clean no-Python/no-Hermes proof requires root, chroot and ldd')
    def test_clean_chroot_runs_without_python_or_hermes_installed(self):
        jail = self.root / 'jail'
        jail.mkdir()

        def copy_executable(source, destination):
            target = jail / destination.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            libraries = subprocess.run(['ldd', str(source)], text=True, capture_output=True)
            # ldd exits 1 for a static binary, which needs no shared libraries.
            if libraries.returncode and not any(s in libraries.stdout + libraries.stderr
                                                 for s in ('not a dynamic executable', 'statically linked')):
                self.fail(libraries.stdout + libraries.stderr)
            for library in set(re.findall(r'(/[^\s()]+)', libraries.stdout)):
                libtarget = jail / library.lstrip('/')
                libtarget.parent.mkdir(parents=True, exist_ok=True)
                if not libtarget.exists():
                    shutil.copy2(library, libtarget)

        copy_executable(RECEIVER, '/receiver')
        copy_executable('/bin/bash', '/bin/bash')
        for directory in ('home/isolated', 'config', 'dev', 'usr/bin'):
            (jail / directory).mkdir(parents=True, exist_ok=True)
        (jail / 'dev/null').touch()
        self.assertFalse((jail / 'usr/bin/python3').exists())
        self.assertFalse(list(jail.rglob('model_tools*')))
        env = {'HOME': '/home/isolated', 'TELEOPERATOR_HOME': '/config', 'PATH': '/bin:/usr/bin'}
        with RpcProcess([shutil.which('chroot'), str(jail), '/receiver'], env=env, timeout=10) as rpc:
            self.assertEqual(rpc.call('ping')['runtime'], 'standalone-rust')
            result = rpc.call('tool', {'name': 'terminal', 'arguments': {
                'command': 'for tool in python python3 hermes; do if command -v "$tool"; then exit 91; fi; done; printf NO_HERMES_OR_PYTHON'}})
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual(result['output'], 'NO_HERMES_OR_PYTHON')
            rpc.call('tool', {'name': 'write_file', 'arguments': {
                'path': '/home/isolated/proof.txt', 'content': 'RUST_ONLY'}})
            read = rpc.call('tool', {'name': 'read_file', 'arguments': {'path': '/home/isolated/proof.txt'}})
            self.assertEqual(read['content'], '1|RUST_ONLY')
        self.assertEqual((jail / 'home/isolated/proof.txt').read_text(), 'RUST_ONLY')


if __name__ == '__main__':
    unittest.main()
