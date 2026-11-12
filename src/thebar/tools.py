"""Tools the society gives each agent: messaging, papers, insights, proposals, and experiments.

These run in the app (not the sandbox) and validate their inputs.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

from thebar import openalex, papers
from thebar.config import AgentConfig
from thebar.db import now
from thebar.timeline import IMAGE_SUFFIXES

if TYPE_CHECKING:
    from thebar.society import Society

READ_CHARS = 12_000
SHARED_TOOLS = 'agents/shared_tools'  # scripts any agent can use; mounted in every sandbox as shared_tools/
toolset: FunctionToolset[Deps] = FunctionToolset()


@dataclass
class Deps:
    society: Society
    run_id: int
    agent: AgentConfig
    activity_id: int

    @property
    def db(self):
        return self.society.db

    @property
    def root(self) -> Path:
        return self.society.config.root

    def idem(self, ctx: RunContext) -> str:
        """Key that makes a tool call's write happen at most once."""
        return f'{self.activity_id}:{ctx.tool_call_id}'


def _post(ctx: RunContext[Deps], body: str, thread_id: int | None = None, recipient: str | None = None) -> int:
    d = ctx.deps
    d.db.execute(
        'INSERT OR IGNORE INTO messages (run_id, thread_id, author, recipient, body, created_at, idem) VALUES (?,?,?,?,?,?,?)',
        (d.run_id, thread_id, d.agent.id, recipient, body, now(), d.idem(ctx)),
    )
    msg_id = d.db.one('SELECT id FROM messages WHERE idem = ?', (d.idem(ctx),))['id']
    readers = [recipient] if recipient else [a for a in d.society.run_agents(d.run_id) if a != d.agent.id]
    for reader in readers:
        d.db.execute('INSERT OR IGNORE INTO unread VALUES (?,?)', (msg_id, reader))
    owner = 'dm:' + ':'.join(sorted([d.agent.id, recipient])) if recipient else d.agent.id
    d.db.index(f'message:{msg_id}', owner, '', body)
    kind = 'dm' if recipient else 'post'
    d.db.emit(kind, {'message_id': msg_id, 'thread_id': thread_id, 'to': recipient, 'body': body},
              d.run_id, d.agent.id, d.activity_id)
    for reader in readers:
        d.society.wake(d.run_id, reader)
    return msg_id


# ---- Messaging ----

@toolset.tool
def list_threads(ctx: RunContext[Deps]) -> list[dict]:
    """List public discussion threads with their reply counts."""
    return ctx.deps.db.all(
        'SELECT t.id, t.title, t.author, COUNT(m.id) AS messages, MAX(m.created_at) AS last_post FROM threads t '
        'LEFT JOIN messages m ON m.thread_id = t.id WHERE t.run_id = ? GROUP BY t.id ORDER BY last_post DESC LIMIT 30',
        (ctx.deps.run_id,),
    )


@toolset.tool
def read_thread(ctx: RunContext[Deps], thread_id: int) -> dict:
    """Read all messages in a public thread."""
    thread = ctx.deps.db.one('SELECT * FROM threads WHERE id = ? AND run_id = ?', (thread_id, ctx.deps.run_id))
    if not thread:
        raise ModelRetry(f'No thread {thread_id}. Use list_threads.')
    msgs = ctx.deps.db.all('SELECT id, author, body, created_at FROM messages WHERE thread_id = ? ORDER BY id', (thread_id,))
    return {'title': thread['title'], 'messages': msgs}


@toolset.tool
def create_thread(ctx: RunContext[Deps], title: str, body: str) -> str:
    """Start a new public discussion thread visible to every agent."""
    d = ctx.deps
    existing = d.db.one('SELECT thread_id FROM messages WHERE idem = ?', (d.idem(ctx),))
    thread_id = existing['thread_id'] if existing else d.db.insert(
        'INSERT INTO threads (run_id, title, author, created_at) VALUES (?,?,?,?)', (d.run_id, title, d.agent.id, now()))
    msg_id = _post(ctx, body, thread_id=thread_id)
    return f'Created thread {thread_id} (first message is message:{msg_id}).'


