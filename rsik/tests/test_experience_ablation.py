"""Synthetic transport controls through real OpenAIRoles and projections."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from rsik.analytics.experience_ablation import ARMS, compare_experience_inputs
from rsik.autonomous import history_item
from rsik.autonomy_contract import remember, project_search_memory
from rsik.budgets import Budget
from rsik.handbook.store import Handbook
from rsik.objectives import prompt_objective
from rsik.openai_roles import OpenAIRoles
from rsik.redact import LeakScanner
from rsik.state import canonical, digest
from rsik.tests.test_autonomy_boundary import public
from rsik.tests.test_handbook_and_promotion import delta, entry
from rsik.tests.test_objective_handbook import context, profile, retrieve
from rsik.tests.test_openai_roles import response


@pytest.fixture
def controls(tmp_path):
    goal = profile()
    book = Handbook(tmp_path / 'book')
    book.commit_delta(delta(entry()), context(goal), idempotency_key='control')
    handbook = list(retrieve(book, goal))
    archive = {'schema': 'rsi-autonomy-memory/v1', 'rounds': 0, 'evicted_entries': 0, 'entries': []}
    scope = {'objective_profile': goal, 'compatibility_fingerprint': 'b' * 64,
             'component': 'context_selection'}
    remember(archive, 'a' * 64, 1, history_item(None, 'REJECTED', ['FACT_LOSS'],
        execution_status='COMPLETED', quality_verdict='REJECTED', failure_origin='CANDIDATE'), **scope)
    memory = project_search_memory(archive, **scope)
    base = public()
    base['objective'] = prompt_objective(goal)
    base['compatibility_fingerprint'] = 'b' * 64
    base['parent_code'] += '\n# 中文固定上下文'
    specs = {}
    for arm, (has_memory, has_handbook) in ARMS.items():
        visible = deepcopy(base)
        if has_memory:
            visible['search_memory'] = memory
        if has_handbook:
            visible['handbook_entries'] = handbook
            visible['handbook_entry_ids'] = [item['entry_id'] for item in handbook]
        seen = []
        def transport(body, *_):
            seen.append(body)
            return response({'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC_CONTROL'})
        directory = tmp_path / arm
        client = OpenAIRoles(Budget(directory / 'budget.sqlite'), directory / 'roles',
                             directory / 'unused', LeakScanner(), transport=transport,
                             seam_profile='query-autonomous-v1')
        client.propose('P2_BUDGET', visible, None)
        client.propose('P2_BUDGET', visible, None)
        assert len(seen) == 1  # Same actor/context uses its existing cached receipt.
        path, = client.directory.glob('*.input-receipt.json')
        receipt = json.loads(path.read_bytes())
        specs[arm] = {'receipt_path': path, 'receipt_sha256': sha256(path.read_bytes()).hexdigest(),
                      'wire_sha256': receipt['request_wire_sha256']}
    return tmp_path, specs


def rewrite(spec, mutate):
    path = spec['receipt_path']
    receipt = json.loads(path.read_bytes())
    wire_path = path.parent / receipt['request_wire_file']
    body = json.loads(wire_path.read_bytes())
    mutate(body)
    wire = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    wire_path.write_bytes(wire)
    receipt.update(request_hash=digest(body), request_wire_sha256=sha256(wire).hexdigest(),
                   request_body_bytes=len(wire),
                   user_message_utf8_bytes=len(body['input'][1]['content'].encode('utf-8')))
    public_value = json.loads(body['input'][1]['content'])['public']
    memory = public_value.get('search_memory')
    history = public_value.get('search_history')
    receipt['search_memory_entries_in_request'] = len(memory['entries']) if memory is not None else None
    receipt['search_context_canonical_bytes'] = len(canonical({'search_history': history, 'search_memory': memory}))
    path.write_bytes(canonical(receipt))
    spec.update(receipt_sha256=sha256(path.read_bytes()).hexdigest(), wire_sha256=sha256(wire).hexdigest())


def change_context(body, mutate):
    value = json.loads(body['input'][1]['content'])
    mutate(value)
    body['input'][1]['content'] = canonical(value).decode()


def test_four_exact_utf8_inputs_and_no_payload_disclosure(controls):
    root, specs = controls
    report = compare_experience_inputs(specs, permitted_directory=root)
    assert report['input_control'] == 'VERIFIED'
    assert report['provider_token_usage'] == 'UNKNOWN'
    assert report['model_effect'] == report['answer_quality'] == 'NOT_RUN'
    assert report['rows'][0]['request_body_utf8_bytes_delta'] == 0
    assert all(row['request_body_utf8_bytes_delta'] > 0 for row in report['rows'][1:])
    for row in report['rows']:
        path = specs[row['arm']]['receipt_path']
        receipt = json.loads(path.read_bytes())
        assert row['request_body_utf8_bytes'] == len((path.parent / receipt['request_wire_file']).read_bytes())
    assert '中文固定上下文' not in json.dumps(report, ensure_ascii=False)


def test_tampered_wire_rejected(controls):
    root, specs = controls
    path = specs['combined']['receipt_path']
    receipt = json.loads(path.read_bytes())
    with (path.parent / receipt['request_wire_file']).open('ab') as stream:
        stream.write(b' ')
    with pytest.raises(ValueError, match='ABLATION_WIRE_HASH'):
        compare_experience_inputs(specs, permitted_directory=root)


@pytest.mark.parametrize('fault', ['parent', 'observations', 'instructions', 'model', 'cache'])
def test_resealed_unrelated_change_rejected(controls, fault):
    root, specs = controls
    def mutate(body):
        if fault == 'model':
            body['model'] = 'different-model'
        elif fault == 'cache':
            body['prompt_cache_key'] = 'different-cache'
        else:
            def mutate_context(value):
                if fault == 'instructions':
                    value['objective_instructions'] += ' unrelated'
                elif fault == 'parent':
                    value['public']['parent_code'] += '\n# unrelated'
                else:
                    value['public']['observations'] = []
            change_context(body, mutate_context)
    rewrite(specs['search-memory'], mutate)
    with pytest.raises(ValueError, match='ABLATION_UNRELATED_INPUT_CHANGED'):
        compare_experience_inputs(specs, permitted_directory=root)


def test_truthy_empty_memory_cannot_supply_memory_arm(controls):
    root, specs = controls
    rewrite(specs['search-memory'], lambda body: change_context(body,
        lambda value: value['public']['search_memory'].update(entries=[])))
    with pytest.raises(ValueError, match='ABLATION_EXPERIENCE_PRESENCE'):
        compare_experience_inputs(specs, permitted_directory=root)


def test_paths_and_pinned_receipt_hash(controls):
    root, specs = controls
    with pytest.raises(ValueError, match='ABLATION_RECEIPT_PATH'):
        compare_experience_inputs(specs, permitted_directory=root / 'book')
    spec = specs['control']
    original = spec['receipt_path'].read_bytes()
    spec['receipt_path'].write_bytes(original + b' ')
    with pytest.raises(ValueError, match='ABLATION_RECEIPT_HASH'):
        compare_experience_inputs(specs, permitted_directory=root)


def test_resealed_path_traversal_rejected_without_following(controls):
    root, specs = controls
    spec = specs['control']
    receipt = json.loads(spec['receipt_path'].read_bytes())
    receipt['request_wire_file'] = '../private.json'
    spec['receipt_path'].write_bytes(canonical(receipt))
    spec['receipt_sha256'] = sha256(spec['receipt_path'].read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='ABLATION_ADJACENT_REQUEST_REQUIRED'):
        compare_experience_inputs(specs, permitted_directory=root)
