"""Native Hermes plugin using the documented middleware interface."""
import json
import os
from pathlib import Path
import shlex
import threading

from .controller import Controller
from .configuration import add_machine, load_settings

DESCRIPTION = ('Switch this conversation between local and configured SSH machines. Native terminal, '
               'file and skill tools follow the selected machine. Use search/describe/call for its MCPs. '
               'status shows location; back toggles; transfer copies or moves a file between named machines. '
               'Never assume remote failure means local execution.')
SCHEMA = {'name': 'teleoperator', 'description': DESCRIPTION, 'parameters': {
    'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['status', 'switch', 'back', 'reconnect', 'disconnect',
                                               'search', 'describe', 'call', 'transfer']},
        'machine': {'type': 'string'}, 'query': {'type': 'string'},
        'name': {'type': 'string'}, 'arguments': {'type': 'object'},
        'source_machine': {'type': 'string'}, 'source': {'type': 'string'},
        'destination_machine': {'type': 'string'}, 'destination': {'type': 'string'},
        'move': {'type': 'boolean', 'default': False},
    }, 'required': ['action'], 'additionalProperties': False}}


def identity(kwargs):
    # Hermes supplies these through documented tool/hook/command callbacks.
    # Never infer identity from process-global environment in a shared gateway.
    key = kwargs.get('session_id') or kwargs.get('session_key') or kwargs.get('task_id')
    if not isinstance(key, str) or not key:
        raise ValueError('No stable conversation identity; refusing ambiguous routing')
    return key


def config_path():
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except ImportError:
        home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    return Path(home) / 'teleoperator.json'


def session_profile():
    try:
        from gateway.session_context import get_session_env
        return get_session_env('HERMES_SESSION_PROFILE', '')
    except ImportError:
        return ''


def profile_for_home(path):
    try:
        from hermes_constants import profile_name_for_home
        return profile_name_for_home(path)
    except ImportError:
        return None


def register(ctx):
    # Registration is offline and side-effect-free. Connections start on explicit selection.
    controllers = {}
    init_lock = threading.RLock()

    def controller():
        with init_lock:
            path = config_path()
            if not controllers:
                settings = load_settings(path)
                controllers['value'] = Controller(
                    settings.get('machines', {}),
                    transfer_dir=settings.get('transfer_dir'),
                    allow_local_delete=settings.get('allow_local_delete') is True)
                controllers['path'] = path
            elif controllers['path'] != path:
                raise ValueError('Profile changed; refusing ambiguous configuration')
            return controllers['value']

    def handle(args, **kwargs):
        try:
            key, c = identity(kwargs), controller()
            action = args['action']
            if action == 'status': result = c.status(key)
            elif action == 'switch': result = c.switch(key, args['machine'])
            elif action == 'back': result = c.back(key)
            elif action == 'reconnect': result = c.reconnect(key, args.get('machine'))
            elif action == 'disconnect': result = c.disconnect(key, args.get('machine'))
            elif action in {'search', 'describe'}:
                result = c.call(key, action, {k: args[k] for k in ('query', 'name') if k in args}, args.get('machine'))
            elif action == 'call':
                result = c.call(key, 'tool', {'name': args['name'], 'arguments': args.get('arguments', {})}, args.get('machine'))
            elif action == 'transfer':
                result = c.transfer(key, args['source_machine'], args['source'], args['destination_machine'],
                                    args['destination'], args.get('move', False))
            else: raise ValueError('Unknown action')
            return json.dumps(result)
        except Exception as exc:
            return json.dumps({'error': str(exc), 'teleoperator': {'fallback': False}})

    def route(**kwargs):
        # Hermes execution middleware is fail-open if a callback raises. NEVER raise here.
        try:
            result = controller().route(identity(kwargs), kwargs['tool_name'], kwargs['args'], kwargs['next_call'])
            return json.dumps(result) if isinstance(result, dict) else result
        except Exception as exc:
            return json.dumps({'error': str(exc), 'teleoperator': {'fallback': False}})

    def context(**kwargs):
        try:
            state = controller().status(identity(kwargs))
            return {'context': 'Teleoperator location: ' + json.dumps(state) +
                    '\nUse teleoperator status after uncertainty. Remote native tools use that host, not the launch host. '
                    'Discover remote MCPs with teleoperator search/describe/call. Do not use execute_code remotely. '
                    'Web search/extract and human clarification remain local. Local memory is not migrated.'}
        except Exception:
            return None

    def command(raw_args, **kwargs):
        try:
            words = shlex.split(raw_args)
            action = words[0] if words else 'status'
            if action in {'status', 'back', 'disconnect', 'reconnect'}:
                args = {'action': action}
                if len(words) > 1: args['machine'] = words[1]
            else:
                args = {'action': 'switch', 'machine': words[-1]}
            return handle(args, **kwargs)
        except Exception as exc:
            return json.dumps({'error': str(exc)})

    def add_command(raw_args, **kwargs):
        try:
            key = identity(kwargs)
            path = config_path()
            profile = session_profile()
            if profile and profile_for_home(path.parent) != profile:
                raise ValueError('Focused conversation belongs to a different profile; refusing ambiguous configuration')
            c = controller()
            add_machine(controllers['path'], raw_args, c)
            return json.dumps(c.status(key))
        except Exception as exc:
            return json.dumps({'error': str(exc), 'teleoperator': {'fallback': False}})

    def cleanup():
        if controllers: controllers['value'].close()

    def session_end(**kwargs):
        try:
            if controllers: controller().close_session(identity(kwargs))
        except Exception:
            pass

    ctx.register_tool(name='teleoperator', toolset='teleoperator', schema=SCHEMA,
                      handler=handle, check_fn=lambda: True)
    ctx.register_middleware('tool_execution', route)
    ctx.register_hook('pre_llm_call', context)
    ctx.register_hook('on_session_end', session_end)
    ctx.register_command('teleport', command, description='Switch machine or show current location',
                         args_hint='[machine|local|back|status|reconnect|disconnect]')
    ctx.register_command('teleoperator-add', add_command, description='Add a machine without connecting',
                         args_hint='<JSON object>')
    ctx.on_unload(cleanup)
