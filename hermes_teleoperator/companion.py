"""An SSH stdio companion, never an unauthenticated TCP listener."""
import json
import os
from pathlib import Path
import platform
import sys

PROTOCOL = 1
MAX_FRAME = 16 * 1024 * 1024


class Companion:
    def __init__(self):
        self.runtime = None

    def dispatch(self, method, params):
        if method in {'tool', 'search', 'describe'}:
            if self.runtime is None:
                from .runtime import HermesRuntime
                self.runtime = HermesRuntime()
            if method == 'tool':
                return self.runtime.tool(params['name'], params.get('arguments', {}))
            if method == 'describe':
                return self.runtime.describe(params['name'])
            return self.runtime.search(params.get('query', ''))
        if method in {'file_get', 'file_put', 'file_remove'}:
            from .files import file_operation
            return file_operation(method, params)
        if method == 'ping':
            return {
                'protocol': PROTOCOL, 'hostname': platform.node(),
                'platform': platform.platform(), 'home': str(Path.home()),
                'cwd': os.getcwd(), 'pid': os.getpid(),
                'hermes_home': os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')),
            }
        raise ValueError(f'Unknown method: {method}')


def serve(companion, source, sink):
    while True:
        line = source.readline(MAX_FRAME + 1)
        if not line:
            return
        if len(line) > MAX_FRAME:
            raise ValueError('Request exceeds frame limit')
        request_id = None
        try:
            req = json.loads(line)
            request_id = req['id']
            params = req.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            # All library/tool diagnostics go to stderr, never into framing.
            from contextlib import redirect_stdout
            with redirect_stdout(sys.stderr):
                result = companion.dispatch(req['method'], params)
            response = {'id': request_id, 'result': result}
        except Exception as exc:
            response = {'id': request_id, 'error': {'type': type(exc).__name__, 'message': str(exc)}}
        sink.write(json.dumps(response, ensure_ascii=True) + '\n')
        sink.flush()


def main():
    companion = Companion()
    try:
        serve(companion, sys.stdin, sys.stdout)
    finally:
        if companion.runtime is not None:
            from contextlib import redirect_stdout
            with redirect_stdout(sys.stderr):
                companion.runtime.close()


if __name__ == '__main__':
    main()
