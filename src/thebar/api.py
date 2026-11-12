"""HTTP API and web UI. Start with `make dev`, then open http://localhost:8000."""

import asyncio
import difflib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from thebar import timeline, tools
from thebar.db import now
from thebar.society import UPDATES_THREAD, Society

ROOT = Path(os.environ.get('THEBAR_ROOT', Path(__file__).resolve().parents[2]))
society: Society = None  # set in lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    global society
    society = Society(ROOT)
    await society.start()
    yield
    await society.shutdown()


app = FastAPI(lifespan=lifespan)


def run_or_404(run_id: int) -> dict:
    try:
        return society.get_run(run_id)
    except KeyError:
        raise HTTPException(404, f'No run {run_id}')


# ---- Runs and controls ----

class NewRun(BaseModel):
    question: str | None = None


@app.post('/api/runs')
def create_run(body: NewRun):
    try:
        return {'id': society.create_run(body.question)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get('/api/runs')
def list_runs():
    runs = society.db.all('SELECT id, question, status, created_at, started_at, ended_at FROM runs ORDER BY id DESC')
    for r in runs:
        r['agents'] = society.run_agents(r['id'])
        r['last_activity'] = society.db.one(
            'SELECT agent_id, status, started_at FROM activities WHERE run_id = ? ORDER BY id DESC LIMIT 1', (r['id'],))
        r['newest_artifact'] = society.db.one(
            'SELECT kind, id, title FROM artifacts WHERE run_id = ? ORDER BY id DESC LIMIT 1', (r['id'],))
    return runs


@app.get('/api/runs/{run_id}')
def get_run(run_id: int):
    run = run_or_404(run_id)
    run['config'] = json.loads(run['config'])
    run['agents'] = [agent_status(run_id, a) for a in society.run_agents(run_id)]
    return run


def agent_status(run_id: int, agent_id: str) -> dict:
    db = society.db
    row = db.one('SELECT paused FROM run_agents WHERE run_id = ? AND agent_id = ?', (run_id, agent_id))
    starts = [r['started_at'] for r in db.all(
        'SELECT started_at FROM activities WHERE run_id = ? AND agent_id = ? ORDER BY id DESC LIMIT 6', (run_id, agent_id))]
    gaps = [(_t(a) - _t(b)) / 60 for a, b in zip(starts, starts[1:])]
    return {
        'id': agent_id,
        'paused': bool(row['paused']),
        'busy': society._busy(run_id, agent_id),
        'next_activity': society.next_wake(run_id, agent_id),
        'actual_interval_minutes': round(sum(gaps) / len(gaps), 1) if gaps else None,
        'activities': db.all(
            'SELECT id, trigger, status, started_at, ended_at, error, prompt_hash FROM activities '
            'WHERE run_id = ? AND agent_id = ? ORDER BY id DESC LIMIT 10', (run_id, agent_id)),
    }


def _t(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


@app.post('/api/runs/{run_id}/start')
async def start_run(run_id: int):
    run_or_404(run_id)
    try:
        await society.start_run(run_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return society.get_run(run_id)


@app.post('/api/runs/{run_id}/pause')
def pause_run(run_id: int):
    run_or_404(run_id)
    society.pause_run(run_id)
    return society.get_run(run_id)


@app.post('/api/runs/{run_id}/resume')
def resume_run(run_id: int):
    run_or_404(run_id)
    society.resume_run(run_id)
    return society.get_run(run_id)


@app.post('/api/runs/{run_id}/stop')
async def stop_run(run_id: int):
    run_or_404(run_id)
    await society.stop_run(run_id)
    return society.get_run(run_id)


@app.post('/api/runs/{run_id}/agents/{agent_id}/add')
def add_agent(run_id: int, agent_id: str):
    run_or_404(run_id)
    try:
        society.add_agent(run_id, agent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return agent_status(run_id, agent_id)


@app.post('/api/runs/{run_id}/agents/{agent_id}/pause')
def pause_agent(run_id: int, agent_id: str):
    run_or_404(run_id)
    society.pause_agent(run_id, agent_id)
    return agent_status(run_id, agent_id)


@app.post('/api/runs/{run_id}/agents/{agent_id}/resume')
def resume_agent(run_id: int, agent_id: str):
    run_or_404(run_id)
    society.resume_agent(run_id, agent_id)
    return agent_status(run_id, agent_id)


@app.post('/api/runs/{run_id}/agents/{agent_id}/wake')
def wake_agent(run_id: int, agent_id: str):
    """Human request: start the agent's next activity now."""
    run_or_404(run_id)
    society.wake(run_id, agent_id, immediate=True)
    return agent_status(run_id, agent_id)


@app.get('/api/activities/{activity_id}')
def activity(activity_id: int):
    """One activity with its tool calls, for the detail view."""
    act = society.db.one('SELECT * FROM activities WHERE id = ?', (activity_id,))
    if not act:
        raise HTTPException(404)
    act['tool_calls'] = society.db.all('SELECT * FROM tool_calls WHERE activity_id = ? ORDER BY id', (activity_id,))
    act['model_calls'] = society.db.one(
        'SELECT COUNT(*) AS n, SUM(seconds) AS seconds, SUM(output_tokens) AS output_tokens FROM model_calls WHERE activity_id = ?', (activity_id,))
    return act


@app.post('/api/activities/{activity_id}/cancel')
def cancel_activity(activity_id: int):
    society.cancel_activity(activity_id)
    return {'ok': True}


# ---- Events ----

def _events(after: int, run_id: int | None, limit: int = 500) -> list[dict]:
    sql = 'SELECT * FROM events WHERE id > ?'
    params: tuple = (after,)
    if run_id:
        sql += ' AND run_id = ?'
        params += (run_id,)
    rows = society.db.all(sql + ' ORDER BY id LIMIT ?', params + (limit,))
    for r in rows:
        r['payload'] = json.loads(r['payload'])
    return rows


@app.get('/api/events')
def list_events(after: int = 0, run_id: int | None = None, limit: int = 500, newest: int | None = None):
    """Events after an id, oldest first; or with `newest`, the latest N events."""
    if newest:
        last = society.db.one('SELECT MAX(id) AS m FROM events')['m'] or 0
        return _events(max(0, last - newest * 3), run_id, newest * 3)[-newest:]
    return _events(after, run_id, limit)


@app.get('/api/events/stream')
async def stream_events(request: Request, after: int = 0, run_id: int | None = None):
    """Server-sent events, read from the events table. Reconnecting clients resume from Last-Event-ID."""
    last = int(request.headers.get('last-event-id', after))

    async def gen():
        nonlocal last
        while not await request.is_disconnected():
            for e in _events(last, run_id):
                last = e['id']
                yield f'id: {e["id"]}\ndata: {json.dumps(e)}\n\n'
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type='text/event-stream')


# ---- Discussions ----

@app.get('/api/runs/{run_id}/threads')
def threads(run_id: int):
    return society.db.all(
        'SELECT t.*, COUNT(m.id) AS messages, MAX(m.created_at) AS last_post FROM threads t LEFT JOIN messages m '
        'ON m.thread_id = t.id WHERE t.run_id = ? GROUP BY t.id ORDER BY last_post DESC', (run_id,))


@app.get('/api/threads/{thread_id}')
def thread(thread_id: int):
    return {
        'thread': society.db.one('SELECT * FROM threads WHERE id = ?', (thread_id,)),
        'messages': society.db.all('SELECT * FROM messages WHERE thread_id = ? ORDER BY id', (thread_id,)),
    }


@app.get('/api/runs/{run_id}/timeline')
def run_timeline(run_id: int):
    run_or_404(run_id)
    return {'agents': society.run_agents(run_id), **timeline.build(society.db, run_id, UPDATES_THREAD, ROOT)}


@app.get('/api/runs/{run_id}/dms')
def dms(run_id: int):
    return society.db.all('SELECT * FROM messages WHERE run_id = ? AND recipient IS NOT NULL ORDER BY id', (run_id,))


@app.get('/api/search')
def search(q: str):
    return society.db.search(q, limit=30)


@app.get('/api/ref/{ref}')
def resolve_ref(ref: str):
    try:
        text = tools.resolve(society, ref)
    except (KeyError, ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    images = []
    if ref.startswith('experiment:'):
        row = society.db.one('SELECT path FROM experiments WHERE id = ?', (ref.split(':')[1],))
        images = timeline.experiment_images(ROOT, row['path'])
    pdf = None
    if ref.startswith('manuscript:'):
        row = society.db.one('SELECT path FROM artifacts WHERE id = ?', (ref.split(':')[1].split('@')[0],))
        pdf = f'{row["path"]}/paper.pdf' if (ROOT / row['path'] / 'paper.pdf').exists() else None
    return {'ref': ref, 'text': text, 'images': images, 'pdf': pdf}


# ---- Agents, papers, artifacts, experiments, tools ----

@app.get('/api/agents')
def agents():
    society.config.load()
    out = []
    for a in society.config.agents.values():
        memory = ROOT / a.workspace / 'memory' / 'memory.md'
        out.append({
            **a.__dict__,
            'model_config': society.config.models.get(a.model),
            'memory': memory.read_text() if memory.exists() else '',
            'tools': [str(p.relative_to(ROOT)) for p in sorted((ROOT / a.workspace / 'tools').rglob('*')) if p.is_file()],
        })
    return out


@app.get('/api/papers')
def list_papers():
    papers = society.db.all('SELECT * FROM papers ORDER BY collection, id')
    for p in papers:
        p['reads'] = society.db.all('SELECT agent_id, status FROM paper_reads WHERE paper_id = ?', (p['id'],))
    return papers


@app.get('/api/papers/{paper_id}/pdf')
def paper_pdf(paper_id: int):
    p = society.db.one('SELECT path FROM papers WHERE id = ?', (paper_id,))
    if not p:
        raise HTTPException(404)
    return FileResponse(ROOT / p['path'], media_type='application/pdf')


@app.get('/api/artifacts')
def list_artifacts():
    return society.db.all('SELECT * FROM artifacts ORDER BY id DESC')


@app.get('/api/artifacts/{artifact_id}')
def artifact(artifact_id: int):
    a = society.db.one('SELECT * FROM artifacts WHERE id = ?', (artifact_id,))
    if not a:
        raise HTTPException(404)
    a['versions'] = society.db.all('SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY revision', (artifact_id,))
    return a


@app.get('/api/artifacts/{artifact_id}/diff')
def artifact_diff(artifact_id: int, a: int, b: int):
    rows = {r['revision']: r['body'] for r in society.db.all(
        'SELECT revision, body FROM artifact_versions WHERE artifact_id = ? AND revision IN (?, ?)', (artifact_id, a, b))}
    if a not in rows or b not in rows:
        raise HTTPException(404)
    return {'diff': '\n'.join(difflib.unified_diff(rows[a].splitlines(), rows[b].splitlines(), f'revision {a}', f'revision {b}', lineterm=''))}


@app.get('/api/experiments')
def experiments():
    rows = society.db.all('SELECT * FROM experiments ORDER BY id DESC')
    for r in rows:
        folder = ROOT / (r['path'] or '')
        r['files'] = sorted(p.name for p in folder.iterdir()) if r['path'] and folder.exists() else []
        r['results'] = (folder / 'results.json').read_text()[:5000] if (folder / 'results.json').exists() else None
        r['images'] = timeline.experiment_images(ROOT, r['path'])
    return rows


@app.get('/api/tool_calls')
def tool_calls(agent_id: str | None = None, limit: int = 100):
    if agent_id:
        return society.db.all('SELECT * FROM tool_calls WHERE agent_id = ? ORDER BY id DESC LIMIT ?', (agent_id, limit))
    return society.db.all('SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?', (limit,))


@app.get('/api/file')
def get_file(path: str):
    """Read a file from artifacts/, proposals/, agents/, data/papers/ (notes), or config/."""
    target = (ROOT / path).resolve()
    allowed = [ROOT / d for d in ('artifacts', 'proposals', 'agents', 'data/papers', 'config')]
    if not any(target.is_relative_to(d.resolve()) for d in allowed) or not target.is_file():
        raise HTTPException(404)
    if target.suffix in ('.png', '.jpg', '.svg', '.pdf'):
        return FileResponse(target)
    return {'path': path, 'text': target.read_text(errors='replace')}


# ---- Configuration ----

@app.get('/api/config')
def config_files():
    return {'files': sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'config').rglob('*') if p.is_file()),
            'history': society.db.all('SELECT id, path, created_at FROM config_versions ORDER BY id DESC LIMIT 50')}


class FileEdit(BaseModel):
    path: str
    text: str


@app.put('/api/config')
def save_config(edit: FileEdit):
    """Edit a config or prompt file. The previous text is kept in config_versions. Applies to future activities."""
    target = (ROOT / edit.path).resolve()
    if not target.is_relative_to((ROOT / 'config').resolve()):
        raise HTTPException(400, 'Only files under config/ can be edited.')
    if target.exists():
        society.db.insert('INSERT INTO config_versions (path, content, created_at) VALUES (?,?,?)', (edit.path, target.read_text(), now()))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(edit.text)
    try:
        society.config.load()
    except Exception as e:
        target.write_text(society.db.one('SELECT content FROM config_versions WHERE path = ? ORDER BY id DESC LIMIT 1', (edit.path,))['content'])
        raise HTTPException(400, f'Invalid config, change reverted: {e}')
    society.db.emit('config_changed', {'path': edit.path})
    return {'ok': True}


# ---- Resource status ----

@app.get('/api/status')
async def status():
    db = society.db
    calls = db.one('SELECT COUNT(*) AS n, AVG(seconds) AS avg_seconds, SUM(output_tokens) AS output_tokens, '
                   'SUM(1 - ok) AS errors FROM (SELECT * FROM model_calls ORDER BY id DESC LIMIT 50)')
    ollama = None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            ollama = (await client.get('http://localhost:11434/api/ps')).json()
    except Exception as e:
        ollama = {'error': str(e)}
    containers = ''
    try:
        proc = await asyncio.create_subprocess_exec(
            'docker', 'stats', '--no-stream', '--format', '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        containers = '\n'.join(l for l in out.decode().splitlines() if l.startswith('thebar-'))
    except Exception:
        pass
    return {
        'active_activities': [{'activity_id': k, 'run_id': r, 'agent_id': a} for k, (r, a, _) in society.active.items()],
        'model_queue_waiting': len(getattr(society.inference_lock, '_waiters', None) or []),
        'recent_model_calls': calls,
        'ollama': ollama,
        'containers': containers,
    }


@app.get('/')
def index():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')
