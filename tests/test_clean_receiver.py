"""Clean receiver root: no Hermes, Python, Node, Bun, Cargo or network access required.

The test runner is Python on the gateway. Its chroot child sees only the explicitly
copied Bash/coreutils runtime and receiver. No host Python libraries are mounted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from hermes_teleoperator.transport import RpcProcess

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('TELEOPERATOR_RECEIVER', str(ROOT / 'receiver/target/release/hermes-teleoperator-receiver')))


@unittest.skipUnless(os.geteuid() == 0 and shutil.which('chroot') and BINARY.is_file(),
                     'requires built standalone receiver, root and chroot')
class CleanReceiverTests(unittest.TestCase):
    def test_install_shell_skills_transfer_and_mcp_without_hermes_or_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            jail = Path(tmp)

            def copy_program(program):
                source = Path(shutil.which(program))
                target = jail / source.relative_to('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                result = subprocess.run(['ldd', str(source)], capture_output=True, text=True)
                for dep in re.findall(r'(/[^\s()]+)', result.stdout):
                    origin = Path(dep)
                    if origin.is_file():
                        dest = jail / origin.relative_to('/')
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(origin, dest)
                return str(source)

            # Runtime needed by install.sh. No language runtime is copied.
            for program in ('bash', 'sh', 'id', 'uname', 'stat', 'dirname', 'mkdir',
                            'chmod', 'mktemp', 'cp', 'sha256sum', 'mv', 'rm', 'cat',
                            'sleep', 'head'):
                copy_program(program)
            # Linux may use /bin as a merged-/usr symlink; preserve both entry paths.
            (jail / 'bin').mkdir(exist_ok=True)
            for name in ('bash', 'sh', 'sleep', 'cat', 'mkdir'):
                target = jail / 'bin' / name
                if not target.exists():
                    target.symlink_to('/usr/bin/' + name)
            (jail / 'dev').mkdir()
            # A regular /dev/null suffices for this closed test root (no mknod needed).
            (jail / 'dev/null').touch()
            (jail / 'etc').mkdir()
            (jail / 'etc/hostname').write_text('clean-receiver\n')
            (jail / 'home/target').mkdir(parents=True)
            shutil.copy2(BINARY, jail / 'receiver')
            # Development GNU builds may link libgcc; musl release builds do not.
            deps = subprocess.run(['ldd', str(BINARY)], capture_output=True, text=True)
            for dep in re.findall(r'(/[^\s()]+)', deps.stdout):
                origin = Path(dep)
                if origin.is_file():
                    dest = jail / origin.relative_to('/')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, dest)
            (jail / 'receiver').chmod(0o700)
            shutil.copy2(ROOT / 'install.sh', jail / 'install.sh')
            digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
            env = {'PATH': '/usr/bin:/bin', 'HOME': '/home/target', 'LC_ALL': 'C'}
            installed = subprocess.run([shutil.which('chroot'), str(jail), '/bin/bash', '/install.sh',
                '--binary', '/receiver', '--sha256', digest, '--allow-root'],
                env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            home = jail / 'home/target/.config/hermes-teleoperator'
            fixture = jail / 'fixture.sh'
            fixture.write_text('''#!/bin/bash
while IFS= read -r request; do
  if [[ $request =~ \\"id\\":[[:space:]]*([0-9]+) ]]; then
    id=${BASH_REMATCH[1]}
    case "$request" in
      *'"method":"initialize"'*) result='{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"bash-fixture","version":"1"}}' ;;
      *'"method":"tools/list"'*) result='{"tools":[{"name":"marker","description":"Clean MCP fixture","inputSchema":{"type":"object","properties":{}}}]}' ;;
      *'"method":"tools/call"'*) result='{"content":[{"type":"text","text":"CLEAN_MCP_WITHOUT_PYTHON"}]}' ;;
      *) result='{}' ;;
    esac
    printf '{"jsonrpc":"2.0","id":%s,"result":%s}\\n' "$id" "$result"
  fi
done
''')
            (home / 'config.json').write_text(json.dumps({'expose_mcp': ['fixture'],
                'mcp_servers': {'fixture': {'command': '/bin/bash', 'args': ['/fixture.sh']}}}))
            skill = home / 'skills/clean-skill'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: clean-skill\ndescription: Clean target skill\n---\nNO_HERMES_INSTALLED\n')
            # Hard assertion on the destination root, not just an import failure.
            for name in ('python', 'python3', 'node', 'bun', 'cargo', 'hermes'):
                for directory in ('bin', 'usr/bin', 'usr/local/bin'):
                    self.assertFalse((jail / directory / name).exists(), name)
            self.assertFalse((jail / 'root/.hermes').exists())
            with RpcProcess([shutil.which('chroot'), str(jail), '/home/target/.local/bin/hermes-teleoperator-receiver'],
                            env=env, timeout=15) as peer:
                ping = peer.call('ping')
                self.assertEqual(ping['protocol'], 2)
                self.assertEqual(ping['runtime'], 'standalone-rust')
                def tool(name, arguments):
                    return peer.call('tool', {'name': name, 'arguments': arguments})
                shell = tool('terminal', {'command': 'export CLEAN_STATE=yes; cd /home/target; printf CLEAN_SHELL_OK'})
                self.assertIn('CLEAN_SHELL_OK', json.dumps(shell))
                self.assertIn('yes', json.dumps(tool('terminal', {'command': 'printf "$CLEAN_STATE"; pwd'})))
                self.assertIn('clean-skill', json.dumps(tool('skills_list', {})))
                self.assertIn('NO_HERMES_INSTALLED', json.dumps(tool('skill_view', {'name': 'clean-skill'})))
                data = {'path': '/home/target/transfer.bin', 'data': 'AP8=',
                        'sha256': hashlib.sha256(b'\x00\xff').hexdigest()}
                peer.call('file_put', data)
                self.assertEqual(peer.call('file_get', {'path': data['path']})['sha256'], data['sha256'])
                self.assertIn('mcp__fixture__marker', json.dumps(peer.call('search', {'query': 'marker'})))
                result = tool('mcp__fixture__marker', {})
                self.assertIn('CLEAN_MCP_WITHOUT_PYTHON', json.dumps(result))