@toolset.tool
def post_reply(ctx: RunContext[Deps], thread_id: int, body: str) -> str:
    """Reply in a public thread. Cite earlier work as message:<id>, paper:<id>, insight:<id>, etc."""
    if not ctx.deps.db.one('SELECT id FROM threads WHERE id = ? AND run_id = ?', (thread_id, ctx.deps.run_id)):
        raise ModelRetry(f'No thread {thread_id}. Use list_threads.')
    return f'Posted message:{_post(ctx, body, thread_id=thread_id)}.'


@toolset.tool
def list_dms(ctx: RunContext[Deps]) -> list[dict]:
    """List your private conversations."""
    me = ctx.deps.agent.id
    return ctx.deps.db.all(
        "SELECT CASE WHEN author = ? THEN recipient ELSE author END AS other, COUNT(*) AS messages, MAX(created_at) AS last "
        'FROM messages WHERE run_id = ? AND recipient IS NOT NULL AND (author = ? OR recipient = ?) GROUP BY other',
        (me, ctx.deps.run_id, me, me),
    )


@toolset.tool
def read_dm(ctx: RunContext[Deps], other_agent: str) -> list[dict]:
    """Read your private conversation with another agent."""
    me = ctx.deps.agent.id
    return ctx.deps.db.all(
        'SELECT id, author, body, created_at FROM messages WHERE run_id = ? AND recipient IS NOT NULL '
        'AND ((author = ? AND recipient = ?) OR (author = ? AND recipient = ?)) ORDER BY id',
        (ctx.deps.run_id, me, other_agent, other_agent, me),
    )


@toolset.tool
def send_dm(ctx: RunContext[Deps], to: str, body: str) -> str:
    """Send a private message to another agent. Only you, they, and the human administrator can see it."""
    if to not in ctx.deps.society.run_agents(ctx.deps.run_id) or to == ctx.deps.agent.id:
        raise ModelRetry(f'Unknown agent {to!r}. Agents: {ctx.deps.society.run_agents(ctx.deps.run_id)}')
    return f'Sent message:{_post(ctx, body, recipient=to)}.'


@toolset.tool
def search_messages(ctx: RunContext[Deps], query: str) -> list[dict]:
    """Full-text search over public messages and your own direct messages."""
    me = ctx.deps.agent.id
    hits = ctx.deps.db.search(query, 'message:', limit=20)
    return [h for h in hits if not (h['owner'] or '').startswith('dm:') or me in h['owner'].split(':')][:10]


# ---- Papers ----

@toolset.tool
def list_papers(ctx: RunContext[Deps], collection: str | None = None) -> list[dict]:
    """List known papers. By default lists every collection; pass a collection directory to filter."""
    sql = ('SELECT p.id, p.title, p.authors, p.year, p.collection, p.notes_path IS NOT NULL AS has_notes, r.status AS my_status, '
           'r.last_page AS my_last_page '
           'FROM papers p LEFT JOIN paper_reads r ON r.paper_id = p.id AND r.agent_id = ?')
    params: tuple = (ctx.deps.agent.id,)
    if collection:
        sql += ' WHERE p.collection = ?'
        params += (collection.rstrip('/'),)
    return ctx.deps.db.all(sql + ' ORDER BY p.id', params)


