"""Data for the timeline view: everything a run produced as nodes, and the references between them as edges.

Every node made during an activity has `activity` set to that activity's id, so the UI can show
how it came about. A node is one item in one column: an activity, a message, paper notes, an insight or proposal
revision, an experiment, or (in the 'literature' column) a paper. An edge goes from a node to
an earlier node it refers to.
"""

import json
import re

from thebar.db import DB

REF_RE = re.compile(r'\b(paper|insight|proposal|message|experiment):(\d+)(?:@(\d+))?')
LITERATURE = 'literature'


def activity_of(idem: str | None) -> int | None:
    """Writes are keyed '<activity id>:<tool call id>', so the key tells which activity made them."""
    head = (idem or '').split(':')[0]
    return int(head) if head.isdigit() else None


def refs_in(*texts: str) -> list[str]:
    return [m.group(0) for t in texts if t for m in REF_RE.finditer(t)]


def build(db: DB, run_id: int, updates_thread: str) -> dict:
    nodes: list[dict] = []
    links: list[tuple[str, str, str]] = []  # (from key, ref, type); refs resolved to node keys below

    def node(key, agent, time, kind, title, text='', **extra):
        nodes.append({'key': key, 'agent': agent, 'time': time, 'kind': kind, 'title': title, 'text': text, **extra})

    for a in db.all('SELECT * FROM activities WHERE run_id = ? ORDER BY id', (run_id,)):
        calls = db.all('SELECT tool, ended_at IS NOT NULL AS done FROM tool_calls WHERE activity_id = ? ORDER BY id', (a['id'],))
        node(f'activity:{a["id"]}', a['agent_id'], a['started_at'], 'activity', f'Activity #{a["id"]}',
             a['error'] or '', status=a['status'], trigger=a['trigger'], ended_at=a['ended_at'],
             tools=[c['tool'] for c in calls], activity=a['id'])

    for m in db.all('SELECT m.*, t.title AS thread, t.author AS thread_author FROM messages m LEFT JOIN threads t ON t.id = m.thread_id '
                    'WHERE m.run_id = ? ORDER BY m.id', (run_id,)):
        if m['recipient']:
            kind, title = 'dm', f'DM to {m["recipient"]}'
        elif m['thread'] == updates_thread and m['thread_author'] == 'system':
            kind, title = 'update', 'Public update'
        else:
            kind, title = 'post', m['thread']
        key = f'message:{m["id"]}'
        node(key, m['author'], m['created_at'], kind, title, m['body'], activity=activity_of(m['idem']))
        links += [(key, r, 'ref') for r in refs_in(m['body'])]
        if kind == 'post':
            prev = db.one('SELECT MAX(id) AS id FROM messages WHERE thread_id = ? AND id < ?', (m['thread_id'], m['id']))['id']
            if prev:
                links.append((key, f'message:{prev}', 'reply'))

    for v in db.all('SELECT v.*, a.kind, a.title, a.forked_from FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id '
                    'WHERE a.run_id = ? ORDER BY v.created_at', (run_id,)):
        key = f'{v["kind"]}:{v["artifact_id"]}@{v["revision"]}'
        node(key, v['author'], v['created_at'], v['kind'], v['title'], v['body'], revision=v['revision'],
             activity=activity_of(v['idem']))
        links += [(key, r, 'ref') for r in json.loads(v['refs'] or '[]') + refs_in(v['body'])]
        if v['revision'] > 1:
            links.append((key, f'{v["kind"]}:{v["artifact_id"]}@{v["revision"] - 1}', 'revision'))
        elif v['forked_from']:
            links.append((key, f'{v["kind"]}:{v["forked_from"]}', 'revision'))

    for e in db.all('SELECT * FROM experiments WHERE run_id = ? ORDER BY id', (run_id,)):
        key = f'experiment:{e["id"]}'
        node(key, e['author'], e['created_at'], 'experiment', f'Experiment #{e["id"]}', e['hypothesis'], exit_code=e['exit_code'],
             activity=e['activity_id'])
        links += [(key, r, 'ref') for r in json.loads(e['refs'] or '[]')]

    for e in db.all("SELECT * FROM events WHERE run_id = ? AND type = 'paper_notes' ORDER BY id", (run_id,)):
        paper_id = json.loads(e['payload'])['paper_id']
        title = (db.one('SELECT title FROM papers WHERE id = ?', (paper_id,)) or {}).get('title', '')
        key = f'notes:{e["id"]}'
        node(key, e['agent_id'], e['created_at'], 'notes', f'Notes: {title}', path=json.loads(e['payload'])['path'], paper=paper_id,
             activity=e['activity_id'])
        links.append((key, f'paper:{paper_id}', 'ref'))

    nodes.sort(key=lambda n: n['time'])

    # Papers appear in the literature column just before the first item that refers to them.
    first_use: dict[str, str] = {}
    for key, ref, _ in links:
        if ref.startswith('paper:'):
            t = next(n['time'] for n in nodes if n['key'] == key)
            first_use[ref] = min(first_use.get(ref, t), t)
    for ref, t in first_use.items():
        p = db.one('SELECT title, authors, year FROM papers WHERE id = ?', (int(ref.split(':')[1]),))
        if p:
            nodes.append({'key': ref, 'agent': LITERATURE, 'time': t, 'kind': 'paper', 'title': p['title'],
                          'text': f'{p["authors"] or ""} {p["year"] or ""}'.strip()})
    nodes.sort(key=lambda n: (n['time'], n['kind'] != 'paper'))

    # Resolve each reference to a node: an exact key, or the latest revision of an insight or proposal.
    keys = {n['key'] for n in nodes}
    latest: dict[str, str] = {}
    for n in nodes:
        if n['kind'] in ('insight', 'proposal'):
            latest[n['key'].split('@')[0]] = n['key']
    edges, seen = [], set()
    for key, ref, type in links:
        target = ref if ref in keys else latest.get(ref)
        if target and target != key and (key, target) not in seen:
            seen.add((key, target))
            edges.append({'from': key, 'to': target, 'type': type})
    return {'nodes': nodes, 'edges': edges}
