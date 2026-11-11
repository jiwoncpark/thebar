import shutil
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of the project config with one small paper per agent. Sandbox and web search are off."""
    shutil.copytree(REPO / 'config', tmp_path / 'config')
    society = yaml.safe_load((tmp_path / 'config/society.yaml').read_text())
    society['sandbox']['enabled'] = False
    society['web_search'] = False
    (tmp_path / 'config/society.yaml').write_text(yaml.safe_dump(society))
    for collection, name in [('post_bayesian', 'digital_twins_causality.pdf'), ('frontier_scaling', 'The Era of Experience Paper.pdf')]:
        (tmp_path / 'data/papers' / collection).mkdir(parents=True)
        shutil.copy(REPO / 'data/papers' / collection / name, tmp_path / 'data/papers' / collection / name)
    return tmp_path