@toolset.tool
def read_paper(ctx: RunContext[Deps], paper_id: int, start_page: int = 1, num_pages: int = 3) -> str:
    """Read up to 5 pages of a paper. Papers can be long: you decide whether to keep reading or stop.

    Before reading a different paper, save notes on this one with save_paper_notes (you can revise them later).
    """
    d = ctx.deps
    pending = d.db.one(
        "SELECT paper_id FROM paper_reads WHERE agent_id = ? AND status = 'notes_pending' AND paper_id != ?",
        (d.agent.id, paper_id),
    )
    if pending:
        raise ModelRetry(f'Save notes for paper:{pending["paper_id"]} with save_paper_notes before reading another paper.')
    paper = d.db.one('SELECT * FROM papers WHERE id = ?', (paper_id,))
    if not paper:
        raise ModelRetry(f'No paper {paper_id}. Use list_papers.')
    path = d.root / paper['path']
    total = papers.page_count(path)
    if not 1 <= start_page <= total:
        raise ModelRetry(f'paper:{paper_id} has pages 1 to {total}.')
    # Whole pages only, up to 5 pages and READ_CHARS characters (always at least one page).
    text, shown = '', 0
    for page in papers.read_pages(path, start_page, max(1, min(num_pages, 5))):
        if shown and len(text) + len(page) > READ_CHARS:
            break
        text, shown = text + page + '\n\n', shown + 1
    end = start_page + shown - 1
    d.db.execute(
        "INSERT INTO paper_reads (paper_id, agent_id, status, last_page) VALUES (?, ?, 'notes_pending', ?) "
        'ON CONFLICT DO UPDATE SET last_page = MAX(COALESCE(last_page, 0), excluded.last_page)',
        (paper_id, d.agent.id, end),
    )
    d.db.emit('paper_read', {'paper_id': paper_id, 'title': paper['title'], 'pages': [start_page, end], 'total': total},
              d.run_id, d.agent.id, d.activity_id)
    if end < total:
        after = (f'You have read pages {start_page}-{end} of {total}. Decide whether you have read enough for your purposes. '
                 f'To continue, call read_paper(paper_id={paper_id}, start_page={end + 1}). '
                 'Otherwise save notes on what you read; you can revise them after reading more later.')
    else:
        after = 'This is the end of the paper.'
    return f'{paper["title"]} (paper:{paper_id}), pages {start_page}-{end} of {total}\n\n{text[:READ_CHARS]}\n{after}'


NOTE_SECTIONS = ['Citation', 'Research question', 'Main contribution', 'Method and assumptions', 'Results',
                 'Critical assessment', 'Connection to our research question', 'Connections to other work']


@toolset.tool
def save_paper_notes(ctx: RunContext[Deps], paper_id: int, notes: str) -> str:
    """Save reading notes for a paper (replacing earlier notes). Include page or section numbers where you can.

    `notes` is markdown with these level-2 headings, in order: ## Citation, ## Research question,
    ## Main contribution, ## Method and assumptions, ## Results, ## Critical assessment,
    ## Connection to our research question, ## Connections to other work.
    """
    d = ctx.deps
    paper = d.db.one('SELECT * FROM papers WHERE id = ?', (paper_id,))
    if not paper:
        raise ModelRetry(f'No paper {paper_id}.')
    missing = [h for h in NOTE_SECTIONS if f'## {h}'.lower() not in notes.lower()]
    if missing:
        raise ModelRetry('Notes are missing these headings: ' + ', '.join(f'## {h}' for h in missing))
    body = f'# Notes: {paper["title"]}\n\npaper:{paper_id} · written by {d.agent.id} · {now()}\n\n{notes.strip()}\n'
    notes = d.root / paper['collection'] / f'notes_{papers.slug(paper["title"])}.md'
    notes.write_text(body)
    rel = str(notes.relative_to(d.root))
    d.db.execute('UPDATE papers SET notes_path = ? WHERE id = ?', (rel, paper_id))
    d.db.execute("INSERT INTO paper_reads (paper_id, agent_id, status) VALUES (?, ?, 'completed') "
                 "ON CONFLICT DO UPDATE SET status = 'completed'", (paper_id, d.agent.id))
    d.db.index(f'notes:{paper_id}', d.agent.id, paper['title'], body)
    d.db.emit('paper_notes', {'paper_id': paper_id, 'path': rel}, d.run_id, d.agent.id, d.activity_id)
    return f'Saved notes to {rel}.'


@toolset.tool
def search_paper_notes(ctx: RunContext[Deps], query: str) -> list[dict]:
    """Full-text search over all saved paper notes and paper first pages."""
    return ctx.deps.db.search(query, 'notes:') + ctx.deps.db.search(query, 'paper:', limit=5)


@toolset.tool
async def search_papers(ctx: RunContext[Deps], query: str) -> list[dict]:
    """Search arXiv for papers. Use download_paper to add one to your collection."""
    return await papers.search_arxiv(query)


