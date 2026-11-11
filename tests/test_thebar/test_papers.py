import pytest

from thebar import papers
from thebar.db import DB


def test_index_is_idempotent(root):
    db = DB(':memory:')
    papers.index_collection(db, root, 'data/papers/post_bayesian')
    papers.index_collection(db, root, 'data/papers/post_bayesian')
    rows = db.all('SELECT * FROM papers')
    assert len(rows) == 1 and rows[0]['sha256']
    assert 'page 1' in papers.read_pages(root / rows[0]['path'], 1, 1)


def test_slug():
    assert papers.slug('The Era of Experience: A Paper!') == 'the_era_of_experience_a_paper'


@pytest.mark.parametrize('url', ['http://arxiv.org/pdf/1', 'https://localhost/x.pdf', 'https://127.0.0.1/x.pdf', 'file:///etc/passwd'])
async def test_download_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        await papers.download(url)
