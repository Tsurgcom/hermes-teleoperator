"""One encrypted SSH stdio channel per machine and conversation."""
import json
import queue
import shlex
import subprocess
import threading
import uuid

MAX_FRAME = 16 * 1024 * 1024


class TransportError(RuntimeError):
    pass


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
    remote = []
    if target.get('hermes_home'):
        remote += ['env', 'HERMES_HOME=' + target['hermes_home']]
    remote += [target.get('python', 'python3'), '-m', 'hermes_teleoperator.companion']
    cmd.extend([host, shlex.join(remote)])
    return cmd


class RpcProcess:
    def __init__(self, argv, timeout=120, env=None, cwd=None):
        self.timeout = timeout
        self._lock = threading.RLock()
        self._frames = queue.Queue()
        self._stderr = bytearray()
        self.closed = False
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
                if response.get('id') != request_id:
                    raise ValueError('Mismatched response id')
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
