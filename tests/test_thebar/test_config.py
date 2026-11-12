from thebar.config import Config


def test_loads_agents_and_prompt(root):
    config = Config(root)
    assert set(config.agents) == {'ardor', 'ada', 'thea'}
    assert config.validate() == []
    text, h = config.prompt(config.agents['ardor'])
    assert 'You are Ardor' in text and len(h) == 12


def test_third_agent_by_config_only(root):
    (root / 'config/agents/lolita.yaml').write_text(
        'id: lolita\nname: Lolita\nidentity_prompt: config/prompts/ada.md\npapers_dir: data/papers/x\nworkspace: agents/lolita\n')
    config = Config(root)
    assert config.agents['lolita'].model == config.default_model
    assert config.validate() == []
