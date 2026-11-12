from thebar import openalex


def test_abstract_is_rebuilt_in_order():
    assert openalex.abstract({'world': [1], 'hello': [0, 2]}) == 'hello world hello'
    assert openalex.abstract(None) == ''


def test_flatten():
    work = {
        'id': 'https://openalex.org/W1', 'doi': None, 'title': 'T', 'publication_year': 2020, 'type': 'article',
        'primary_topic': {'display_name': 'Topic', 'field': {'display_name': 'Physics'}, 'subfield': None},
        'primary_location': {'source': None}, 'authorships': [{}, {}], 'cited_by_count': 5,
        'counts_by_year': [{'year': 2021, 'cited_by_count': 3}], 'abstract_inverted_index': {'hi': [0]},
    }
    row = openalex.flatten(work)
    assert row['id'] == 'W1' and row['field'] == 'Physics' and row['subfield'] is None and row['venue'] is None
    assert row['n_authors'] == 2 and row['citations_by_year'] == {2021: 3} and row['abstract'] == 'hi'