@toolset.tool
async def download_paper(ctx: RunContext[Deps], arxiv_id: str | None = None, pdf_url: str | None = None) -> str:
    """Download a paper into your collection, by arXiv id (e.g. '2305.12345') or a public https PDF link."""
    d = ctx.deps
    if arxiv_id:
        found = d.db.one('SELECT id FROM papers WHERE arxiv_id = ?', (arxiv_id,))
        if found:
            return f'Already have it: paper:{found["id"]}.'
        found = await papers.search_arxiv('', id_list=arxiv_id)
        meta = {k: found[0][k] for k in ('title', 'authors', 'year')} if found else {}
        pdf_url = f'https://arxiv.org/pdf/{arxiv_id}'
    elif not pdf_url:
        raise ModelRetry('Give arxiv_id or pdf_url.')
    else:
        meta = {}
    try:
        data = await papers.download(pdf_url)
    except Exception as e:
        raise ModelRetry(f'Download failed: {e}')
    directory = d.root / d.agent.papers_dir
    path = directory / f'{papers.slug(arxiv_id or Path(pdf_url).stem)}.pdf'
    if not path.exists():
        path.write_bytes(data)
    paper_id = papers.add_paper(d.db, path, d.agent.papers_dir, d.root, d.agent.id, arxiv_id=arxiv_id, url=pdf_url, **meta)
    title = d.db.one('SELECT title FROM papers WHERE id = ?', (paper_id,))['title']
    d.db.emit('paper_downloaded', {'paper_id': paper_id, 'title': title, 'url': pdf_url}, d.run_id, d.agent.id, d.activity_id)
    return f'Saved as paper:{paper_id} ({title}).'


# ---- Insights and proposals ----

SUMMARY_HEADING = '## Summary'


def _save_artifact(ctx, kind, title, body, references, artifact_id=None, base_revision=None, fork_of=None) -> str:
    d = ctx.deps
    if kind != 'manuscript' and not body.lstrip().startswith(SUMMARY_HEADING):
        raise ModelRetry(
            f'Start the body with "{SUMMARY_HEADING}": 2-4 sentences in plain words, with no symbols or acronyms, that a '
            'researcher outside this project understands without reading anything else: the claim, the main evidence, '
            'and why it matters for the research question.'
        )
    refs = json.dumps(references)
    done = d.db.one('SELECT artifact_id, revision FROM artifact_versions WHERE idem = ?', (d.idem(ctx),))
    if done:
        return f'Saved {kind}:{done["artifact_id"]} revision {done["revision"]}.'
    if artifact_id is None:
        folder = {'insight': f'artifacts/insights/{d.agent.id}', 'proposal': 'proposals', 'manuscript': 'artifacts/manuscripts'}[kind]
        suffix = '' if kind == 'manuscript' else '.md'  # a manuscript is a folder with paper.tex, refs.bib, and paper.pdf
        path = f'{folder}/{papers.slug(title)}{suffix}'
        if d.db.one('SELECT id FROM artifacts WHERE path = ?', (path,)):
            path = f'{folder}/{papers.slug(title)}_{d.agent.id}_{d.activity_id}{suffix}'
        artifact_id = d.db.insert(
            'INSERT INTO artifacts (kind, author, title, path, revision, forked_from, run_id, created_at) VALUES (?,?,?,?,0,?,?,?)',
            (kind, d.agent.id, title, path, fork_of, d.run_id, now()),
        )
    art = d.db.one('SELECT * FROM artifacts WHERE id = ? AND kind = ?', (artifact_id, kind))
    if not art:
        raise ModelRetry(f'No {kind} {artifact_id}.')
    if kind == 'insight' and art['author'] != d.agent.id:
        raise ModelRetry('You can only revise your own insights. Write a new insight that cites it instead.')
    if art['revision'] > 0 and base_revision != art['revision']:
        raise ModelRetry(
            f'{kind}:{artifact_id} is at revision {art["revision"]}, not {base_revision}. '
            f'Read it with read_reference("{kind}:{artifact_id}") and revise from the current text.'
        )
    revision = art['revision'] + 1
    authors = sorted({r['author'] for r in d.db.all('SELECT author FROM artifact_versions WHERE artifact_id = ?', (artifact_id,))} | {d.agent.id})
    header = {'id': f'{kind}:{artifact_id}', 'title': title, 'authors': authors, 'revision': revision,
              'updated_at': now(), 'references': references}
    if art['forked_from']:
        header['forked_from'] = f'{kind}:{art["forked_from"]}'
    if kind != 'manuscript':  # write_manuscript copies the manuscript's folder itself
        text = f'---\n{yaml.safe_dump(header, sort_keys=False)}---\n\n{body}\n'
        path = d.root / art['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    d.db.execute('INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?)', (artifact_id, revision, d.agent.id, body, refs, now(), d.idem(ctx)))
    d.db.execute('UPDATE artifacts SET revision = ?, title = ? WHERE id = ?', (revision, title, artifact_id))
    d.db.index(f'{kind}:{artifact_id}', d.agent.id, title, body)
    d.db.emit(kind, {'id': artifact_id, 'title': title, 'revision': revision, 'path': art['path']}, d.run_id, d.agent.id, d.activity_id)
    return f'Saved {kind}:{artifact_id} revision {revision} at {art["path"]}.'


