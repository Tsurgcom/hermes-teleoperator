"""Opt-in native Hermes integration, separate subprocess and profile."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERMES_PYTHON = os.environ.get('TELEOPERATOR_HERMES_PYTHON')
HERMES_SOURCE = os.environ.get('TELEOPERATOR_HERMES_SOURCE')


@unittest.skipUnless(HERMES_PYTHON and HERMES_SOURCE, 'set TELEOPERATOR_HERMES_PYTHON and TELEOPERATOR_HERMES_SOURCE')
class NativeTests(unittest.TestCase):
    def test_native_plugin_runs_through_real_hermes_middleware(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'profile'
            (home / 'plugins').mkdir(parents=True)
            (home / 'plugins' / 'hermes-teleoperator').symlink_to(ROOT, target_is_directory=True)
            (home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['hermes-teleoperator']}}))
            script = '''import json
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
from model_tools import handle_function_call
print(handle_function_call('teleoperator', {'action': 'status'}, session_id='real-session'))
'''
            result = subprocess.run([HERMES_PYTHON, '-c', script], capture_output=True, text=True,
                env={**os.environ, 'HERMES_HOME': str(home), 'PYTHONPATH': str(ROOT) + ':' + HERMES_SOURCE},
                cwd=tmp, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"active": "local"', result.stdout, result.stdout + result.stderr)

    def test_remote_native_files_terminal_and_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'profile'
            skill = home / 'skills' / 'remote-only'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: remote-only\ndescription: Remote test skill.\n---\nREMOTE_SKILL_SENTINEL\n')
            fixture = Path(tmp) / 'input.txt'
            fixture.write_text('remote fixture')
            requests = [
                {'id': 1, 'method': 'tool', 'params': {'name': 'read_file', 'arguments': {'path': str(fixture)}}},
                {'id': 2, 'method': 'tool', 'params': {'name': 'skills_list', 'arguments': {}}},
                {'id': 3, 'method': 'tool', 'params': {'name': 'skill_view', 'arguments': {'name': 'remote-only'}}},
                {'id': 4, 'method': 'tool', 'params': {'name': 'terminal', 'arguments': {'command': 'pwd', 'workdir': tmp}}},
                {'id': 5, 'method': 'describe', 'params': {'name': 'read_file'}},
                {'id': 6, 'method': 'tool', 'params': {'name': 'terminal', 'arguments': {'command': 'cd ' + str(home)}}},
                {'id': 7, 'method': 'tool', 'params': {'name': 'terminal', 'arguments': {'command': 'pwd'}}},
                {'id': 8, 'method': 'tool', 'params': {'name': 'write_file', 'arguments': {'path': str(Path(tmp) / 'new.txt'), 'content': 'created remotely'}}},
                {'id': 9, 'method': 'tool', 'params': {'name': 'patch', 'arguments': {'path': str(Path(tmp) / 'new.txt'), 'old_string': 'created remotely', 'new_string': 'patched remotely'}}},
                {'id': 10, 'method': 'tool', 'params': {'name': 'read_file', 'arguments': {'path': str(Path(tmp) / 'new.txt')}}},
            ]
            env = {**os.environ, 'HERMES_HOME': str(home), 'TERMINAL_ENV': 'local',
                   'PYTHONPATH': str(ROOT) + os.pathsep + HERMES_SOURCE}
            result = subprocess.run([HERMES_PYTHON, '-m', 'hermes_teleoperator.companion'],
                input=''.join(json.dumps(x) + '\n' for x in requests), capture_output=True,
                text=True, env=env, cwd=tmp, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            responses = [json.loads(x) for x in result.stdout.splitlines()]
            self.assertEqual(len(responses), len(requests), result.stderr)
            for response in responses:
                self.assertNotIn('error', response, response)
            self.assertIn('remote fixture', json.dumps(responses[0]))
            self.assertIn('remote-only', json.dumps(responses[1]))
            self.assertIn('REMOTE_SKILL_SENTINEL', json.dumps(responses[2]))
            self.assertIn(tmp, json.dumps(responses[3]))
            self.assertIn('parameters', json.dumps(responses[4]))
            self.assertIn(str(home), json.dumps(responses[6]))
            self.assertEqual((Path(tmp) / 'new.txt').read_text(), 'patched remotely')
            self.assertIn('patched remotely', json.dumps(responses[9]))
