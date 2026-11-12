"""Research runs, per-agent scheduling, and running one agent activity.

Terms:
- run: one session of the society investigating a question (status: created, running, paused, stopping, stopped, failed).
- activity: one scheduled period of work by one agent. Each activity is one Agent.run call,
  recorded by StepPersistence under the id 'activity-<id>'.
"""

import asyncio
import json
import os
import random
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pydantic_ai import UsageLimits
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent
from pydantic_ai_harness.step_persistence import SqliteStepStore

from thebar import papers
from thebar.agent import ActivityResult, build_agent, current_activity, ensure_workspace, remove_all_containers, remove_container
from thebar.config import Config
from thebar.db import DB, now
from thebar.tools import Deps

UPDATES_THREAD = 'Research updates'


class Society:
    def __init__(self, root: Path, db_path: str | Path | None = None, model=None):
        self.config = Config(root)
        state = Path(root) / 'state'
        self.db = DB(db_path or state / 'society.db')
        self.step_store = SqliteStepStore(database=state / 'steps.db')
        self.inference_lock = asyncio.Lock()
        self.scheduler = AsyncIOScheduler()
        self.active: dict[int, tuple[int, str, asyncio.Task]] = {}  # activity_id -> (run_id, agent_id, task)
        self.model = model  # override for tests

    # ---- Startup and shutdown ----

    async def start(self) -> None:
        """Recover from the previous process and resume runs that were running."""
        self.scheduler.start()
        await self._remove_containers()
        for act in self.db.all("SELECT id FROM activities WHERE status = 'running'"):
            unresolved = await self.step_store.list_unresolved_tool_effects(run_id=f'activity-{act["id"]}')
            note = 'interrupted by shutdown'
            if unresolved:
                note += '; tool calls with unknown outcome: ' + ', '.join(f'{e.tool_name} ({e.tool_call_id})' for e in unresolved)
            self.db.execute("UPDATE activities SET status = 'interrupted', ended_at = ?, error = ? WHERE id = ?", (now(), note, act['id']))
        self.db.execute("UPDATE runs SET status = 'stopped', ended_at = ? WHERE status = 'stopping'", (now(),))
        for run in self.db.all("SELECT id FROM runs WHERE status = 'running'"):
            self._schedule_run(run['id'])

    async def shutdown(self) -> None:
        """Cancel running activities. Runs stay 'running' and resume on next start."""
        self.scheduler.shutdown(wait=False)
        tasks = [t for _, _, t in self.active.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._remove_containers()

    async def _remove_containers(self) -> None:
        if self.config.society['sandbox']['enabled']:
            try:
                await remove_all_containers()
            except FileNotFoundError:
                pass

    # ---- Runs ----

    def run_agents(self, run_id: int) -> list[str]:
        return [r['agent_id'] for r in self.db.all('SELECT agent_id FROM run_agents WHERE run_id = ?', (run_id,))]

    def get_run(self, run_id: int) -> dict:
        run = self.db.one('SELECT * FROM runs WHERE id = ?', (run_id,))
        if not run:
            raise KeyError(f'No run {run_id}')
        return run

    def create_run(self, question: str | None = None) -> int:
        self.config.load()
        problems = self.config.validate()
        if problems:
            raise ValueError('; '.join(problems))
        question = (question or self.config.society['question']).strip()
        run_id = self.db.insert(
            "INSERT INTO runs (question, status, config, created_at) VALUES (?, 'created', ?, ?)",
            (question, json.dumps(self.config.snapshot()), now()),
        )
        for agent in self.config.enabled_agents():
            self.db.execute('INSERT INTO run_agents (run_id, agent_id) VALUES (?,?)', (run_id, agent.id))
        self.db.insert('INSERT INTO threads (run_id, title, author, created_at) VALUES (?,?,?,?)', (run_id, UPDATES_THREAD, 'system', now()))
        self.db.emit('run_created', {'question': question}, run_id)
        return run_id

    async def start_run(self, run_id: int) -> None:
        run = self.get_run(run_id)
        if run['status'] != 'created':
            return
        await self.check_models(run_id)
        for agent_id in self.run_agents(run_id):
            agent = self.config.agents[agent_id]
            ensure_workspace(self.config.root, agent)
            papers.index_collection(self.db, self.config.root, agent.papers_dir)
        self._set_status(run_id, 'running', started_at=now())
        self._schedule_run(run_id)

    def pause_run(self, run_id: int) -> None:
        """Stop starting new activities. Activities already running finish normally."""
        if self.get_run(run_id)['status'] == 'running':
            self._unschedule_run(run_id)
            self._set_status(run_id, 'paused')

    def resume_run(self, run_id: int) -> None:
        if self.get_run(run_id)['status'] == 'paused':
            self._set_status(run_id, 'running')
            self._schedule_run(run_id)

    async def stop_run(self, run_id: int) -> None:
        if self.get_run(run_id)['status'] in ('stopping', 'stopped', 'failed'):
            return
        self._set_status(run_id, 'stopping')
        self._unschedule_run(run_id)
        tasks = [t for r, _, t in self.active.values() if r == run_id]
        for t in tasks:
            t.cancel()  # also stops activities waiting for their turn on a local model
        if tasks:
            await asyncio.wait(tasks, timeout=30)
        self._set_status(run_id, 'stopped', ended_at=now())

    def add_agent(self, run_id: int, agent_id: str) -> None:
        """Add an agent from config/agents/ to an existing run. It sees the run's history from its first activity."""
        self.config.load()
        if agent_id not in self.config.agents:
            raise ValueError(f'No agent {agent_id!r} in config/agents/.')
        if agent_id in self.run_agents(run_id):
            return
        agent = self.config.agents[agent_id]
        ensure_workspace(self.config.root, agent)
        papers.index_collection(self.db, self.config.root, agent.papers_dir)
        self.db.execute('INSERT INTO run_agents (run_id, agent_id) VALUES (?,?)', (run_id, agent_id))
        self.db.emit('agent_added', {}, run_id, agent_id)
        if self.get_run(run_id)['status'] == 'running':
            self._add_job(run_id, agent_id, delay=5)

    def pause_agent(self, run_id: int, agent_id: str) -> None:
        self.db.execute('UPDATE run_agents SET paused = 1 WHERE run_id = ? AND agent_id = ?', (run_id, agent_id))
        self._remove_job(run_id, agent_id)
        self.db.emit('agent_paused', {}, run_id, agent_id)

    def resume_agent(self, run_id: int, agent_id: str) -> None:
        self.db.execute('UPDATE run_agents SET paused = 0 WHERE run_id = ? AND agent_id = ?', (run_id, agent_id))
        if self.get_run(run_id)['status'] == 'running':
            self._add_job(run_id, agent_id, delay=5)
        self.db.emit('agent_resumed', {}, run_id, agent_id)

    def cancel_activity(self, activity_id: int) -> None:
        if activity_id in self.active:
            self.active[activity_id][2].cancel()

    async def check_models(self, run_id: int) -> None:
        """Make sure each Ollama model the run uses is installed. Never switches models."""
        for agent_id in self.run_agents(run_id):
            cfg = self.config.models[self.config.agents[agent_id].model]
            if self.model is not None:
                continue
            if cfg.get('api_key_env') and not os.environ.get(cfg['api_key_env']):
                raise RuntimeError(f'{agent_id} uses {cfg["model"]}, which needs the {cfg["api_key_env"]} environment variable.')
            if cfg['provider'] != 'ollama':
                continue
            base = cfg['base_url'].removesuffix('/v1')
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    names = [m['name'] for m in (await client.get(f'{base}/api/tags')).json()['models']]
            except httpx.HTTPError as e:
                raise RuntimeError(f'Ollama is not reachable at {base}: {e}')
            if cfg['model'] not in names:
                raise RuntimeError(f'Ollama model {cfg["model"]} is not installed. Run: ollama pull {cfg["model"]}')

    def _set_status(self, run_id: int, status: str, **fields) -> None:
        sets = ', '.join(['status = ?'] + [f'{k} = ?' for k in fields])
        self.db.execute(f'UPDATE runs SET {sets} WHERE id = ?', (status, *fields.values(), run_id))
        self.db.emit('run_status', {'status': status}, run_id)

    # ---- Scheduling ----
    # One APScheduler job per (run, agent). Missed runs are coalesced into one, so a sleeping
    # Mac never builds up a backlog. A message to an agent can move its next run earlier.

    def _job_id(self, run_id: int, agent_id: str) -> str:
        return f'{run_id}:{agent_id}'

    def _schedule_run(self, run_id: int) -> None:
        for i, row in enumerate(self.db.all('SELECT agent_id FROM run_agents WHERE run_id = ? AND paused = 0', (run_id,))):
            self._add_job(run_id, row['agent_id'], delay=5 + 60 * i)

    def _unschedule_run(self, run_id: int) -> None:
        for agent_id in self.run_agents(run_id):
            self._remove_job(run_id, agent_id)

    def _add_job(self, run_id: int, agent_id: str, delay: float) -> None:
        a = self.config.society['activity']
        self.scheduler.add_job(
            self.run_activity, IntervalTrigger(minutes=a['interval_minutes'], jitter=a['jitter_minutes'] * 60),
            args=[run_id, agent_id, 'schedule'], id=self._job_id(run_id, agent_id), replace_existing=True,
            coalesce=True, max_instances=1, misfire_grace_time=None,
            next_run_time=datetime.now().astimezone() + timedelta(seconds=delay),
        )

    def _remove_job(self, run_id: int, agent_id: str) -> None:
        if self.scheduler.get_job(self._job_id(run_id, agent_id)):
            self.scheduler.remove_job(self._job_id(run_id, agent_id))

    def next_wake(self, run_id: int, agent_id: str) -> str | None:
        job = self.scheduler.get_job(self._job_id(run_id, agent_id))
        return job.next_run_time.isoformat(timespec='seconds') if job and job.next_run_time else None

    def wake(self, run_id: int, agent_id: str, immediate: bool = False) -> None:
        """Move an agent's next activity earlier, no sooner than the cooldown after its last activity started."""
        job = self.scheduler.get_job(self._job_id(run_id, agent_id))
        if not job or not job.next_run_time:
            return
        at = datetime.now().astimezone() + timedelta(seconds=1)
        if not immediate:
            last = self.db.one('SELECT MAX(started_at) AS t FROM activities WHERE run_id = ? AND agent_id = ?', (run_id, agent_id))['t']
            if last:
                cooldown = timedelta(minutes=self.config.society['activity']['wake_cooldown_minutes'])
                at = max(at, datetime.fromisoformat(last) + cooldown)
        if at < job.next_run_time:
            job.modify(next_run_time=at)

    # ---- Activities ----

    def _busy(self, run_id: int, agent_id: str) -> bool:
        return any(r == run_id and a == agent_id for r, a, _ in self.active.values())

    async def run_activity(self, run_id: int, agent_id: str, trigger: str) -> int | None:
        run = self.get_run(run_id)
        member = self.db.one('SELECT paused FROM run_agents WHERE run_id = ? AND agent_id = ?', (run_id, agent_id))
        if run['status'] != 'running' or not member or member['paused'] or self._busy(run_id, agent_id):
            return None
        self.config.load()  # prompt and config edits apply from the next activity
        agent = self.config.agents[agent_id]
        instructions, prompt_hash = self.config.prompt(agent)
        self.db.execute('INSERT OR IGNORE INTO prompt_versions VALUES (?,?,?)', (prompt_hash, instructions, now()))
        activity_id = self.db.insert(
            "INSERT INTO activities (run_id, agent_id, trigger, status, prompt_hash, started_at) VALUES (?,?,?, 'running', ?, ?)",
            (run_id, agent_id, trigger, prompt_hash, now()),
        )
        self.active[activity_id] = (run_id, agent_id, asyncio.current_task())
        self.db.emit('activity_started', {'trigger': trigger, 'prompt_hash': prompt_hash}, run_id, agent_id, activity_id)
        current_activity.set((activity_id, agent_id))
        status, error = 'completed', None
        # A local model runs one whole activity at a time, so each activity keeps the model's prompt cache to itself.
        # Waiting for a turn does not count toward the activity's time limit.
        local = self.model is not None or self.config.models[agent.model]['provider'] == 'ollama'
        try:
            async with self.inference_lock if local else nullcontext():
                prompt = self._activity_prompt(run, agent)
                ai = build_agent(self, agent, activity_id, instructions, self.model)
                limits = self.config.society['activity']
                result = await asyncio.wait_for(
                    ai.run(
                        prompt, deps=Deps(self, run_id, agent, activity_id),
                        usage_limits=UsageLimits(request_limit=limits['max_model_requests']),
                        event_stream_handler=self._log_tools(run_id, agent_id, activity_id),
                    ),
                    timeout=limits['max_duration_minutes'] * 60,
                )
                self._finish(run_id, agent, activity_id, result.output)
        except asyncio.CancelledError:
            status, error = 'canceled', 'canceled'
        except TimeoutError:
            status, error = 'timed_out', f'exceeded {self.config.society["activity"]["max_duration_minutes"]} minutes'
        except Exception as e:
            status, error = 'failed', f'{type(e).__name__}: {e}'
        finally:
            del self.active[activity_id]
            if self.config.society['sandbox']['enabled']:
                await remove_container(activity_id)  # also ends any background jobs the agent started
            self.db.execute('UPDATE activities SET status = ?, ended_at = ?, error = ? WHERE id = ?', (status, now(), error, activity_id))
            self.db.emit('activity_ended', {'status': status, 'error': error}, run_id, agent_id, activity_id)
        return activity_id

    def _finish(self, run_id: int, agent, activity_id: int, output: ActivityResult) -> None:
        """Post the public update and save the agent's memory."""
        thread = self.db.one('SELECT id FROM threads WHERE run_id = ? AND title = ? AND author = ?', (run_id, UPDATES_THREAD, 'system'))
        msg_id = self.db.insert(
            'INSERT INTO messages (run_id, thread_id, author, body, created_at, idem) VALUES (?,?,?,?,?,?)',
            (run_id, thread['id'], agent.id, output.public_update, now(), f'{activity_id}:update'),
        )
        for other in self.run_agents(run_id):
            if other != agent.id:
                self.db.execute('INSERT OR IGNORE INTO unread VALUES (?,?)', (msg_id, other))
        self.db.index(f'message:{msg_id}', agent.id, UPDATES_THREAD, output.public_update)
        memory_dir = self.config.root / agent.workspace / 'memory'
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / 'memory.md').write_text(output.memory)
        with open(memory_dir / 'history.md', 'a') as f:
            f.write(f'\n\n## activity {activity_id} ({now()})\n\n{output.memory}\n')
        self.db.execute('UPDATE activities SET public_update = ? WHERE id = ?', (output.public_update, activity_id))
        self.db.emit('update', {'message_id': msg_id, 'body': output.public_update}, run_id, agent.id, activity_id)

    def _activity_prompt(self, run: dict, agent) -> str:
        """What the agent sees when it wakes: the question, its memory, and what is new since last time."""
        db = self.db
        memory_file = self.config.root / agent.workspace / 'memory' / 'memory.md'
        memory = memory_file.read_text() if memory_file.exists() else '(empty: this is your first session)'
        unread = db.all(
            'SELECT m.id, m.author, m.recipient, m.thread_id, t.title, m.body FROM unread u JOIN messages m ON m.id = u.message_id '
            'LEFT JOIN threads t ON t.id = m.thread_id WHERE u.agent_id = ? AND m.run_id = ? ORDER BY m.id DESC LIMIT 12',
            (agent.id, run['id']),
        )
        db.execute('DELETE FROM unread WHERE agent_id = ? AND message_id IN (SELECT id FROM messages WHERE run_id = ?)', (agent.id, run['id']))
        lines = []
        for m in reversed(unread):
            where = 'DM to you' if m['recipient'] else f'thread {m["thread_id"]} "{m["title"]}"'
            body = m['body'] if len(m['body']) < 1500 else m['body'][:1500] + ' ...(truncated; use read_reference)'
            lines.append(f'- message:{m["id"]} from {m["author"]} ({where}):\n{body}')
        pending = db.all(
            "SELECT p.id, p.title, r.last_page FROM paper_reads r JOIN papers p ON p.id = r.paper_id "
            "WHERE r.agent_id = ? AND r.status = 'notes_pending'",
            (agent.id,),
        )
        my_papers = db.all(
            'SELECT p.id, p.title, r.status, r.last_page FROM papers p LEFT JOIN paper_reads r ON r.paper_id = p.id AND r.agent_id = ? '
            'WHERE p.collection = ?',
            (agent.id, agent.papers_dir),
        )
        others = [a for a in self.run_agents(run['id']) if a != agent.id]
        parts = [
            f'Research question: {run["question"]}',
            f'You are {agent.name} (id: {agent.id}). Other agents: {", ".join(others) or "none"}. Time: {now()}.',
            f'## Your memory from previous sessions\n\n{memory}',
            '## New messages\n\n' + ('\n\n'.join(lines) if lines else '(none)'),
            '## Your paper collection (' + agent.papers_dir + ')\n\n'
            + '\n'.join(f'- paper:{p["id"]} {p["title"]} [{p["status"] or "not read"}'
                        + (f', read to page {p["last_page"]}' if p['last_page'] else '') + ']' for p in my_papers),
        ]
        if pending:
            parts.append('You started reading, without saving notes yet: ' + ', '.join(
                f'paper:{p["id"]} {p["title"]} (read to page {p["last_page"]})' for p in pending)
                + '. Continue reading or save notes; you need to save notes before starting a different paper.')
        every = self.config.society['activity'].get('field_reading_every', 0)
        done = db.one("SELECT COUNT(*) AS n FROM activities WHERE run_id = ? AND agent_id = ? AND status = 'completed'",
                      (run['id'], agent.id))['n']
        if every and done % every == every - 1:
            parts.append((self.config.root / 'config/prompts/field_reading.md').read_text())
        parts.append('Decide what to do in this session, do it, then return your public update and memory.')
        return '\n\n'.join(parts)

    def _log_tools(self, run_id: int, agent_id: str, activity_id: int):
        """Event handler that records every tool call and its result."""
        calls: dict[str, int] = {}

        async def handler(ctx, events):
            async for event in events:
                if isinstance(event, FunctionToolCallEvent):
                    args = event.part.args if isinstance(event.part.args, str) else json.dumps(event.part.args)
                    calls[event.part.tool_call_id] = self.db.insert(
                        'INSERT INTO tool_calls (activity_id, agent_id, tool, args, started_at) VALUES (?,?,?,?,?)',
                        (activity_id, agent_id, event.part.tool_name, args, now()),
                    )
                    self.db.emit('tool_call', {'tool': event.part.tool_name, 'args': (args or '')[:2000]}, run_id, agent_id, activity_id)
                elif isinstance(event, FunctionToolResultEvent):
                    content = str(getattr(event.part, 'content', ''))
                    row = calls.get(event.part.tool_call_id)
                    if row:
                        self.db.execute('UPDATE tool_calls SET result = ?, ended_at = ? WHERE id = ?', (content[:20000], now(), row))
                    self.db.emit('tool_result', {'tool': event.part.tool_name, 'result': content[:2000]}, run_id, agent_id, activity_id)

        return handler