@toolset.tool
def save_insight(ctx: RunContext[Deps], title: str, body: str, references: list[str], insight_id: int | None = None,
                 base_revision: int | None = None) -> str:
    """Save an insight: a general claim about the research question, such as a hypothesis, connection, criticism,
    finding, negative result, or new question, that a researcher outside this project would find worth knowing.

    The title states the general claim in plain words, not the result of one experiment. The body starts with
    "## Summary": 2-4 plain sentences, no symbols or acronyms, readable without anything else. Then cover the claim,
    motivation, evidence (cite experiments and papers), uncertainty, limitations, and implications, defining any
    symbol you use.
    references: e.g. ["paper:2", "message:7"], or [] for a new conjecture.
    To update one of your insights, pass insight_id and its current revision as base_revision.
    """
    return _save_artifact(ctx, 'insight', title, body, references, insight_id, base_revision)


@toolset.tool
def write_proposal(ctx: RunContext[Deps], title: str, body: str, references: list[str], proposal_id: int | None = None,
                   base_revision: int | None = None, fork_of: int | None = None) -> str:
    """Create, revise, or fork a research proposal. Any agent may revise any proposal.

    The body starts with "## Summary": 2-4 plain sentences, no symbols or acronyms, readable without anything else.
    Then these sections: Research question, Motivation, Hypothesis or proposed method,
    Computational experiment, Dataset / simulation environment, Baselines, Evaluation metrics,
    What would falsify the hypothesis?, Expected implications, Limitations and open questions.
    To revise, pass proposal_id and its current revision as base_revision. To fork, pass fork_of.
    """
    return _save_artifact(ctx, 'proposal', title, body, references, proposal_id, base_revision, fork_of)


@toolset.tool
def list_artifacts(ctx: RunContext[Deps], kind: str | None = None) -> list[dict]:
    """List insights, proposals, and manuscripts (kind='insight', 'proposal', or 'manuscript' to filter)."""
    sql = 'SELECT kind, id, title, author, revision FROM artifacts'
    return ctx.deps.db.all(sql + (' WHERE kind = ?' if kind else '') + ' ORDER BY id DESC LIMIT 40', (kind,) if kind else ())


@toolset.tool
def read_reference(ctx: RunContext[Deps], ref: str) -> str:
    """Read anything by reference: paper:<id>, insight:<id>, proposal:<id>, proposal:<id>@<revision>, message:<id>, experiment:<id>."""
    try:
        return resolve(ctx.deps.society, ref, ctx.deps.agent.id)
    except (KeyError, ValueError) as e:
        raise ModelRetry(str(e))


