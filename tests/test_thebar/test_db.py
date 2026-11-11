from thebar.db import DB


def test_emit_and_search():
    db = DB(':memory:')
    event_id = db.emit('post', {'body': 'hi'}, run_id=1, agent_id='ada')
    assert db.one('SELECT type FROM events WHERE id = ?', (event_id,))['type'] == 'post'
    db.index('message:1', 'ada', '', 'martingale posteriors are predictive')
    db.index('message:2', 'ardor', '', 'scaling laws')
    assert [h['ref'] for h in db.search('martingale')] == ['message:1']
    assert db.search('scaling', prefix='paper:') == []


def test_search_tolerates_fts_syntax():
    db = DB(':memory:')
    db.index('message:1', None, '', 'a "quoted" OR thing')
    assert db.search('"quoted" OR (') != []
