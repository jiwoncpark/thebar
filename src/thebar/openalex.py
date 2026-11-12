"""Fetch scholarly works from OpenAlex (https://openalex.org) as flat rows.

The app fetches the data, because sandboxes have no network. Set OPENALEX_API_KEY for a
larger daily budget; without it, requests share a small free budget per IP address.
"""

import os

import httpx

API = 'https://api.openalex.org/works'
MAX_WORKS = 2000
FIELDS = ('id,doi,title,publication_year,publication_date,cited_by_count,counts_by_year,primary_topic,'
          'abstract_inverted_index,primary_location,authorships,referenced_works_count,type')


def abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return ''
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return ' '.join(word for _, word in words)


def flatten(work: dict) -> dict:
    topic = work.get('primary_topic') or {}
    source = ((work.get('primary_location') or {}).get('source') or {})
    return {
        'id': work['id'].rsplit('/', 1)[-1],
        'doi': work.get('doi'),
        'title': work.get('title'),
        'year': work.get('publication_year'),
        'date': work.get('publication_date'),
        'type': work.get('type'),
        'venue': source.get('display_name'),
        'field': (topic.get('field') or {}).get('display_name'),
        'subfield': (topic.get('subfield') or {}).get('display_name'),
        'topic': topic.get('display_name'),
        'n_authors': len(work.get('authorships') or []),
        'n_references': work.get('referenced_works_count'),
        'cited_by_count': work.get('cited_by_count'),
        # Citations received in each calendar year (OpenAlex keeps about the last 10 years).
        'citations_by_year': {c['year']: c['cited_by_count'] for c in work.get('counts_by_year') or []},
        'abstract': abstract(work.get('abstract_inverted_index')),
    }


async def fetch_works(search: str | None, filter: str | None, sort: str | None, limit: int) -> tuple[list[dict], int]:
    """Up to `limit` works matching the query, and the total number of matches."""
    params = {'select': FIELDS, 'per-page': 200, 'cursor': '*'}
    if search:
        params['search'] = search
    if filter:
        params['filter'] = filter
    if sort:
        params['sort'] = sort
    if os.environ.get('OPENALEX_API_KEY'):
        params['api_key'] = os.environ['OPENALEX_API_KEY']
    rows, total = [], 0
    async with httpx.AsyncClient(timeout=60) as client:
        while len(rows) < limit and params['cursor']:
            r = await client.get(API, params=params)
            if r.status_code >= 400:
                raise ValueError(f'OpenAlex returned {r.status_code}: {r.text[:300]}')
            data = r.json()
            total = data['meta']['count']
            rows += [flatten(w) for w in data['results']]
            params['cursor'] = data['meta'].get('next_cursor')
            if not data['results']:
                break
    return rows[:limit], total
