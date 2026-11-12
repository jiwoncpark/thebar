"""Build a Pydantic AI agent for one activity, using Harness capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from ddgs import DDGS
from pydantic import BaseModel, Field
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai_harness import Coder, DockerSandbox
from pydantic_ai_harness.compaction import SlidingWindowCompaction, WarnNearLimits
from pydantic_ai_harness.step_persistence import StepPersistence

from thebar.config import AgentConfig
from thebar.db import now
from thebar.tools import SHARED_TOOLS, Deps, toolset

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
    if cfg['provider'] == 'anthropic':
        return AnthropicModel(cfg['model'], provider=AnthropicProvider(api_key=os.environ.get(cfg.get('api_key_env', 'ANTHROPIC_API_KEY'))))
    if cfg['provider'] == 'openai':
        return OpenAIChatModel(cfg['model'], provider=OpenAIProvider(api_key=os.environ.get(cfg['api_key_env']), base_url=cfg.get('base_url')))
    raise ValueError(f'Unknown provider {cfg["provider"]!r}')


async def web_search(query: str) -> list[dict] | str:
    """Searches the web for the given query and returns the results."""
    try:
        # 'auto' tries several engines (Bing, Google, Yahoo, DuckDuckGo, ...), so one engine blocking us does not stop search.
        return await asyncio.to_thread(DDGS().text, query, max_results=8, backend='auto')
    except Exception as e:  # e.g. no results, or rate limited; tell the agent instead of ending the activity
        return f'Search failed: {e}. Try other words, or search arXiv with search_papers.'


class LoggedModel(WrapperModel):
    """Logs each model request to the model_calls table."""

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
        started = time.monotonic()
        ok = False
        try:
            async with self.wrapped.request_stream(*args, **kwargs) as stream:
                yield stream
            ok = True
        finally:
            self._log(started, ok, stream.usage if ok else None)


def model_settings(cfg: dict) -> dict:
    settings = {'temperature': cfg['temperature']} if 'temperature' in cfg else {}
    if cfg['provider'] == 'anthropic':
        settings |= {
            'anthropic_cache': True,  # cache the growing conversation between requests
            'anthropic_effort': cfg.get('effort', 'medium'),
            # If the model declines a request, the API retries it on another model instead of stopping.
            'anthropic_betas': ['server-side-fallback-2026-07-01'],
            'extra_body': {'fallbacks': 'default'},
        }
    return settings


def project_label(root: Path) -> str:
    """Identifies this project's containers, so a second copy of the app (e.g. tests) never removes ours."""
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]


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
                    '--label', f'thebar={project_label(society.config.root)}', '--label', f'thebar.activity={activity_id}',
                    '--memory', str(sb['memory_limit']), '--cpus', str(sb['cpu_limit']), '--pids-limit', '256',
                    '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--volume', f'{workspace / "scratch"}:/workspace/scratch',
                    '--volume', f'{workspace / "tools"}:/workspace/tools',
                    '--volume', f'{(society.config.root / SHARED_TOOLS).resolve()}:/workspace/shared_tools',
                    # Recorded experiments, read-only, so manuscripts can include any agent's plots.
                    '--volume', f'{(society.config.root / "artifacts/experiments").resolve()}:/workspace/experiments:ro',
                ],
            ),
            Coder(sub_agents=False, repo_context=False),
        ]
    if s.get('web_search'):
        # Search run by the app (several engines via ddgs), not the provider's own, so every search is logged and visible in the UI.
        capabilities += [WebSearch(native=False, local=Tool(web_search)), WebFetch(native=False, local=True)]
    return Agent(
        LoggedModel(model or build_model(model_cfg), society),
        name=agent.id,
        instructions=instructions,
        deps_type=Deps,
        output_type=ActivityResult,
        toolsets=[toolset],
        retries=3,  # how many times a tool can reject a call before the activity fails
        capabilities=capabilities,
        model_settings=model_settings(model_cfg),
    )


async def remove_container(root: Path, activity_id: int) -> None:
    await _remove(f'label=thebar={project_label(root)}', f'label=thebar.activity={activity_id}')


async def remove_all_containers(root: Path) -> None:
    """Remove this project's sandbox containers left over from a previous process."""
    await _remove(f'label=thebar={project_label(root)}')


async def _remove(*filters: str) -> None:
    """Remove the containers matching all the given docker label filters."""
    args = [a for f in filters for a in ('--filter', f)]
    proc = await asyncio.create_subprocess_exec('docker', 'ps', '-aq', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if ids := out.decode().split():
        rm = await asyncio.create_subprocess_exec('docker', 'rm', '-f', *ids, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await rm.wait()


def ensure_workspace(root: Path, agent: AgentConfig) -> None:
    for sub in ('memory', 'scratch', 'tools'):
        (root / agent.workspace / sub).mkdir(parents=True, exist_ok=True)
    (root / SHARED_TOOLS).mkdir(parents=True, exist_ok=True)
    (root / 'artifacts/experiments').mkdir(parents=True, exist_ok=True)
