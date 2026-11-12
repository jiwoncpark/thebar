from scripted import scripted_model
from thebar import timeline
from thebar.society import UPDATES_THREAD, Society

NOTES = {'notes': '## Citation\nc\n## Research question\nq\n## Main contribution\nm\n## Method and assumptions\na\n'
                  '## Results\nr\n## Critical assessment\nx\n## Connection to our research question\ny\n## Connections to other work\nz'}


def test_refs_in():
    assert timeline.refs_in('see paper:2 and proposal:1@3.', None) == ['paper:2', 'proposal:1@3']


async def test_nodes_and_edges(root):
    steps = [
        ('read_paper', {'paper_id': 1}),
        ('save_paper_notes', {'paper_id': 1, **NOTES}),
        ('save_insight', {'title': 'Calibration', 'body': '## Summary\n' + 'Builds on paper:1.', 'references': []}),
        ('create_thread', {'title': 'Judges', 'body': 'See insight:1'}),
        ('post_reply', {'thread_id': 2, 'body': 'I disagree.'}),
        ('write_proposal', {'title': 'P', 'body': '## Summary\n' + 'v1', 'references': ['insight:1']}),
        ('write_proposal', {'title': 'P', 'body': '## Summary\n' + 'v2', 'references': [], 'proposal_id': 2, 'base_revision': 1}),
    ]
    society = Society(root, model=scripted_model({'ada': steps, 'ardor': []}))
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    await society.run_activity(run_id, 'ada', 'test')
    t = timeline.build(society.db, run_id, UPDATES_THREAD)

    kinds = {n['key']: n['kind'] for n in t['nodes']}
    assert kinds['paper:1'] == 'paper'
    assert kinds['insight:1@1'] == 'insight' and kinds['proposal:2@2'] == 'proposal'
    assert [n['kind'] for n in t['nodes']].count('update') == 1
    activity = next(n for n in t['nodes'] if n['kind'] == 'activity')
    assert activity['tools'][:2] == ['read_paper', 'save_paper_notes']

    edges = {(e['from'], e['to'], e['type']) for e in t['edges']}
    assert ('insight:1@1', 'paper:1', 'ref') in edges
    assert ('message:1', 'insight:1@1', 'ref') in edges
    assert ('message:2', 'message:1', 'reply') in edges
    assert ('proposal:2@1', 'insight:1@1', 'ref') in edges
    assert ('proposal:2@2', 'proposal:2@1', 'revision') in edges
    # Every edge points to an earlier (or same-time) node.
    time = {n['key']: n['time'] for n in t['nodes']}
    assert all(time[e['to']] <= time[e['from']] for e in t['edges'])
    await society.shutdown()


def test_experiment_images_include_captions(tmp_path):
    folder = tmp_path / 'artifacts/experiments/1'
    folder.mkdir(parents=True)
    (folder / 'gap.png').write_bytes(b'png')
    (folder / 'gap.txt').write_text('What the gap means.')
    (folder / 'raw.png').write_bytes(b'png')
    (folder / 'results.json').write_text('{}')
    assert timeline.experiment_images(tmp_path, 'artifacts/experiments/1') == [
        {'path': 'artifacts/experiments/1/gap.png', 'caption': 'What the gap means.'},
        {'path': 'artifacts/experiments/1/raw.png', 'caption': ''},
    ]
