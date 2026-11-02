"""Measure the standalone receiver as built, with stated conditions."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [
    ROOT / 'receiver/target/x86_64-unknown-linux-musl/release/hermes-teleoperator-receiver',
    ROOT / 'receiver/target/release/hermes-teleoperator-receiver',
]


def measure(binary, commands=0):
    start = time.perf_counter()
    process = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None and stdout is not None
    try:
        stdin.write(b'{"id":1,"method":"ping"}\n')
        stdin.flush()
        ping = json.loads(stdout.readline())
        cold_ms = (time.perf_counter() - start) * 1000
        for index in range(commands):
            request = {'id': index + 2, 'method': 'tool', 'params': {
                'name': 'terminal', 'arguments': {'command': 'true'}}}
            stdin.write(json.dumps(request).encode() + b'\n')
            stdin.flush()
            json.loads(stdout.readline())
        time.sleep(0.5)
        status = Path(f'/proc/{process.pid}/status').read_text()
        rss_kib = next(int(line.split()[1]) for line in status.splitlines() if line.startswith('VmRSS'))
        return {'cold_start_ms': round(cold_ms, 2), 'receiver_rss_kib': rss_kib,
                'commands_run': commands, 'protocol': ping['result']['protocol'],
                'runtime': ping['result']['runtime']}
    finally:
        stdin.close()
        process.wait(timeout=5)


if __name__ == '__main__':
    binary = Path(os.environ.get('TELEOPERATOR_RECEIVER') or next(
        (path for path in CANDIDATES if path.is_file()), CANDIDATES[0]))
    if not binary.is_file():
        sys.exit('Build the receiver first: cargo build --release --locked --manifest-path receiver/Cargo.toml')
    report = {'binary': str(binary.relative_to(ROOT)) if str(binary).startswith(str(ROOT)) else str(binary),
              'binary_bytes': binary.stat().st_size,
              'environment': {'kernel': os.uname().sysname + ' ' + os.uname().release,
                              'machine': os.uname().machine, 'cpu_count': os.cpu_count()},
              'measurements': [measure(binary, count) for count in (0, 20)]}
    # RSS includes the idle bash shell required for persistent cwd/env, not an agent or model.
    report['notes'] = [
        'Cold start is spawn-to-first-response latency, not a network or SSH round trip.',
        'RSS is this receiver plus its idle Bash shell after commands; MCP server processes are separate.',
        'Receiver is on demand. It exits when SSH stdin reaches EOF; it is not an always-on daemon.',
    ]
    print(json.dumps(report, indent=2))
