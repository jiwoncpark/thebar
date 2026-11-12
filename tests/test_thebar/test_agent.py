"""Sandbox and real-model tests. Run with `make smoke` (needs Docker, the sandbox image, and Ollama)."""

import json

import pytest
import yaml

from scripted import scripted_model
from thebar.society import Society


def enable_sandbox(root, network=False):
    path = root / 'config/society.yaml'
    cfg = yaml.safe_load(path.read_text())
    cfg['sandbox']['enabled'] = True
    cfg['sandbox']['network_access'] = network
    path.write_text(yaml.safe_dump(cfg))


SCRIPT = '''import json, socket, os
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3); net = True
except OSError:
    net = False
json.dump({"mean": sum([1, 2, 3]) / 3, "network": net, "uid": os.getuid(),
           "host_repo_visible": os.path.exists("/workspace/config")}, open("results.json", "w"))
print("done")
'''


@pytest.mark.live
async def test_sandbox_tool_and_experiment(root):
    """Coder writes a script in the Docker sandbox, then record_experiment runs it and saves the results."""
    enable_sandbox(root)
    steps = [
        ('shell', {'command': 'mkdir -p scratch/exp1'}),
        ('write_file', {'path': 'scratch/exp1/run.py', 'content': SCRIPT}),
        ('record_experiment', {'directory': 'scratch/exp1', 'hypothesis': 'mean of 1,2,3 is 2', 'references': []}),
    ]
    society = Society(root, model=scripted_model({'ardor': steps, 'ada': []}))
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    activity_id = await society.run_activity(run_id, 'ardor', 'test')
    act = society.db.one('SELECT * FROM activities WHERE id = ?', (activity_id,))
    assert act['status'] == 'completed', act['error']
    results = json.loads((root / 'artifacts/experiments/1/results.json').read_text())
    assert results == {'mean': 2.0, 'network': False, 'uid': 1000, 'host_repo_visible': False}
    assert (root / 'artifacts/experiments/1/run.py').exists()
    assert (root / 'agents/ardor/scratch/exp1/run.py').exists()  # persists on the host for later activities
    await society.shutdown()


TEX = r"""\documentclass{article}
\usepackage[numbers]{natbib}
\title{Means of small samples}
\begin{document}
\maketitle
The mean of 1, 2, 3 is 2 (experiment 1), as is well known \citep{%s}.
\bibliographystyle{plainnat}
\bibliography{refs}
\end{document}
"""
BIB = "@book{fisher1925, author = {Fisher, R. A.}, title = {Statistical Methods for Research Workers}, publisher = {Oliver and Boyd}, year = {1925}}"


@pytest.mark.live
async def test_manuscript_compiles_and_checks_citations(root):
    """write_manuscript compiles LaTeX with BibTeX in the sandbox and refuses citations missing from refs.bib."""
    enable_sandbox(root)
    steps = [
        ('shell', {'command': 'mkdir -p scratch/exp1 scratch/ms'}),
        ('write_file', {'path': 'scratch/exp1/run.py', 'content': SCRIPT}),
        ('record_experiment', {'directory': 'scratch/exp1', 'hypothesis': 'the mean of 1, 2, 3 is 2', 'references': []}),
        ('write_proposal', {'title': 'Means', 'body': '## Summary\nTest the mean.', 'references': []}),
        ('write_file', {'path': 'scratch/ms/refs.bib', 'content': BIB}),
        ('write_file', {'path': 'scratch/ms/paper.tex', 'content': TEX % 'nobody2020'}),
        ('write_manuscript', {'directory': 'scratch/ms', 'proposal_id': 1, 'references': ['experiment:1']}),  # refused
        ('write_file', {'path': 'scratch/ms/paper.tex', 'content': TEX % 'fisher1925'}),
        ('write_manuscript', {'directory': 'scratch/ms', 'proposal_id': 1, 'references': ['experiment:1']}),
    ]
    society = Society(root, model=scripted_model({'ardor': steps, 'ada': []}))
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    activity_id = await society.run_activity(run_id, 'ardor', 'test')
    assert society.db.one('SELECT status, error FROM activities WHERE id = ?', (activity_id,))['status'] == 'completed'
    results = [r['result'] for r in society.db.all("SELECT result FROM tool_calls WHERE tool = 'write_manuscript' ORDER BY id")]
    assert 'nobody2020' in results[0]
    art = society.db.one("SELECT * FROM artifacts WHERE kind = 'manuscript'")
    assert art['title'] == 'Means of small samples'
    assert (root / art['path'] / 'paper.pdf').exists()
    await society.shutdown()


@pytest.mark.live
async def test_real_model_activity(root):
    """One activity with the real local model: it must finish and post an update."""
    enable_sandbox(root)
    society = Society(root)
    await society.start()
    run_id = society.create_run()
    await society.start_run(run_id)
    activity_id = await society.run_activity(run_id, 'ardor', 'test')
    act = society.db.one('SELECT * FROM activities WHERE id = ?', (activity_id,))
    calls = society.db.all('SELECT tool, args, result FROM tool_calls')
    model = society.db.one('SELECT COUNT(*) n, AVG(seconds) s, SUM(output_tokens) t FROM model_calls')
    print(f'\nstatus={act["status"]} error={act["error"]}\nmodel calls={model}\ntools={[c["tool"] for c in calls]}\nupdate={act["public_update"]}')
    assert act['status'] == 'completed', act['error']
    await society.shutdown()
