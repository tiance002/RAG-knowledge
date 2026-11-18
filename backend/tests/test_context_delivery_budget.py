import hashlib
from copy import deepcopy

import pytest

from app.services.context_delivery import bound_context_delivery


def member(mid, text, file='file', **extra):
    return dict(id=mid, content=text, content_hash=hashlib.sha256(text.encode()).hexdigest(),
                file_id=file, kb_id='kb', page_number=None, original_index=0,
                parent_id='parent-' + file, file_name='source.txt', **extra)


def row(seed, members):
    return dict(id=seed['id'], file_id=seed['file_id'], kb_id=seed['kb_id'],
                source_mapping_status='VERIFIED', content='\n'.join(m['content'] for m in members),
                original_content=seed['content'], context_group={
                    'seed_evidence_ids': [seed['id']], 'source_members': members})


def delivered(rows):
    return {m['id']: m for r in rows for m in (r.get('context_group') or {}).get('source_members', [])}


def test_all_independent_seeds_reserved_before_optional_and_order_kept():
    a, b = member('a', 'alpha'), member('b', 'beta')
    optional = member('x', 'optional context')
    source = [row(a, [optional, a]), row(b, [b])]
    original = deepcopy(source)
    result = bound_context_delivery(source, {'a': a, 'b': b}, 15)
    assert set(delivered(result)) == {'a', 'b'}
    assert result[0]['context_group']['omitted_optional_ids'] == ['x']
    assert source == original
    assert not result[0]['delivery_budget']['mandatory_overrun']


def test_mandatory_overrun_retains_whole_seed_and_reports_it():
    seed = member('seed', 'header\nUnit: kg\n123.45\n')
    result = bound_context_delivery([row(seed, [seed])], {'seed': seed}, 0)
    assert delivered(result)['seed'] == seed
    assert result[0]['delivery_budget']['mandatory_overrun'] is True
    assert result[0]['context_group']['omitted_seed_ids'] == []


def test_equal_text_in_independent_sources_is_never_collapsed():
    a, b = member('a', 'same bytes', 'source-a'), member('b', 'same bytes', 'source-b')
    result = bound_context_delivery([row(a, [a]), row(b, [b])], {'a': a, 'b': b}, 0)
    assert set(delivered(result)) == {'a', 'b'}
    assert {m['file_id'] for m in delivered(result).values()} == {'source-a', 'source-b'}


@pytest.mark.parametrize('structural', [
    {'content_type': 'table'}, {'heading_text': 'Measurements'},
    {'source_metadata': {'footnotes': ['All values in kg']}},
    {'source_type': 'source_native_table_expansion'},
])
def test_known_headers_units_native_notes_are_mandatory_whole(structural):
    seed = member('seed', 'value')
    structure = member('structure', 'header and units', **structural)
    result = bound_context_delivery([row(seed, [structure, seed])], {'seed': seed}, 0)
    assert delivered(result)['structure'] == structure
    assert result[0]['delivery_budget']['mandatory_overrun']


def test_seed_missing_from_old_clipped_group_is_restored_exactly():
    seed, sibling = member('seed', 'whole original seed'), member('other', 'optional')
    result = bound_context_delivery([row(seed, [sibling])], {'seed': seed}, 0)
    assert delivered(result)['seed']['content'] == seed['content']
    assert result[0]['original_content'] == seed['content']


def test_unknown_mapping_retained_without_fabricating_source():
    unknown = dict(id='unknown', content='unverified context', source_mapping_status='UNKNOWN')
    result = bound_context_delivery([unknown], {}, 0)
    assert result[0]['content'] == unknown['content']
    assert result[0]['source_mapping_status'] == 'UNKNOWN'
    assert 'context_group' not in result[0]
    assert result[0]['delivery_budget_source_status'] == 'UNKNOWN_RETAINED_WHOLE'


@pytest.mark.parametrize('conflict', ['file', 'index', 'locator', 'hash'])
def test_conflicting_identity_or_source_metadata_errors_propagate(conflict):
    seed = member('seed', 'same bytes', source_metadata={'source_sha256': 'a'})
    bad = deepcopy(seed)
    if conflict == 'file':
        bad['file_id'] = 'other-file'
    elif conflict == 'index':
        bad['original_index'] = 2
    elif conflict == 'locator':
        bad['source_metadata']['source_sha256'] = 'b'
    else:
        bad['content'] = 'different bytes'
    with pytest.raises(ValueError, match='CONTEXT_MEMBER_'):
        bound_context_delivery([row(seed, [bad])], {'seed': seed}, 24000)


def test_known_seed_locator_survives_earlier_sibling_representation():
    seed = member('seed', 'whole seed', source_metadata={'source_sha256': 'verified'})
    earlier = deepcopy(seed)
    earlier['source_metadata'] = {}
    result = bound_context_delivery([row(seed, [earlier])], {'seed': seed}, 24000)
    assert delivered(result)['seed']['source_metadata'] == {'source_sha256': 'verified'}
