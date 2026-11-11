"""Build a Pydantic AI agent for one activity, using Harness capabilities."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai_harness import Coder, DockerSandbox
from pydantic_ai_harness.compaction import SlidingWindowCompaction, WarnNearLimits
from pydantic_ai_harness.step_persistence import StepPersistence

from thebar.config import AgentConfig
from thebar.db import now
from thebar.tools import Deps, toolset

if TYPE_CHECKING:
    from thebar.society import Society

# (activity_id, agent_id) of the activity making the current model request, for logging.
current_activity: ContextVar[tuple[int, str] | None] = ContextVar('current_activity', default=None)


class ActivityResult(BaseModel):
    public_update: str = Field(description='Substantive update for the other agents about what you did or found.')
    memory: str = Field(description='Your private notes for your next session. Replaces your previous memory.')


def build_model(cfg: dict) -> Model:
    if cfg['provider'] == 'ollama':
        return OpenAIChatModel(cfg['model'], provider=OllamaProvider(base_url=cfg['base_url']))
    if cfg['provider'] == 'openai':
        return OpenAIChatModel(cfg['model'], provider=OpenAIProvider(api_key=os.environ.get(cfg['api_key_env']), base_url=cfg.get('base_url')))
    raise ValueError(f'Unknown provider {cfg["provider"]!r}')


class QueuedModel(WrapperModel):
    """Lets one model request run at a time across all agents, and logs each request.

    asyncio.Lock serves waiters in arrival order, and each agent has at most one waiting
    request, so agents take turns.
    """

    def __init__(self, wrapped: Model, society: Society):
        super().__init__(wrapped)
        self.society = society

    def _log(self, started: float, ok: bool, usage=None) -> None:
        activity = current_activity.get()
        self.society.db.insert(
            'INSERT INTO model_calls (activity_id, agent_id, started_at, seconds, input_tokens, output_tokens, ok) VALUES (?,?,?,?,?,?,?)',
            (activity and activity[0], activity and activity[1], now(), round(time.monotonic() - started, 2),
             usage.input_tokens if usage else None, usage.output_tokens if usage else None, int(ok)),
        )

    async def request(self, *args, **kwargs):
        async with self.society.inference_lock:
            started = time.monotonic()
            try:
                response = await self.wrapped.request(*args, **kwargs)
            except BaseException:
                self._log(started, False)
                raise
            self._log(started, True, response.usage)
            return response

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs) -> AsyncIterator:
        async with self.society.inference_lock:
            started = time.monotonic()
            ok = False
            try:
                async with self.wrapped.request_stream(*args, **kwargs) as stream:
                    yield stream
                ok = True
            finally:
                self._log(started, ok, stream.usage if ok else None)


def container_name(activity_id: int) -> str:
    return f'thebar-activity-{activity_id}'


def build_agent(society: Society, agent: AgentConfig, activity_id: int, instructions: str, model: Model | None = None) -> Agent:
    s = society.config.society
    model_cfg = society.config.models[agent.model]
    context_tokens = model_cfg.get('context_tokens', 16384)
    capabilities = [
        StepPersistence(store=society.step_store, run_id=f'activity-{activity_id}', agent_name=agent.id),
        SlidingWindowCompaction(max_tokens=int(context_tokens * 0.6), keep_tokens=int(context_tokens * 0.3)),
        WarnNearLimits(max_iterations=s['activity']['max_model_requests']),
    ]
    sb = s['sandbox']
    if sb['enabled']:
        workspace = (society.config.root / agent.workspace).resolve()
        capabilities += [
            DockerSandbox(
                image=sb['image'],
                allow_internet_access=sb['network_access'],
                env={'HOME': '/workspace/scratch', 'MPLCONFIGDIR': '/workspace/scratch/.matplotlib'},
                docker_args=[
                    '--name', container_name(activity_id), '--label', 'thebar=1',
                    '--memory', str(sb['memory_limit']), '--cpus', str(sb['cpu_limit']), '--pids-limit', '256',
                    '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--volume', f'{workspace / "scratch"}:/workspace/scratch',
                    '--volume', f'{workspace / "tools"}:/workspace/tools',
                ],
            ),
            Coder(sub_agents=False, repo_context=False),
        ]
    if s.get('web_search'):
        capabilities += [WebSearch(local=True), WebFetch(local=True)]
    return Agent(
        QueuedModel(model or build_model(model_cfg), society),
        name=agent.id,
        instructions=instructions,
        deps_type=Deps,
        output_type=ActivityResult,
        toolsets=[toolset],
        capabilities=capabilities,
        model_settings={'temperature': model_cfg.get('temperature', 0.7)},
    )


async def remove_container(activity_id: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        'docker', 'rm', '-f', container_name(activity_id),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def remove_all_containers() -> None:
    """Remove sandbox containers left over from a previous process."""
    proc = await asyncio.create_subprocess_exec(
        'docker', 'ps', '-aq', '--filter', 'label=thebar=1', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    ids = out.decode().split()
    if ids:
        rm = await asyncio.create_subprocess_exec('docker', 'rm', '-f', *ids, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await rm.wait()


def ensure_workspace(root: Path, agent: AgentConfig) -> None:
    for sub in ('memory', 'scratch', 'tools'):
        (root / agent.workspace / sub).mkdir(parents=True, exist_ok=True)
