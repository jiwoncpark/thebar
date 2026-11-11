import pytest
from fastapi.testclient import TestClient

from scripted import scripted_model
from thebar import api
from thebar.society import Society


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setattr(api, 'ROOT', root)
    # Use the scripted model instead of Ollama.
    monkeypatch.setattr(api, 'Society', lambda r: Society(r, model=scripted_model({'ardor': [], 'ada': []})))
    with TestClient(api.app) as c:
        yield c


def test_run_controls(client):
    run_id = client.post('/api/runs', json={'question': 'Q?'}).json()['id']
    assert client.post(f'/api/runs/{run_id}/start').json()['status'] == 'running'
    assert client.post(f'/api/runs/{run_id}/start').json()['status'] == 'running'  # idempotent
    assert client.post(f'/api/runs/{run_id}/pause').json()['status'] == 'paused'
    assert client.post(f'/api/runs/{run_id}/resume').json()['status'] == 'running'
    assert client.post(f'/api/runs/{run_id}/agents/ada/pause').json()['paused'] is True
    assert client.post(f'/api/runs/{run_id}/agents/ada/resume').json()['paused'] is False
    assert client.post(f'/api/runs/{run_id}/stop').json()['status'] == 'stopped'
    run = client.get(f'/api/runs/{run_id}').json()
    assert run['question'] == 'Q?' and 'config/society.yaml' in run['config']
    types = [e['type'] for e in client.get(f'/api/events?run_id={run_id}').json()]
    assert types[0] == 'run_created' and types[-1] == 'run_status'
    assert client.get('/api/runs/999').status_code == 404


def test_config_edit_keeps_history_and_rejects_bad_yaml(client, root):
    path = 'config/prompts/ada.md'
    assert client.put('/api/config', json={'path': path, 'text': 'You are Ada, edited.'}).status_code == 200
    assert (root / path).read_text() == 'You are Ada, edited.'
    assert client.get('/api/config').json()['history'][0]['path'] == path
    bad = client.put('/api/config', json={'path': 'config/society.yaml', 'text': ': : :'})
    assert bad.status_code == 400 and 'question' in (root / 'config/society.yaml').read_text()
    assert client.put('/api/config', json={'path': '../etc/x', 'text': ''}).status_code == 400


def test_file_access_is_limited(client):
    assert client.get('/api/file', params={'path': 'config/prompts/ada.md'}).status_code == 200
    assert client.get('/api/file', params={'path': '../../etc/passwd'}).status_code == 404
    assert client.get('/').status_code == 200
