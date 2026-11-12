import json

from scripted import scripted_model
from thebar import openalex
from thebar.society import Society


async def test_fetch_openalex_works_writes_rows_to_sandbox(root, monkeypatch):
    async def fake_fetch(search, filter, sort, limit):
        assert (search, filter, limit) == ('judges', 'publication_year:2020', 2000)
        return [{'id': 'W1', 'year': 2020, 'cited_by_count': 4, 'title': 'A'}], 99

    monkeypatch.setattr(openalex, 'fetch_works', fake_fetch)
    steps = [('fetch_openalex_works', {'save_as': 'Judges 2020', 'search': 'judges', 'filter': 'publication_year:2020', 'max_results': 5000})]
    society = Society(root, model=scripted_model({'ada': steps, 'ardor': []}))
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    await society.run_activity(run_id, 'ada', 'test')

    path = root / 'agents/ada/scratch/data/judges_2020.jsonl'
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{'id': 'W1', 'year': 2020, 'cited_by_count': 4, 'title': 'A'}]
    result = society.db.one("SELECT result FROM tool_calls WHERE tool = 'fetch_openalex_works'")['result']
    assert 'Saved 1 of 99 matching works to scratch/data/judges_2020.jsonl' in result
    await society.shutdown()
