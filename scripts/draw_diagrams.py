"""Render dependency-free, Hermes-coloured README diagrams."""
from pathlib import Path
from html import escape

OUT = Path(__file__).resolve().parents[1] / 'assets'
OUT.mkdir(exist_ok=True)


def text(x, y, value, size=18, color='#fafaf6', anchor='start'):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}">{escape(value)}</text>'


def box(x, y, w, h, title, lines, gold=False):
    stroke = '#ffd700' if gold else '#5f6154'
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="#1b1d18" stroke="{stroke}" stroke-width="2"/>'
    s += text(x+24, y+36, title, 21, stroke if gold else '#fafaf6')
    for i, line in enumerate(lines):
        s += text(x+24, y+72+i*28, line, 17, '#babdae')
    return s


def svg(title, desc, height, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>
<defs><pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#24271e" stroke-width="0.5"/></pattern><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10" fill="none" stroke="#ffd700" stroke-width="1.5"/></marker></defs>
<rect width="1120" height="{height}" rx="12" fill="#10110f"/><rect width="1120" height="{height}" fill="url(#grid)"/>
<g font-family="ui-monospace, SFMono-Regular, Consolas, monospace">{body}</g></svg>'''


body = text(36, 48, '01 / ONE CONVERSATION. REMOTE NATIVE TOOLS.', 22, '#ffd700')
body += '<path d="M418 221H526V176H638 M526 221V365H638" fill="none" stroke="#ffd700" stroke-width="2" marker-end="url(#arrow)"/><path d="M526 176H638" stroke="#ffd700" stroke-width="2" marker-end="url(#arrow)"/>'
body += box(36, 110, 382, 240, 'GATEWAY / HERMES', ['Model + conversation stay here', 'Teleoperator plugin', 'Select a destination', 'Receive labelled tool results'], True)
body += text(460, 130, 'SSH', 20, '#ffd700')
body += text(448, 274, 'encrypted', 16, '#babdae')
body += text(448, 298, 'stdio', 16, '#babdae')
body += box(638, 100, 446, 158, 'WORKSTATION', ['Standalone Rust receiver', 'Terminal · Files · Skills · MCP', 'Credentials stay on this host'])
body += box(638, 292, 446, 158, 'SERVER', ['Standalone Rust receiver', 'Terminal · Files · Skills · MCP', 'Credentials stay on this host'])
body += text(36, 414, 'NO EXTRA LISTENER', 18, '#ffd700')
body += text(36, 445, 'Only SSH. No second conversation agent.', 16, '#babdae')
(OUT / 'architecture.svg').write_text(svg('Teleoperator architecture', 'One gateway keeps the model and conversation. SSH channels connect it to on-demand companions on a workstation and server, each using standalone tool implementations and local credentials; no Hermes installation on the receiver.', 486, body))

body = text(36, 48, '02 / SWITCH MACHINES. KEEP YOUR BEARINGS.', 22, '#ffd700')
body += '<path d="M348 173H394 M726 173H772" fill="none" stroke="#ffd700" stroke-width="2" marker-end="url(#arrow)"/>'
body += box(36, 98, 312, 150, 'LOCAL', ['/teleport workstation', 'Connect + verify identity'])
body += box(394, 98, 332, 150, 'WORKSTATION', ['Native tools run here', '/teleport server'], True)
body += box(772, 98, 312, 150, 'SERVER', ['Native tools run here', '/teleport back'])
body += '<path d="M922 248V292H560V248" fill="none" stroke="#ffd700" stroke-width="2" stroke-dasharray="6 5" marker-end="url(#arrow)"/>'
body += text(741, 321, 'back reuses a live connection', 16, '#babdae', 'middle')
body += box(36, 356, 1048, 124, 'CONNECTION LOST? STOP HERE.', ['Remote stays selected. No silent local fallback.', 'Inspect the outcome, then reconnect explicitly. Reconnect starts a fresh shell.'], True)
body += text(36, 521, 'Selection is per conversation. Live connections keep their shell state.', 17, '#babdae')
(OUT / 'switching.svg').write_text(svg('Switching and failure behaviour', 'Switch from local to workstation to server. Back returns to the previous machine and reuses its live connection. A lost connection never silently redirects commands to local; reconnect explicitly after checking the outcome.', 552, body))
print('Rendered assets/architecture.svg and assets/switching.svg')
