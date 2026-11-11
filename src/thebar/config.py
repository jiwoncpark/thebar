"""Load YAML configuration and Markdown prompts from the config/ directory."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    id: str
    name: str
    model: str
    identity_prompt: str
    papers_dir: str
    workspace: str
    enabled: bool = True


class Config:
    """All settings, read fresh from disk each time `load()` is called."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.load()

    def load(self) -> None:
        cfg = self.root / 'config'
        self.society = yaml.safe_load((cfg / 'society.yaml').read_text())
        models = yaml.safe_load((cfg / 'models.yaml').read_text())
        self.models: dict = models['models']
        self.default_model: str = models['default']
        self.agents: dict[str, AgentConfig] = {}
        for path in sorted((cfg / 'agents').glob('*.yaml')):
            data = yaml.safe_load(path.read_text())
            data.setdefault('model', self.default_model)
            agent = AgentConfig(**data)
            self.agents[agent.id] = agent

    def enabled_agents(self) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.enabled]

    def path(self, relative: str) -> Path:
        return self.root / relative

    def prompt(self, agent: AgentConfig) -> tuple[str, str]:
        """The full instructions for an agent, and a short hash identifying this prompt version."""
        prompts = self.root / 'config' / 'prompts'
        text = '\n\n'.join([
            (prompts / 'system.md').read_text(),
            self.path(agent.identity_prompt).read_text(),
            (prompts / 'activity.md').read_text(),
        ])
        return text, hashlib.sha256(text.encode()).hexdigest()[:12]

    def snapshot(self) -> dict[str, str]:
        """Text of every config file, stored with each run so it can be reproduced later."""
        return {str(p.relative_to(self.root)): p.read_text() for p in sorted((self.root / 'config').rglob('*')) if p.is_file()}

    def validate(self) -> list[str]:
        problems = []
        for agent in self.enabled_agents():
            if agent.model not in self.models:
                problems.append(f'{agent.id}: unknown model {agent.model!r}')
            if not self.path(agent.identity_prompt).exists():
                problems.append(f'{agent.id}: missing prompt {agent.identity_prompt}')
        if not self.enabled_agents():
            problems.append('no enabled agents')
        return problems
