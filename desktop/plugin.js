import { host, useValue, Button, Input, Codicon, Popover, PopoverContent, PopoverTrigger } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

async function requestForFocusedSession(sessionId, command) {
  const owner = host.state.focusedSessionOwner?.get()
  if (!owner?.connectionId || !owner?.profile) throw new Error('Cannot identify this conversation’s gateway.')
  const routes = await host.profileRoutes()
  const matches = routes.filter(route => route.connectionId === owner.connectionId && route.profile === owner.profile)
  if (matches.length !== 1) throw new Error('This conversation’s gateway is ambiguous.')
  const current = host.state.focusedSessionOwner?.get()
  if (!current || current.connectionId !== owner.connectionId || current.profile !== owner.profile || (host.state.focusedSessionId && host.state.focusedSessionId.get() !== sessionId)) throw new Error('Conversation changed. Retry.')
  try {
    return await host.requestProfile(matches[0], 'slash.exec', { session_id: sessionId, command }, undefined, { spawnPriority: 'foreground' })
  } catch (err) {
    if (String(err?.message || err).includes('unsupported_command')) throw new Error('The agent plugin is not available for this profile. Enable it and restart the gateway.')
    throw err
  }
}

async function runCommand(sessionId, action, machine) {
  if (!sessionId) throw new Error('Open a conversation first.')
  if (!['status', 'switch', 'back', 'disconnect', 'reconnect'].includes(action)) throw new Error('Unsupported action.')
  let command = '/teleport ' + (action === 'switch' ? '' : action)
  if (['switch', 'disconnect', 'reconnect'].includes(action)) {
    if (typeof machine !== 'string' || !machine || /[\x00-\x1f\x7f]/.test(machine)) throw new Error('Invalid machine name.')
    if (action === 'switch' && ['status', 'back', 'disconnect', 'reconnect'].includes(machine)) throw new Error('Machine name is reserved by /teleport.')
    command += (action === 'switch' ? '' : ' ') + "'" + machine.replace(/'/g, "'\\''") + "'"
  }
  const response = await requestForFocusedSession(sessionId, command)
  let status
  try { status = JSON.parse(response.output) } catch { throw new Error('Teleoperator did not answer: ' + String(response?.output || 'empty response')) }
  if (status?.error) throw new Error(status.error)
  if (!status || typeof status.active !== 'string' || !Array.isArray(status.configured) || !Array.isArray(status.connected)) throw new Error('Unexpected status from the gateway.')
  return status
}

async function saveMachine(sessionId, fields) {
  if (!sessionId) throw new Error('Open a conversation first.')
  const data = { ...fields }
  if (data.port !== undefined) {
    if (!/^\d+$/.test(String(data.port)) || +data.port < 1 || +data.port > 65535) throw new Error('SSH port must be between 1 and 65535.')
    data.port = Number(data.port)
  }
  const response = await requestForFocusedSession(sessionId, '/teleoperator-add ' + JSON.stringify(data))
  let result
  try { result = JSON.parse(response.output) } catch { throw new Error('This gateway does not support manual setup. Update and restart the agent plugin.') }
  if (result?.error) throw new Error(result.error)
  if (!result || !Array.isArray(result.configured) || typeof result.active !== 'string' || !Array.isArray(result.connected) || !result.configured.includes(fields.name)) throw new Error('Unexpected reply from the gateway. Verify the machine list.')
  return result
}

function AddMachineForm({ disabled, saving, onSave, onCancel }) {
  const [values, setValues] = useState({ name: '', host: '', port: '22', executable: '', identity_file: '', known_hosts: '', teleoperator_home: '' })
  const portEdited = useRef(false)
  const fields = [
    ['executable', 'Receiver path (optional)', '/home/user/.local/bin/hermes-teleoperator-receiver'],
    ['identity_file', 'Private key path on gateway (optional)', '/home/user/.ssh/teleoperator'],
    ['known_hosts', 'Known hosts file on gateway (optional)', '/home/user/.ssh/known_hosts'],
    ['teleoperator_home', 'Receiver config directory (optional)', '/home/user/.config/hermes-teleoperator']
  ]
  return jsxs('form', { onSubmit: event => { event.preventDefault(); onSave(Object.fromEntries(Object.entries(values).filter(([key, value]) => value.trim() && (key !== 'port' || portEdited.current)).map(([key, value]) => [key, value.trim()]))) },
    style: { display: 'grid', gap: '8px', maxHeight: 'min(60vh, 500px)', overflowY: 'auto' }, children: [
      jsx('strong', { children: 'Add a machine' }),
      jsx('small', { children: 'Saves connection settings only. First install the receiver, authorize its SSH key and verify the host key. Never paste private key contents.' }),
      jsxs('label', { style: { display: 'grid', gap: '3px' }, children: [
        jsx('span', { children: 'Machine name' }), jsx(Input, { name: 'name', value: values.name, placeholder: 'workstation', required: true, autoComplete: 'off', onChange: event => setValues(old => ({ ...old, name: event.target.value })) })
      ] }),
      jsxs('div', { 'data-fields': 'ssh', style: { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 80px', gap: '8px' }, children: [
        jsxs('label', { style: { display: 'grid', gap: '3px', minWidth: 0 }, children: [
          jsx('span', { children: 'SSH' }), jsx(Input, { name: 'host', value: values.host, placeholder: 'user@host or alias', required: true, autoComplete: 'off', onChange: event => setValues(old => ({ ...old, host: event.target.value })) })
        ] }),
        jsxs('label', { style: { display: 'grid', gap: '3px', minWidth: 0 }, children: [
          jsx('span', { children: 'Port' }), jsx(Input, { name: 'port', value: values.port, inputMode: 'numeric', autoComplete: 'off', onChange: event => { portEdited.current = true; setValues(old => ({ ...old, port: event.target.value })) } })
        ] })
      ] }),
      jsxs('details', { children: [
        jsx('summary', { style: { cursor: 'pointer' }, children: 'Advanced' }),
        jsx('div', { style: { display: 'grid', gap: '8px', marginTop: '8px' }, children: fields.map(([key, label, placeholder]) => jsxs('label', { style: { display: 'grid', gap: '3px' }, children: [
          jsx('span', { children: label }), jsx(Input, { name: key, value: values[key], placeholder, autoComplete: 'off', onChange: event => setValues(old => ({ ...old, [key]: event.target.value })) })
        ] }, key)) })
      ] }),
      jsxs('div', { style: { display: 'flex', justifyContent: 'flex-end', gap: '6px' }, children: [
        jsx(Button, { type: 'button', variant: 'secondary', size: 'sm', onClick: onCancel, children: 'Cancel' }),
        jsx(Button, { type: 'submit', size: 'sm', disabled, children: saving ? 'Saving…' : 'Save machine' })
      ] })
    ] })
}

function Companion({ sessionId, busy }) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [open, setOpen] = useState(false)
  const [reconnectMachine, setReconnectMachine] = useState(null)
  const [adding, setAdding] = useState(false)
  const [saving, setSaving] = useState(false)
  const generation = useRef(0)
  const inFlight = useRef(false)

  async function act(action, machine) {
    if (inFlight.current || !sessionId || host.state.focusedSessionId.get() !== sessionId) return
    if (action !== 'status' && (host.state.busy.get() || status?.unknown)) return
    inFlight.current = true
    const request = ++generation.current
    setPending(true)
    setReconnectMachine(null)
    try {
      const result = await runCommand(sessionId, action, machine)
      if (generation.current === request) { setStatus(result); setError('') }
    } catch (err) {
      if (generation.current === request) {
        // A missing or malformed reply cannot establish whether an action ran.
        // Keep the machine names, but never present the old location as current.
        setStatus(old => old ? { ...old, unknown: true } : null)
        setError(err.message)
        if (action !== 'status') {
          try {
            const fresh = await runCommand(sessionId, 'status')
            if (generation.current === request) {
              setStatus(fresh)
              if (action === 'switch' && /^disconnected\b/i.test(err.message)) setReconnectMachine(machine)
            }
          } catch { /* Keep names and unknown state until the next status read. */ }
        }
      }
    } finally {
      if (generation.current === request) { inFlight.current = false; setPending(false) }
    }
  }
  useEffect(() => {
    if (sessionId) act('status')
    return () => { generation.current++; inFlight.current = false }
  }, [sessionId])

  function addMachine() {
    if (sessionId && host.state.focusedSessionId.get() === sessionId) { setError(''); setAdding(true) }
  }

  async function submitMachine(fields) {
    if (inFlight.current || !sessionId || host.state.focusedSessionId.get() !== sessionId || host.state.busy.get()) return
    inFlight.current = true
    const request = ++generation.current
    setPending(true)
    setSaving(true)
    try {
      const result = await saveMachine(sessionId, fields)
      if (generation.current === request) { setStatus(result); setError(''); setAdding(false) }
    } catch (err) {
      if (generation.current === request) setError(err.message)
    } finally {
      if (generation.current === request) { inFlight.current = false; setPending(false); setSaving(false) }
    }
  }

  const label = !sessionId || error || !status || status.unknown ? 'Teleoperator' : 'Teleoperator · ' + status.active
  const machines = status ? status.configured.filter(name => typeof name === 'string') : []
  const machineRow = name => {
    const selected = !status.unknown && status.active === name
    const connected = !status.unknown && name !== 'local' && status.connected.includes(name)
    const offlineSelected = name !== 'local' && selected && !connected
    const hostname = selected && typeof status.identity?.hostname === 'string' ? status.identity.hostname : null
    const unavailable = pending || busy || status.unknown
    return jsxs('div', {
      role: 'listitem',
      style: { border: connected ? '1px solid var(--ui-stroke-secondary)' : '1px solid transparent', borderRadius: '6px', padding: connected || offlineSelected ? '6px 8px' : '0 8px', display: 'grid', gap: '4px' },
      children: [
        jsxs('button', { type: 'button', disabled: unavailable || (selected && !offlineSelected),
          onClick: () => act(offlineSelected ? 'reconnect' : 'switch', name),
          style: { display: 'flex', justifyContent: 'space-between', width: '100%', textAlign: 'left', padding: '5px 0', cursor: unavailable ? 'default' : 'pointer' },
          children: [jsx('span', { children: name === 'local' ? 'Local' : name }), selected ? jsx('span', { style: { color: 'var(--ui-accent)' }, children: 'Current' }) : null] }),
        connected ? jsxs('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }, children: [
          jsx('span', { style: { color: 'var(--ui-text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis' }, children: hostname || 'SSH connected' }),
          jsx(Button, { size: 'sm', variant: 'secondary', disabled: unavailable, onClick: () => act('disconnect', name), children: 'Disconnect' })
        ] }) : null,
        offlineSelected ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: 'Disconnected · click to reconnect' }) : null
      ]
    }, name)
  }
  return jsxs(Popover, { open, onOpenChange: value => { setOpen(value); if (value) act('status') }, children: [
    jsx(PopoverTrigger, { asChild: true, children: jsx('button', {
      type: 'button', 'aria-label': error ? 'Teleoperator unavailable: ' + error : label, title: 'Teleoperator: conversation machine',
      className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)', style: error ? { color: 'var(--ui-red)' } : undefined, children: label
    }) }),
    jsx(PopoverContent, { side: 'top', align: 'end', children: jsxs('div', {
      style: { display: 'grid', gap: '10px', padding: '4px', color: 'var(--ui-text-secondary)', fontSize: '12px' }, children: [
        jsxs('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' }, children: [
          jsx('strong', { children: 'Hermes Teleoperator' }),
          jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: '4px' }, children: [
            jsx('button', { type: 'button', 'aria-label': adding ? 'Close add machine' : 'Add machine', title: adding ? 'Close' : 'Add machine', disabled: !sessionId,
              onClick: adding ? () => setAdding(false) : addMachine, style: { padding: '4px', display: 'inline-flex', color: 'var(--ui-text-tertiary)' },
              children: jsx(Codicon, { name: adding ? 'close' : 'add', size: '0.85rem' }) })
          ] })
        ] }),
        !sessionId ? jsx('div', { children: 'Open a conversation first.' }) : null,
        adding ? jsx(AddMachineForm, { disabled: pending || busy, saving, onSave: submitMachine, onCancel: () => setAdding(false) }) : null,
        !adding && reconnectMachine ? jsx(Button, { size: 'sm', variant: 'secondary', disabled: pending || busy, onClick: () => act('reconnect', reconnectMachine), children: 'Reconnect ' + reconnectMachine }) : null,
        !adding && status ? jsx('div', { role: 'list', style: { display: 'grid', gap: '4px' }, children: machines.map(machineRow) }) : null,
        !adding && status && !machines.some(name => name !== 'local') ? jsx('div', { style: { color: 'var(--ui-text-tertiary)', padding: '2px 8px' }, children: 'No other machines configured.' }) : null,
        status?.unknown ? jsx('div', { role: 'status', children: 'Location is unknown. Reopen this panel to check status.' }) : null,
        busy && !saving ? jsx('div', { style: { color: 'var(--ui-text-tertiary)', padding: '2px 8px' }, children: 'Agent is running.' }) : null,
        error ? jsx('div', { role: 'alert', title: error, style: { color: 'var(--ui-red)', fontSize: '11px', lineHeight: 1.35, whiteSpace: 'normal', overflowWrap: 'anywhere', minWidth: 0, padding: '2px 8px' }, children: error }) : null
      ]
    }) })
  ] })
}

function StatusBar() {
  const sessionId = useValue(host.state.focusedSessionId)
  const owner = useValue(host.state.focusedSessionOwner)
  const busy = useValue(host.state.busy)
  return jsx(Companion, { sessionId, busy, key: JSON.stringify([sessionId, owner?.connectionId, owner?.profile]) })
}

export default {
  id: 'hermes-teleoperator',
  name: 'Teleoperator',
  register(ctx) {
    ctx.register({ id: 'teleoperator-status', area: 'statusBar.right', order: 130, render: () => jsx(StatusBar, {}) })
  }
}
