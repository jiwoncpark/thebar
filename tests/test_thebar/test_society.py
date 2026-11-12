import asyncio

import pytest

from scripted import scripted_model
from thebar.society import Society


async def make(root, steps) -> tuple[Society, int]:
    society = Society(root, model=scripted_model(steps))
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    return society, run_id


async def test_activity_posts_update_and_memory(root):
    society, run_id = await make(root, {'ardor': [('create_thread', {'title': 'Judges', 'body': 'What is the estimand?'})], 'ada': []})
    activity_id = await society.run_activity(run_id, 'ardor', 'test')
    act = society.db.one('SELECT * FROM activities WHERE id = ?', (activity_id,))
    assert act['status'] == 'completed', act['error']
    assert (root / 'agents/ardor/memory/memory.md').read_text() == 'ardor memory'
    bodies = [m['body'] for m in society.db.all('SELECT body FROM messages ORDER BY id')]
    assert bodies == ['What is the estimand?', 'ardor finished step 1']
    # Ada has both messages unread, and sees them in her next prompt.
    assert society.db.one("SELECT COUNT(*) AS n FROM unread WHERE agent_id = 'ada'")['n'] == 2
    prompt = society._activity_prompt(society.get_run(run_id), society.config.agents['ada'])
    assert 'What is the estimand?' in prompt
    assert society.db.one("SELECT COUNT(*) AS n FROM unread WHERE agent_id = 'ada'")['n'] == 0
    tools = [r['tool'] for r in society.db.all('SELECT tool FROM tool_calls')]
    assert tools == ['create_thread']
    await society.shutdown()


async def test_paper_notes_are_required(root):
    steps = [
        ('read_paper', {'paper_id': 1}),
        ('read_paper', {'paper_id': 2}),  # refused: notes for paper 1 are pending
        ('save_paper_notes', {'paper_id': 1, 'notes': '## Citation\nc'}),  # refused: headings are missing
        ('save_paper_notes', {'paper_id': 1, 'notes': '## Citation\nc\n## Research question\nq\n## Main contribution\nm\n## Method and assumptions\na\n## Results\nr\n## Critical assessment\nx\n## Connection to our research question\ny\n## Connections to other work\nz'}),
    ]
    society, run_id = await make(root, {'ardor': steps, 'ada': []})
    await society.run_activity(run_id, 'ardor', 'test')
    results = [r['result'] for r in society.db.all('SELECT result FROM tool_calls ORDER BY id')]
    assert 'Save notes for paper:1' in results[1]
    assert 'missing these headings' in results[2]
    assert society.db.one("SELECT status FROM paper_reads WHERE paper_id = 1")['status'] == 'completed'
    notes = list((root / 'data/papers').glob('*/notes_*.md'))
    assert len(notes) == 1 and '## Critical assessment' in notes[0].read_text()
    await society.shutdown()


async def test_proposal_revision_needs_current_revision(root):
    steps = [
        ('write_proposal', {'title': 'Calibrated judge', 'body': 'v1', 'references': ['paper:1']}),
        ('write_proposal', {'title': 'Calibrated judge', 'body': 'v2', 'references': [], 'proposal_id': 1, 'base_revision': 1}),
        ('write_proposal', {'title': 'Calibrated judge', 'body': 'stale', 'references': [], 'proposal_id': 1, 'base_revision': 1}),
        ('write_proposal', {'title': 'Fork', 'body': 'f', 'references': ['proposal:1@2'], 'fork_of': 1}),
    ]
    society, run_id = await make(root, {'ada': steps, 'ardor': []})
    await society.run_activity(run_id, 'ada', 'test')
    versions = society.db.all('SELECT artifact_id, revision, body FROM artifact_versions ORDER BY artifact_id, revision')
    assert [(v['artifact_id'], v['revision'], v['body']) for v in versions] == [(1, 1, 'v1'), (1, 2, 'v2'), (2, 1, 'f')]
    assert 'revision: 2' in (root / 'proposals/calibrated_judge.md').read_text()
    assert society.db.one('SELECT forked_from FROM artifacts WHERE id = 2')['forked_from'] == 1
    await society.shutdown()


async def test_dms_are_private(root):
    society, run_id = await make(root, {'ardor': [('send_dm', {'to': 'ada', 'body': 'secret plan'})], 'ada': []})
    await society.run_activity(run_id, 'ardor', 'test')
    from thebar.tools import resolve
    msg = society.db.one("SELECT id FROM messages WHERE body = 'secret plan'")['id']
    assert 'secret plan' in resolve(society, f'message:{msg}', 'ada')
    society.config.agents['third'] = society.config.agents['ada']
    with pytest.raises(KeyError):
        resolve(society, f'message:{msg}', 'third')
    await society.shutdown()