def resolve(society: Society, ref: str, viewer: str | None = None) -> str:
    """Return the text a reference points to. viewer=None means the human administrator (sees everything)."""
    db, root = society.db, society.config.root
    kind, _, rest = ref.partition(':')
    ident, _, rev = rest.partition('@')
    if not ident.isdigit():
        raise ValueError(f'Bad reference {ref!r}.')
    i = int(ident)
    if kind == 'paper':
        p = db.one('SELECT * FROM papers WHERE id = ?', (i,))
        if not p:
            raise KeyError(f'No {ref}.')
        notes = (root / p['notes_path']).read_text() if p['notes_path'] else '(no notes yet)'
        return f'{p["title"]}\n{p["authors"]} {p["year"]} arXiv:{p["arxiv_id"]} doi:{p["doi"]}\n{p["path"]}\n\n{notes}'
    if kind in ('insight', 'proposal', 'manuscript'):
        a = db.one('SELECT * FROM artifacts WHERE id = ? AND kind = ?', (i, kind))
        if not a:
            raise KeyError(f'No {ref}.')
        v = db.one('SELECT * FROM artifact_versions WHERE artifact_id = ? AND revision = ?', (i, int(rev) if rev else a['revision']))
        if not v:
            raise KeyError(f'No revision {rev} of {kind}:{i}.')
        return f'{kind}:{i} "{a["title"]}" revision {v["revision"]} (latest {a["revision"]}) by {v["author"]}\nreferences: {v["refs"]}\n\n{v["body"]}'
    if kind == 'message':
        m = db.one('SELECT * FROM messages WHERE id = ?', (i,))
        if not m or (viewer and m['recipient'] and viewer not in (m['author'], m['recipient'])):
            raise KeyError(f'No {ref}.')
        return f'{m["author"]} at {m["created_at"]}:\n{m["body"]}'
    if kind == 'experiment':
        e = db.one('SELECT * FROM experiments WHERE id = ?', (i,))
        if not e:
            raise KeyError(f'No {ref}.')
        folder = root / e['path']
        results = (folder / 'results.json').read_text()[:4000]
        return f'experiment:{i} by {e["author"]}, exit code {e["exit_code"]}\nHypothesis: {e["hypothesis"]}\n{e["path"]}\n\nresults.json:\n{results}'
    raise ValueError(f'Unknown reference type in {ref!r}.')


# ---- Data ----

@toolset.tool
async def fetch_openalex_works(
    ctx: RunContext[Deps],
    save_as: str,
    search: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    max_results: int = 200,
) -> str:
    """Download scholarly works from OpenAlex into your sandbox as a JSON Lines file, for experiments.

    Each row has: id, doi, title, year, date, type, venue, field, subfield, topic, n_authors,
    n_references, cited_by_count, citations_by_year ({year: count}, about the last 10 years), abstract.
    The file is written to scratch/data/<save_as>.jsonl, where your sandbox code can read it.

    search: full-text search, e.g. "hypothesis generation".
    filter: OpenAlex filter syntax, comma-separated, e.g. "publication_year:2019,type:article,primary_topic.field.id:17"
        (field ids: 17 Computer Science, 26 Mathematics, 13 Biochemistry, 31 Physics, 27 Medicine),
        or "cited_by_count:>10", "from_publication_date:2018-01-01".
    sort: e.g. "cited_by_count:desc" or "publication_date:asc". Without a sort, results are by relevance or id.
    max_results: up to 2000.
    """
    d = ctx.deps
    name = papers.slug(save_as)
    try:
        rows, total = await openalex.fetch_works(search, filter, sort, max(1, min(max_results, openalex.MAX_WORKS)))
    except (ValueError, httpx.HTTPError) as e:
        raise ModelRetry(f'OpenAlex request failed: {e}')
    folder = d.root / d.agent.workspace / 'scratch' / 'data'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    d.db.emit('data_fetched', {'source': 'openalex', 'search': search, 'filter': filter, 'rows': len(rows),
                               'path': f'scratch/data/{name}.jsonl'}, d.run_id, d.agent.id, d.activity_id)
    sample = '\n'.join(f'- {r["year"]} | {r["cited_by_count"]} citations | {r["title"]}' for r in rows[:5])
    return (f'Saved {len(rows)} of {total} matching works to scratch/data/{name}.jsonl.\n'
            f'First rows:\n{sample}')


# ---- Experiments and tools ----

def _scratch_folder(d: Deps, directory: str) -> tuple[Path, str]:
    """A folder under the agent's scratch/, as a host path and as a path relative to scratch/."""
    host_scratch = (d.root / d.agent.workspace / 'scratch').resolve()
    rel = directory.strip('/').removeprefix('workspace/').removeprefix('scratch/').strip('/')
    host_dir = (host_scratch / rel).resolve()
    if not host_dir.is_relative_to(host_scratch) or not host_dir.is_dir():
        raise ModelRetry(f'Not a folder under scratch/: {directory}')
    return host_dir, rel


