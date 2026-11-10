const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load(request) {
  const file = require('node:path').join(__dirname, '../desktop/plugin.js');
  assert.ok(fs.existsSync(file), 'desktop companion must exist');
  let source = fs.readFileSync(file, 'utf8')
    .replace(/^import .*$/gm, '')
    .replace('export default ', 'globalThis.plugin = ')
    .replace(/export (async )?function /g, '$1function ');
  const sandbox = { host: { request, state: { focusedSessionOwner: { get: () => ({ connectionId: 'local', profile: 'default' }) } }, profileRoutes: async () => [{ connectionId: 'local', profile: 'default', targetProfile: 'default' }], requestProfile: async (_route, method, params) => request(method, params) }, jsx: () => null, useState: () => [], useEffect: () => {}, useValue: () => null };
  vm.runInNewContext(source + '\nglobalThis.runCommand = runCommand; globalThis.Companion = Companion; globalThis.AddMachineForm = AddMachineForm; globalThis.saveMachine = typeof saveMachine === "undefined" ? null : saveMachine;', sandbox);
  return sandbox;
}

function render(status, request = async () => {}, options = {}) {
  const box = load(request);
  box.host.state = { ...box.host.state, focusedSessionId: { get: () => 's' }, busy: { get: () => false } };
  box.host.composer = options.composer;
  box.setTimeout = options.setTimeout || setTimeout;
  box.clearTimeout = options.clearTimeout || clearTimeout;
  let index = 0;
  const state = [status, options.error || '', false, true, null, options.spinning || false, options.adding || false, false];
  box.useState = () => { const i = index++; return [state[i], value => { state[i] = value; options.onState?.(i, value); }]; };
  box.useRef = value => ({ current: value });
  box.jsx = box.jsxs = (type, props) => ({ type, props });
  for (const name of ['Button', 'Popover', 'PopoverContent', 'PopoverTrigger', 'Codicon']) box[name] = name;
  return box.Companion({ sessionId: 's', busy: !!options.busy });
}
function flatten(node) {
  if (node == null || typeof node !== 'object') return [String(node ?? '')];
  if (Array.isArray(node)) return node.flatMap(flatten);
  return [node, ...flatten(node.props?.children)];
}
test('loading state does not flash an ellipsis', () => {
  const nodes = flatten(render(null));
  const chip = nodes.find(node => node?.type === 'button' && node.props.title === 'Teleoperator: conversation machine');
  assert.equal(chip.props.children, 'Teleoperator');
});

test('error shows only Teleoperator in theme error color with accessible explanation', () => {
  const nodes = flatten(render(null, async () => {}, { error: 'unsupported_command' }));
  const chip = nodes.find(node => node?.type === 'button' && node.props.title === 'Teleoperator: conversation machine');
  assert.equal(chip.props.children, 'Teleoperator');
  assert.equal(chip.props.style.color, 'var(--ui-red)');
  assert.match(chip.props['aria-label'], /unavailable/i);
});