async def test_lifecycle_and_cancel(root):
    society, run_id = await make(root, {'ardor': [], 'ada': []})
    assert society.scheduler.get_job(f'{run_id}:ardor')
    society.pause_run(run_id)
    society.pause_run(run_id)  # idempotent
    assert society.get_run(run_id)['status'] == 'paused'
    assert society.scheduler.get_job(f'{run_id}:ardor') is None
    assert await society.run_activity(run_id, 'ardor', 'test') is None  # paused runs start nothing
    society.resume_run(run_id)
    assert society.get_run(run_id)['status'] == 'running'

    # An activity stuck waiting for the model is canceled by stop.
    await society.inference_lock.acquire()
    task = asyncio.create_task(society.run_activity(run_id, 'ada', 'test'))
    await asyncio.sleep(0.2)
    await society.stop_run(run_id)
    society.inference_lock.release()
    activity_id = await task
    assert society.db.one('SELECT status FROM activities WHERE id = ?', (activity_id,))['status'] == 'canceled'
    assert society.get_run(run_id)['status'] == 'stopped'
    await society.stop_run(run_id)  # idempotent
    await society.shutdown()


async def test_restart_recovery(root):
    society, run_id = await make(root, {'ardor': [], 'ada': []})
    society.db.execute("INSERT INTO activities (run_id, agent_id, status, started_at) VALUES (?, 'ada', 'running', '2026-01-01')", (run_id,))
    await society.shutdown()

    again = Society(root, model=scripted_model({'ardor': [], 'ada': []}))
    await again.start()
    assert again.db.one("SELECT status FROM activities WHERE agent_id = 'ada'")['status'] == 'interrupted'
    assert again.scheduler.get_job(f'{run_id}:ada')  # running runs are rescheduled
    await again.shutdown()


async def test_wake_respects_cooldown(root):
    society, run_id = await make(root, {'ardor': [], 'ada': []})
    society.pause_run(run_id)
    society.resume_run(run_id)
    job = society.scheduler.get_job(f'{run_id}:ada')
    job.modify(next_run_time=job.next_run_time.replace(year=2030))
    await society.run_activity(run_id, 'ada', 'test')
    society.wake(run_id, 'ada')
    wake_at = society.scheduler.get_job(f'{run_id}:ada').next_run_time
    started = society.db.one("SELECT started_at FROM activities WHERE agent_id = 'ada'")['started_at']
    from datetime import datetime
    assert (wake_at - datetime.fromisoformat(started)).total_seconds() >= 9 * 60
    await society.shutdown()


async def test_reading_progress_is_reported_and_kept(root):
    steps = [('read_paper', {'paper_id': 2, 'start_page': 1, 'num_pages': 20}), ('read_paper', {'paper_id': 2, 'start_page': 999})]
    society, run_id = await make(root, {'ardor': steps, 'ada': []})
    await society.run_activity(run_id, 'ardor', 'test')
    first, second = [r['result'] for r in society.db.all('SELECT result FROM tool_calls ORDER BY id')]
    total = int(first.split(' of ')[1].split()[0])
    assert ', pages 1-' in first and f'of {total}' in first
    last = society.db.one('SELECT last_page FROM paper_reads WHERE paper_id = 2')['last_page']
    assert 1 <= last <= 5  # at most 5 pages per call, whole pages only
    if last < total:
        assert f'start_page={last + 1}' in first and 'Decide whether you have read enough' in first
    assert f'has pages 1 to {total}' in second
    prompt = society._activity_prompt(society.get_run(run_id), society.config.agents['ardor'])
    assert f'read to page {last}' in prompt
    await society.shutdown()


async def test_add_agent_to_existing_run(root):
    path = root / 'config/agents/thea.yaml'
    path.write_text(path.read_text().replace('enabled: true', 'enabled: false'))
    society, run_id = await make(root, {'ardor': [], 'ada': [], 'thea': []})
    assert 'thea' not in society.run_agents(run_id)
    path.write_text(path.read_text().replace('enabled: false', 'enabled: true'))
    society.add_agent(run_id, 'thea')
    assert 'thea' in society.run_agents(run_id)
    assert society.scheduler.get_job(f'{run_id}:thea') is not None
    activity_id = await society.run_activity(run_id, 'thea', 'test')
    assert society.db.one('SELECT status FROM activities WHERE id = ?', (activity_id,))['status'] == 'completed'
    with pytest.raises(ValueError):
        society.add_agent(run_id, 'nobody')
    await society.shutdown()


async def test_field_reading_every_fourth_activity(root):
    society, run_id = await make(root, {'ardor': [], 'ada': [], 'thea': []})
    seen = []
    for _ in range(8):
        seen.append('field-reading session' in society._activity_prompt(society.get_run(run_id), society.config.agents['thea']))
        await society.run_activity(run_id, 'thea', 'test')
    assert seen == [False, False, False, True, False, False, False, True]
    await society.shutdown()