@toolset.tool
async def record_experiment(ctx: RunContext[Deps], directory: str, hypothesis: str, references: list[str]) -> str:
    """Run and record a computational experiment.

    directory: a folder under scratch/ in your sandbox containing run.py (and optionally requirements.txt).
    hypothesis: what the experiment tests, in plain words that someone outside this project understands.
    run.py is executed in the sandbox and must write results.json. Each plot it saves (e.g. plot.png) needs a caption
    in a .txt file of the same name (plot.txt). The code, results, plots, and output are then saved as a permanent
    experiment record.
    """
    d = ctx.deps
    done = d.db.one('SELECT id FROM experiments WHERE idem = ?', (d.idem(ctx),))
    if done:
        return f'Recorded experiment:{done["id"]}.'
    host_dir, rel = _scratch_folder(d, directory)
    if not (host_dir / 'run.py').exists():
        raise ModelRetry(f'Put run.py in a folder under scratch/, e.g. scratch/exp1/run.py. Not found: {directory}')
    if not d.society.config.society['sandbox']['enabled']:
        raise ModelRetry('The sandbox is disabled, so experiments cannot be run.')
    (host_dir / 'results.json').unlink(missing_ok=True)
    timeout = d.society.config.society['sandbox']['command_timeout_seconds']
    result = await ctx.workspace.run(f'cd scratch/{rel} && python run.py', shell=True, timeout=timeout)
    if not (host_dir / 'results.json').exists():
        raise ModelRetry(
            f'run.py exited with code {result.exit_code} and did not write results.json.\n'
            f'stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}'
        )
    uncaptioned = [f.name for f in host_dir.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES and not f.with_suffix('.txt').exists()]
    if uncaptioned:
        raise ModelRetry(
            f'These plots have no caption: {", ".join(uncaptioned)}. Have run.py write a caption for each plot to a .txt '
            'file with the same name (plot.png -> plot.txt), then call record_experiment again.'
        )
    exp_id = d.db.insert(
        'INSERT INTO experiments (run_id, author, activity_id, hypothesis, path, exit_code, refs, created_at, idem) VALUES (?,?,?,?,?,?,?,?,?)',
        (d.run_id, d.agent.id, d.activity_id, hypothesis, '', result.exit_code, json.dumps(references), now(), d.idem(ctx)),
    )
    dest = d.root / 'artifacts' / 'experiments' / str(exp_id)
    shutil.copytree(host_dir, dest, dirs_exist_ok=True, ignore=lambda _, names: [n for n in names if n.startswith('.')])
    (dest / 'experiment.yaml').write_text(yaml.safe_dump({
        'id': f'experiment:{exp_id}', 'author': d.agent.id, 'activity': d.activity_id, 'run': d.run_id,
        'hypothesis': hypothesis, 'references': references, 'source': f'{d.agent.workspace}/scratch/{rel}',
        'exit_code': result.exit_code, 'created_at': now(),
    }, sort_keys=False))
    (dest / 'output.txt').write_text(f'$ python run.py\nexit code {result.exit_code}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}\n')
    d.db.execute('UPDATE experiments SET path = ? WHERE id = ?', (str(dest.relative_to(d.root)), exp_id))
    results = (dest / 'results.json').read_text()
    d.db.emit('experiment', {'id': exp_id, 'hypothesis': hypothesis, 'exit_code': result.exit_code, 'results': results[:2000]},
              d.run_id, d.agent.id, d.activity_id)
    return f'Recorded experiment:{exp_id} (exit code {result.exit_code}).\nresults.json:\n{results[:3000]}'


@toolset.tool
def list_tools(ctx: RunContext[Deps]) -> list[dict]:
    """List reusable scripts: your own (tools/) and those any agent shared (shared_tools/), with their first line."""
    found = []
    for folder, name in [(ctx.deps.root / ctx.deps.agent.workspace / 'tools', 'tools'), (ctx.deps.root / SHARED_TOOLS, 'shared_tools')]:
        if folder.exists():
            found += [{'path': f'{name}/{p.relative_to(folder)}', 'description': (p.read_text(errors='replace').splitlines() or [''])[0][:200]}
                      for p in sorted(folder.rglob('*')) if p.is_file()]
    return found


