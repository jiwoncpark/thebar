"""Paper indexing, PDF text extraction, arXiv search, and downloads."""

import hashlib
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from thebar.db import DB, now

MAX_DOWNLOAD_BYTES = 30_000_000
ARXIV_RE = re.compile(r'arXiv:\s*(\d{4}\.\d{4,5})', re.I)
DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)')


def slug(text: str, limit: int = 60) -> str:
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')[:limit] or 'untitled'


def page_count(path: Path) -> int:
    return len(PdfReader(path).pages)


def read_pages(path: Path, start: int, count: int) -> list[str]:
    """Text of pages start..start+count-1 (1-based), one string per page."""
    reader = PdfReader(path)
    pages = reader.pages[start - 1 : start - 1 + count]
    return [f'--- page {start + i} ---\n{p.extract_text() or ""}' for i, p in enumerate(pages)]


def add_paper(db: DB, path: Path, collection: str, root: Path, added_by: str | None = None, **meta) -> int:
    """Register a PDF, extracting what metadata we can. Returns the paper id (existing one if a duplicate)."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = db.one('SELECT id FROM papers WHERE sha256 = ? OR path = ?', (sha, str(path.relative_to(root))))
    if existing:
        return existing['id']
    reader = PdfReader(path)
    info = reader.metadata or {}
    first = (reader.pages[0].extract_text() or '') if reader.pages else ''
    arxiv = ARXIV_RE.search(first)
    doi = DOI_RE.search(first)
    title = meta.get('title') or (info.get('/Title') or '').strip() or path.stem
    paper_id = db.insert(
        'INSERT INTO papers (path, collection, title, authors, year, arxiv_id, doi, url, sha256, added_by, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (
            str(path.relative_to(root)), collection, title,
            meta.get('authors') or (info.get('/Author') or ''),
            meta.get('year') or (info.get('/CreationDate') or '')[2:6],
            meta.get('arxiv_id') or (arxiv.group(1) if arxiv else None),
            doi.group(1) if doi else None, meta.get('url'), sha, added_by, now(),
        ),
    )
    db.index(f'paper:{paper_id}', None, title, first[:4000])
    return paper_id


def index_collection(db: DB, root: Path, papers_dir: str) -> None:
    """Index every PDF in a collection directory. Files are not modified."""
    directory = root / papers_dir
    directory.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(directory.glob('*.pdf')):
        add_paper(db, pdf, papers_dir, root)
    # Pick up notes that already exist on disk.
    for paper in db.all('SELECT id, title FROM papers WHERE collection = ? AND notes_path IS NULL', (papers_dir,)):
        notes = directory / f'notes_{slug(paper["title"])}.md'
        if notes.exists():
            db.execute('UPDATE papers SET notes_path = ? WHERE id = ?', (str(notes.relative_to(root)), paper['id']))


async def search_arxiv(query: str, limit: int = 5, id_list: str = '') -> list[dict]:
    params = {'id_list': id_list} if id_list else {'search_query': f'all:{query}', 'max_results': limit}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get('https://export.arxiv.org/api/query', params=params)
        r.raise_for_status()
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    results = []
    for entry in ET.fromstring(r.text).findall('a:entry', ns):
        arxiv_id = entry.findtext('a:id', '', ns).rsplit('/abs/', 1)[-1]
        results.append({
            'arxiv_id': re.sub(r'v\d+$', '', arxiv_id),
            'title': ' '.join(entry.findtext('a:title', '', ns).split()),
            'authors': ', '.join(a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)),
            'year': entry.findtext('a:published', '', ns)[:4],
            'summary': ' '.join(entry.findtext('a:summary', '', ns).split())[:600],
        })
    return results


def _public_https(url: str) -> bool:
    """Only allow https URLs that resolve to public addresses (not this machine or the local network)."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, 443)}
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(a).is_global for a in addresses)


async def download(url: str) -> bytes:
    if not _public_https(url):
        raise ValueError('Only public https URLs are allowed.')
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async with client.stream('GET', url) as r:
            r.raise_for_status()
            data = b''
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError('File is larger than the 30 MB limit.')
    if not data.startswith(b'%PDF'):
        raise ValueError('The URL did not return a PDF.')
    return data
