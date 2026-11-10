"""One encrypted SSH stdio channel per machine and conversation."""
import json
import os
import queue
import shlex
import subprocess
import threading
import uuid

MAX_FRAME = 16 * 1024 * 1024

# The only variables an SSH client may inherit. SendEnv and ProxyCommand read
# the user's ssh config, so an inherited gateway environment could carry
# provider credentials off-host. Locale variables are added separately (LC_*).
SSH_ENV_KEYS = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'SSH_AUTH_SOCK', 'TERM')


class TransportError(RuntimeError):
    pass


def ssh_environment(source=None):
    """Return the explicit minimal environment for an SSH subprocess.

    Only allowlisted variables that are actually set are returned. HOME, USER
    and LOGNAME preserve the gateway user's SSH identity and configuration
    lookup, SSH_AUTH_SOCK supports agent authentication (ForwardAgent=no keeps
    the agent local), and PATH lets ssh run configured helpers. LANG and LC_*
    are locale hints; TERM is optional.
    """
    environ = os.environ if source is None else source
    env = {key: environ[key] for key in SSH_ENV_KEYS if key in environ}
    env.update((key, environ[key]) for key in environ
               if key.startswith('LC_') and key not in env)
    return env


def _is_ssh(argv):
    return bool(argv) and os.path.basename(os.fspath(argv[0])) == 'ssh'


def ssh_argv(target):
    host = target['host']
    if not isinstance(host, str) or host.startswith('-') or any(c.isspace() for c in host) or not host:
        raise ValueError('host must be an SSH destination or config alias, not options')
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
           '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=2']
    for key, flag in [('identity_file', '-i'), ('known_hosts', '-o')]:
        if target.get(key):
            value = target[key]
            if key == 'known_hosts':
                value = 'UserKnownHostsFile=' + value
            cmd.extend([flag, value])
    if 'port' in target:
        port = int(target['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid SSH port')
        cmd.extend(['-p', str(port)])
    if 'python' in target or 'hermes_home' in target:
        raise ValueError('legacy Python receiver configuration: reinstall the standalone receiver and use executable/teleoperator_home')
    remote = []
    if target.get('teleoperator_home'):
        remote += ['env', 'TELEOPERATOR_HOME=' + target['teleoperator_home']]
    executable = target.get('executable', '.local/bin/hermes-teleoperator-receiver')
    if not isinstance(executable, str) or not executable or executable.startswith('-'):
        raise ValueError('executable must be a receiver path')
    remote += [executable]
    cmd.extend([host, shlex.join(remote)])
    return cmd


class RpcProcess:
    def __init__(self, argv, timeout=120, env=None, cwd=None):
        self.timeout = timeout
        self._lock = threading.RLock()
        self._frames = queue.Queue()
        self._stderr = bytearray()
        self.closed = False
        # A caller that forgets env must never hand the gateway environment to
        # ssh: credentials would travel via SendEnv or a ProxyCommand helper.
        if env is None and _is_ssh(argv):
            env = ssh_environment()
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, cwd=cwd)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    self._frames.put(TransportError('Connection closed'))
                    return
                if len(line) > MAX_FRAME:
                    self._frames.put(TransportError('Response exceeds frame limit'))
                    return
                self._frames.put(line)
        except Exception as exc:
            self._frames.put(TransportError(str(exc)))

    def _read_stderr(self):
        while True:
            chunk = self.process.stderr.read1(1024)
            if not chunk:
                return
            self._stderr.extend(chunk)
            del self._stderr[:-8192]

    def call(self, method, params=None):
        with self._lock:
            if self.closed:
                raise TransportError('Connection closed; reconnect explicitly')
            request_id = uuid.uuid4().hex
            data = json.dumps({'id': request_id, 'method': method, 'params': params or {}}).encode() + b'\n'
            if len(data) > MAX_FRAME:
                raise TransportError('Request exceeds frame limit')
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                frame = self._frames.get(timeout=self.timeout)
                if isinstance(frame, Exception):
                    raise frame
                response = json.loads(frame)
                if not isinstance(response, dict) or response.get('id') != request_id:
                    raise ValueError('Invalid receiver response id')
                has_result = 'result' in response
                has_error = 'error' in response
                if has_result == has_error:
                    raise ValueError('Invalid receiver response envelope')
                if has_error:
                    error = response['error']
                    if not isinstance(error, dict) or not isinstance(error.get('message'), str):
                        raise ValueError('Invalid receiver response error')
            except Exception as exc:
                self.close()
                detail = self._stderr.decode(errors='replace').strip()
                raise TransportError(f'Connection failed; outcome may be unknown. No retry. {detail}') from exc
            if 'error' in response:
                raise TransportError(response['error']['message'])
            return response['result']

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