# ---- Manuscripts ----

@toolset.tool
async def write_manuscript(ctx: RunContext[Deps], directory: str, proposal_id: int, references: list[str],
                           manuscript_id: int | None = None, base_revision: int | None = None) -> str:
    """Write or revise a short paper, in LaTeX, on a proposal whose main hypothesis has been tested by recorded experiments.

    directory: a folder under scratch/ with paper.tex, refs.bib, and the figures paper.tex includes. Cite with
    \\citep{key} or \\citet{key} using keys from refs.bib, and end with \\bibliography{refs}. The folder is compiled
    with tectonic in your sandbox and saved, with paper.pdf, as manuscript:<id>.
    references: the experiments, insights, and papers it relies on, with at least one experiment, e.g. ["experiment:12"].
    To revise a manuscript (any agent may), pass manuscript_id and its current revision as base_revision.
    """
    d = ctx.deps
    done = d.db.one('SELECT artifact_id, revision FROM artifact_versions WHERE idem = ?', (d.idem(ctx),))
    if done:
        return f'Saved manuscript:{done["artifact_id"]} revision {done["revision"]}.'
    host_dir, rel = _scratch_folder(d, directory)
    if not (host_dir / 'paper.tex').exists() or not (host_dir / 'refs.bib').exists():
        raise ModelRetry(f'Put paper.tex and refs.bib in scratch/{rel}.')
    if not d.db.one("SELECT id FROM artifacts WHERE id = ? AND kind = 'proposal'", (proposal_id,)):
        raise ModelRetry(f'No proposal:{proposal_id}.')
    experiments = [r for r in references if r.startswith('experiment:')]
    if not experiments or any(not d.db.one('SELECT id FROM experiments WHERE id = ?', (r.split(':')[1],)) for r in experiments):
        raise ModelRetry('references must include the recorded experiments the manuscript reports, e.g. ["experiment:12"].')
    tex = (host_dir / 'paper.tex').read_text()
    title = re.search(r'\\title\{(.+?)\}', tex, re.S)
    if not title:
        raise ModelRetry('paper.tex needs a \\title{...}.')
    if '\\cite' not in tex:
        raise ModelRetry('Cite your sources with \\citep{key} or \\citet{key}, using entries in refs.bib.')
    if not d.society.config.society['sandbox']['enabled']:
        raise ModelRetry('The sandbox is disabled, so manuscripts cannot be compiled.')
    (host_dir / 'paper.pdf').unlink(missing_ok=True)
    timeout = d.society.config.society['sandbox']['command_timeout_seconds']
    result = await ctx.workspace.run(f'cd scratch/{rel} && tectonic --keep-logs paper.tex', shell=True, timeout=timeout)
    if not (host_dir / 'paper.pdf').exists():
        raise ModelRetry(f'paper.tex did not compile (exit code {result.exit_code}).\n{(result.stdout + result.stderr)[-3000:]}')
    log = (host_dir / 'paper.log').read_text(errors='replace') if (host_dir / 'paper.log').exists() else ''
    undefined = sorted(set(re.findall(r"Citation `([^']+)'[^\n]*undefined", log)))
    if undefined:
        raise ModelRetry(f'These citations are not in refs.bib: {", ".join(undefined)}. Add entries for them, with real bibliographic details.')
    message = _save_artifact(ctx, 'manuscript', ' '.join(title.group(1).split()), tex,
                             references + [f'proposal:{proposal_id}'], manuscript_id, base_revision)
    art = d.db.one('SELECT a.id, a.path FROM artifacts a JOIN artifact_versions v ON v.artifact_id = a.id WHERE v.idem = ?', (d.idem(ctx),))
    dest = d.root / art['path']
    shutil.copytree(host_dir, dest, dirs_exist_ok=True, ignore=lambda _, names: [n for n in names if n.startswith('.')])
    return f'{message} PDF: {art["path"]}/paper.pdf'