test('refresh keeps the last error until the request finishes', async () => {
  let resolveRequest;
  const changes = [];
  const nodes = flatten(render(null, () => new Promise(resolve => { resolveRequest = resolve; }), {
    error: 'unavailable', onState: (i, value) => changes.push([i, value]),
    setTimeout: () => 1, clearTimeout: () => {}
  }));
  nodes.find(x => x?.props?.['aria-label'] === 'Refresh').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!changes.some(([i]) => i === 1), 'error changed before completion');
  resolveRequest({ output: JSON.stringify({ active: 'local', configured: ['local'], connected: [] }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(changes.some(([i, value]) => i === 1 && value === ''));
});

test('empty state has local and explains there are no other machines', () => {
  const nodes = flatten(render({ active: 'local', configured: ['local'], connected: [], previous: 'local' }));
  assert.ok(nodes.includes('Hermes Teleoperator'));
  assert.ok(nodes.some(x => typeof x === 'string' && /no other machines/i.test(x)));
  assert.ok(!nodes.includes('Back'));
  assert.ok(nodes.some(x => x?.props?.['aria-label'] === 'Refresh'));
});
test('connected machine renders disconnect and identity without legacy status rows', () => {
  const nodes = flatten(render({ active: 'work', configured: ['local', 'work'], connected: ['work'], previous: 'local', identity: { hostname: 'workstation' } }));
  assert.ok(nodes.includes('Disconnect'));
  assert.ok(nodes.includes('workstation'));
  assert.ok(!nodes.some(x => typeof x === 'string' && /^(Selected|Previous|Connected):/.test(x)));
});

test('selected disconnected machine offers explicit reconnect rather than pretending it is connected', () => {
  const nodes = flatten(render({ active: 'work', configured: ['local', 'work'], connected: [], previous: 'local' }));
  assert.ok(nodes.some(x => typeof x === 'string' && /disconnected.*reconnect/i.test(x)));
  assert.ok(!nodes.includes('Disconnect'));
});

test('disconnect button targets the connected machine in the focused conversation', async () => {
  const calls = [];
  const status = { active: 'work', configured: ['local', 'work'], connected: ['work'], previous: 'local' };
  const nodes = flatten(render(status, async (...args) => { calls.push(args); return { output: JSON.stringify(status) }; }));
  const control = nodes.find(x => x?.props?.children === 'Disconnect');
  control.props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[0][1].session_id, 's');
  assert.equal(calls[0][1].command, "/teleport disconnect 'work'");
});

test('refresh starts a full visible spin even when the request is fast', async () => {
  const status = { active: 'local', configured: ['local'], connected: [] };
  const changes = [], timers = [];
  const nodes = flatten(render(status, async () => ({ output: JSON.stringify(status) }), {
    onState: (i, value) => changes.push([i, value]),
    setTimeout: (callback, ms) => { timers.push({ callback, ms }); return 1; }, clearTimeout: () => {}
  }));
  nodes.find(x => x?.props?.['aria-label'] === 'Refresh').props.onClick();
  await Promise.resolve(); await Promise.resolve();
  assert.ok(changes.some(([i, value]) => i === 5 && value === true));
  assert.ok(timers.some(t => t.ms >= 650));
  assert.ok(!changes.some(([i, value]) => i === 5 && value === false));
  timers[0].callback();
  assert.ok(changes.some(([i, value]) => i === 5 && value === false));
  const spinningNodes = flatten(render(status, async () => {}, { spinning: true }));
  const icon = spinningNodes.find(x => x?.type === 'Codicon' && x.props.name === 'refresh');
  assert.match(icon.props.style.animation, /codicon-spin 0\.65s linear 1 forwards/);
});

test('SSH and port share a row and port defaults to 22', () => {
  const box = load(async () => {});
  box.useState = value => [value, () => {}];
  box.useRef = value => ({ current: value });
  box.jsx = box.jsxs = (type, props) => ({ type, props });
  box.Input = 'Input'; box.Button = 'Button';
  let saved;
  const nodes = flatten(box.AddMachineForm({ busy: false, onSave: value => { saved = value; }, onCancel: () => {} }));
  const row = nodes.find(node => node?.props?.['data-fields'] === 'ssh');
  assert.ok(row, 'SSH inputs must share one row');
  const rowNodes = flatten(row);
  assert.ok(rowNodes.includes('SSH'));
  assert.ok(rowNodes.includes('Port'));
  assert.ok(rowNodes.some(node => node?.props?.name === 'host' && node.props.required));
  assert.equal(rowNodes.find(node => node?.props?.name === 'host').props.placeholder, 'user@host or alias');
  assert.equal(rowNodes.find(node => node?.props?.name === 'port').props.value, '22');
  nodes.find(node => node?.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.equal(saved.port, undefined, 'untouched default must not override SSH alias configuration');
  assert.ok(!nodes.some(node => typeof node === 'string' && node.includes('SSH config determines the port')));
});

test('editing port explicitly saves the chosen value', () => {
  const box = load(async () => {});
  let state, saved;
  const ref = { current: false };
  box.useState = initial => [state ??= initial, update => { state = typeof update === 'function' ? update(state) : update; }];
  box.useRef = () => ref;
  box.jsx = box.jsxs = (type, props) => ({ type, props });
  box.Input = 'Input'; box.Button = 'Button';
  const draw = () => flatten(box.AddMachineForm({ onSave: value => { saved = value; }, onCancel: () => {} }));
  draw().find(node => node?.props?.name === 'port').props.onChange({ target: { value: '2222' } });
  draw().find(node => node?.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.equal(saved.port, '2222');
});

test('optional connection settings are grouped under collapsed Advanced', () => {
  const box = load(async () => {});
  box.useState = value => [value, () => {}];
  box.useRef = value => ({ current: value });
  box.jsx = box.jsxs = (type, props) => ({ type, props });
  box.Input = 'Input'; box.Button = 'Button';
  const nodes = flatten(box.AddMachineForm({ busy: false, onSave: () => {}, onCancel: () => {} }));
  const advanced = nodes.find(node => node?.type === 'details');
  assert.ok(advanced);
  assert.notEqual(advanced.props.open, true);
  const advancedNodes = flatten(advanced);
  assert.ok(advancedNodes.includes('Advanced'));
  for (const name of ['executable', 'identity_file', 'known_hosts', 'teleoperator_home']) {
    assert.ok(advancedNodes.some(node => node?.props?.name === name), name);
  }
});

test('a busy conversation disables saving without claiming a save is in progress', () => {
  const nodes = flatten(render(null, async () => {}, { adding: true, busy: true }));
  const form = nodes.find(node => typeof node?.type === 'function' && node.type.name === 'AddMachineForm');
  assert.ok(form);
  assert.equal(form.props.disabled, true);
  assert.equal(form.props.saving, false);
  assert.ok(nodes.some(node => typeof node === 'string' && /wait.*conversation.*saving/i.test(node)));
  assert.ok(!nodes.some(node => typeof node === 'string' && /switching is unavailable/i.test(node)), 'do not show unrelated switching warning in setup form');
});

test('add button opens manual form without sending a prompt', async () => {
  const changes = [];
  const nodes = flatten(render({ active: 'local', configured: ['local'], connected: [] }, async () => {}, { onState: (i, v) => changes.push([i, v]) }));
  await nodes.find(x => x?.props?.['aria-label'] === 'Add machine').props.onClick();
  assert.ok(changes.some(([i, value]) => i === 6 && value === true));
});

test('save uses dedicated session-bound command with explicit fields', async () => {
  let called;
  const { saveMachine } = load(async (...args) => { called = args; return { output: '{"active":"local","configured":["local","work"],"connected":[]}' }; });
  await saveMachine('s', { name: 'work', host: 'me@host', port: '22', identity_file: '/home/me/.ssh/key' });
  assert.equal(called[0], 'slash.exec');
  assert.equal(called[1].session_id, 's');
  assert.match(called[1].command, /^\/teleoperator-add /);
  assert.deepEqual(JSON.parse(called[1].command.slice('/teleoperator-add '.length)), { name: 'work', host: 'me@host', port: 22, identity_file: '/home/me/.ssh/key' });
});

test('manual setup frontend saves through the real Python command', async () => {
  const os = require('node:os'), path = require('node:path'), cp = require('node:child_process');
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'teleoperator-ui-test-'));
  try {
    const python = `import json,sys\nfrom hermes_teleoperator.plugin import register\nfrom test_plugin import Context\nc=Context();register(c)\nprint(c.commands['teleoperator-add'](sys.stdin.read(),session_id='s'))\nc.cleanup()`;
    const { saveMachine } = load(async (_method, params) => {
      const command = params.command.slice('/teleoperator-add '.length);
      const result = cp.spawnSync('python3', ['-c', python], { cwd: path.join(__dirname, '..'), input: command, encoding: 'utf8', env: { ...process.env, HERMES_HOME: dir, PYTHONPATH: path.join(__dirname) } });
      assert.equal(result.status, 0, result.stderr);
      return { output: result.stdout.trim() };
    });
    const result = await saveMachine('s', { name: 'work', host: 'me@host', port: '2222' });
    assert.ok(result.configured.includes('work'));
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(dir, 'teleoperator.json'), 'utf8')).machines.work, { host: 'me@host', port: 2222 });
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('desktop companion registers in the bottom status bar rather than a pane', () => {
  const { plugin } = load(async () => {});
  const contributions = [];
  plugin.register({ register: item => contributions.push(item) });
  assert.equal(contributions.length, 1);
  assert.equal(contributions[0].area, 'statusBar.right');
});

test('bot status targets the focused owner, not the ambient gateway', async () => {
  const box = load(async () => { throw new Error('wrong gateway'); });
  const route = { connectionId: 'source-b', profile: 'worker', targetProfile: 'worker' };
  box.host.state.focusedSessionOwner = { get: () => ({ connectionId: 'source-b', profile: 'worker' }) };
  box.host.profileRoutes = async () => [route];
  let routed;
  box.host.requestProfile = async (...args) => { routed = args; return { output: '{"active":"local","configured":["local"],"connected":[]}' }; };
  await box.runCommand('bot-session', 'status');
  assert.equal(routed[0], route);
  assert.equal(routed[1], 'slash.exec');
  assert.equal(routed[2].session_id, 'bot-session');
});

test('ambiguous bot ownership fails closed without making a request', async () => {
  let calls = 0;
  const box = load(async () => { calls++; });
  box.host.state.focusedSessionOwner = { get: () => null };
  box.host.requestProfile = async () => { calls++; };
  await assert.rejects(box.runCommand('s', 'status'), /owning gateway/);
  assert.equal(calls, 0);
});

test('session cleanup ignores a late status response from the previous conversation', async () => {
  let resolveRequest, cleanup;
  const box = load(() => new Promise(resolve => { resolveRequest = resolve; }));
  box.host.state.focusedSessionId = { get: () => 'old' };
  box.host.state.busy = { get: () => false };
  box.useRef = value => ({ current: value });
  const changes = [];
  let index = 0;
  box.useState = () => { const i = index++; return [i === 3, value => changes.push([i, value])]; };
  box.useEffect = effect => { cleanup = effect(); };
  box.jsx = box.jsxs = (type, props) => ({ type, props });
  box.clearTimeout = () => {};
  for (const name of ['Button', 'Popover', 'PopoverContent', 'PopoverTrigger', 'Codicon']) box[name] = name;
  box.Companion({ sessionId: 'old', busy: false });
  await new Promise(resolve => setImmediate(resolve));
  cleanup();
  changes.length = 0;
  resolveRequest({ output: JSON.stringify({ active: 'stale', configured: ['local', 'stale'], connected: [] }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(changes, [], 'late response must not update status, error, or pending');
});

test('focus change during route lookup prevents a command to the old session', async () => {
  let releaseRoutes, requests = 0;
  const box = load(async () => { requests++; });
  let focused = 'old';
  box.host.state.focusedSessionId = { get: () => focused };
  box.host.profileRoutes = () => new Promise(resolve => { releaseRoutes = resolve; });
  box.host.requestProfile = async () => { requests++; };
  const pending = box.runCommand('old', 'switch', 'work');
  focused = 'new';
  releaseRoutes([{ connectionId: 'local', profile: 'default' }]);
  await assert.rejects(pending, /focus changed/i);
  assert.equal(requests, 0);
});

test('owner change during route lookup prevents a command to the old gateway', async () => {
  let releaseRoutes, requests = 0;
  const box = load(async () => { requests++; });
  let owner = { connectionId: 'local', profile: 'default' };
  box.host.state.focusedSessionId = { get: () => 's' };
  box.host.state.focusedSessionOwner = { get: () => owner };
  box.host.profileRoutes = () => new Promise(resolve => { releaseRoutes = resolve; });
  box.host.requestProfile = async () => { requests++; };
  const pending = box.runCommand('s', 'switch', 'work');
  owner = { connectionId: 'other', profile: 'default' };
  releaseRoutes([{ connectionId: 'local', profile: 'default' }]);
  await assert.rejects(pending, /focus changed/i);
  assert.equal(requests, 0);
});

test('error with retained status does not present the old machine as current in the chip', () => {
  const status = { active: 'work', configured: ['local', 'work'], connected: ['work'] };
  const nodes = flatten(render(status, async () => {}, { error: 'connection lost' }));
  const chip = nodes.find(node => node?.type === 'button' && node.props.title === 'Teleoperator: conversation machine');
  assert.equal(chip.props.children, 'Teleoperator');
  assert.equal(chip.props.style.color, 'var(--ui-red)');
  assert.match(chip.props['aria-label'], /connection lost/);
  assert.ok(nodes.some(node => node?.props?.role === 'alert' && node.props.children === 'connection lost'));
});

test('unsupported slash command provides actionable diagnostic', async () => {
  const box = load(async () => { throw new Error('unsupported_command'); });
  await assert.rejects(box.runCommand('s', 'status'), /agent plugin.*enabled/);
});

test('status reads the exact focused session and parses its actual response', async () => {
  let called;
  const { runCommand } = load(async (...args) => { called = args; return { output: '{"active":"remote","configured":["local","remote"],"connected":["remote"]}' }; });
  const result = await runCommand('session-1', 'status');
  assert.deepEqual(JSON.parse(JSON.stringify(called)), ['slash.exec', { session_id: 'session-1', command: '/teleport status' }]);
  assert.equal(result.active, 'remote');
});

test('switch safely quotes a machine name without changing session', async () => {
  let called;
  const { runCommand } = load(async (...args) => { called = args; return { output: '{"active":"host","configured":["local"],"connected":[]}' }; });
  await runCommand('session-2', 'switch', "desk's pc");
  assert.equal(called[1].session_id, 'session-2');
  assert.equal(called[1].command, "/teleport 'desk'\\''s pc'");
});

test('disconnect and reconnect address the chosen machine safely', async () => {
  const calls = [];
  const { runCommand } = load(async (...args) => { calls.push(args); return { output: '{"active":"desk","configured":["local","desk"],"connected":[]}' }; });
  await runCommand('session-1', 'disconnect', "desk's pc");
  await runCommand('session-1', 'reconnect', 'desk');
  assert.equal(calls[0][1].command, "/teleport disconnect 'desk'\\''s pc'");
  assert.equal(calls[1][1].command, "/teleport reconnect 'desk'");
});

test('missing session and invalid names never issue a command', async () => {
  let calls = 0;
  const { runCommand } = load(async () => { calls++; });
  await assert.rejects(runCommand(null, 'status'), /conversation/);
  await assert.rejects(runCommand('s', 'switch', 'host\nlocal'), /Invalid machine/);
  await assert.rejects(runCommand('s', 'switch', 'back'), /reserved/i);
  assert.equal(calls, 0);
});

test('errors and malformed replies do not masquerade as local status', async () => {
  for (const output of ['unknown command', '{"error":"disconnected"}', '{"active":"local"}']) {
    const { runCommand } = load(async () => ({ output }));
    await assert.rejects(runCommand('s', 'status'));
  }
});
