"""Paired, deterministic evidence through the actual candidate coordinator."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from rsik import query_workflow as q
from rsik.adjudicate import SLOTS, evaluate_candidate
from rsik.collaboration import PROFILES, freeze_pair, run_offline_pair
from rsik.analytics.reader import read_event_log
from rsik.analytics.projections import project_sources
from rsik.analytics.metrics import compute_metrics
from rsik.errors import StageFailure
from rsik.family_decision import make_family
from rsik.query_aware import adapt_legacy, selector
from rsik.role_profiles import roles_for
from rsik.state import canonical, digest
from rsik.tests.test_handbook_and_promotion import entry, delta, evidence_context
from rsik.tests.test_workflow import FakeRoles, fake_rows, public_context

ROOT = Path(__file__).resolve().parents[2]
ABSTAIN = {'status': 'ABSTAIN', 'reason': 'SYNTHETIC', 'result': None}


class Capsule:
    business_root = 'SYNTHETIC'
    def setup(self): pass
    def close(self): pass
    def child(self, stage, args, directory, **kwargs):
        Path(args[-1]).write_bytes(canonical({'status': 'PASS'}))
        return {'status': 'PASS'}


def fixture(tmp_path, monkeypatch, control=None):
    worker = adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    public = public_context()
    public.update(parent_code=selector(worker.decode())[1], experiment_intent='STRUCTURAL_POLICY_SEARCH')
    paths = []
    for i in range(4):
        p = tmp_path / f'fixture-{i}'
        p.mkdir()
        (p/'synthetic.json').write_bytes(canonical({'suite': i, 'rows': fake_rows()}))
        paths.append(p)
    fake = FakeRoles(same_patch=True)
    seen, calls, prompts = [], [], {}
    def response(profile, slot, context, body):
        seen.append((profile, slot))
        prompts.setdefault((profile, slot), []).append(body['input'][0]['content'])
        visible = context['public']
        assert not {'gold','cases','expected_answers','candidate_decisions'} & set(visible)
        if slot == 'diagnose':
            return fake.diagnose(visible)
        if slot in ('curator', 'meta'):
            return deepcopy(ABSTAIN)
        if control == 'abstain' and slot == SLOTS[0]:
            return deepcopy(ABSTAIN)
        if control == 'invalid' and slot == SLOTS[0]:
            return {'bad': 'invalid envelope'}
        value = fake.propose(slot, visible, context['diagnosis'])
        entry = value['result']
        entry['angle'] = roles_for(profile)[slot]
        tail = '    return list(selected)' if slot != SLOTS[2] else '    return sorted(selected)'
        if control == 'ast' and slot == SLOTS[0]:
            tail = '    return eval(query)'
        entry['worker_patch']['new'] = tail
        return value
    def check_split(path, *, expected_count):
        rows = json.loads((path/'synthetic.json').read_bytes())['rows']
        assert len(rows) == expected_count
        return {'data_sha256': q.bytes_hash((path/'synthetic.json').read_bytes())}
    def replay(capsule, path, artifacts, out, **kwargs):
        calls.append(out)
        if control == 'infra' and out.parent.name == SLOTS[0]:
            raise StageFailure('REPLAY_ERROR','SYNTHETIC_INFRA_FAILURE')
        rows = json.loads((path/'synthetic.json').read_bytes())['rows']
        out.mkdir()
        (out/'manifest.json').write_bytes(canonical({'arms': {
            key: {'candidate_id': item['candidate_id'], 'worker_sha256': q.bytes_hash(item['worker']),
                  'sha256': q.bytes_hash(item['config'])} for key, item in artifacts.items()}}))
        (out/'results.json').write_bytes(canonical({'rows': rows}))
        decision = evaluate_candidate({'angle': 'P2_BUDGET', 'proposal_id': 'synthetic',
            'candidate_id': artifacts['B']['candidate_id'],
            'worker_hash': q.bytes_hash(artifacts['B']['worker']), 'config_hash': q.bytes_hash(artifacts['B']['config'])},
            rows, [r['qid'] for r in rows], total_questions=20)
        decision.update(evidence_mode='SYNTHETIC_CONTROL', real_eligible=False)
        return decision
    monkeypatch.setattr(q, 'check_split', check_split)
    monkeypatch.setattr(q, 'replay_artifacts', replay)
    fingerprint = digest('paired-memory/v1')
    body, evidence = entry(), evidence_context(True)
    body['compatibility_fingerprint'] = evidence['compatibility_fingerprint'] = fingerprint
    args = dict(memory=[(delta(body), evidence)], memory_features={'same_source': True},
                worker=worker, config=b'{"selection":"all"}', public=public, fixtures=paths,
                slot_models={s:'gpt-6-luna' for s in ('diagnose', *SLOTS, 'curator', 'meta')},
                budget_usd='0.20')
    return args, response, calls, seen, prompts


def run(tmp_path, monkeypatch, control=None):
    args, response, calls, seen, prompts = fixture(tmp_path, monkeypatch, control)
    root = tmp_path/'pair'
    frozen = freeze_pair(**args)
    report = run_offline_pair(directory=root, frozen=frozen, response_script=response,
                              capsule_factory=lambda _: Capsule(), **args)
    return report, root, frozen, calls, seen, prompts


def test_full_paired_collaboration(tmp_path, monkeypatch):
    report, root, frozen, calls, seen, prompts = run(tmp_path, monkeypatch)
    assert report['status'] == 'SYNTHETIC_ONLY' and report['real_model_calls'] == 0
    assert report['paired_input_matched']
    assert len(calls) == 24 and len(seen) == 12
    assert len({frozen['profiles'][p]['sha256'] for p in PROFILES}) == 2
    assert len({prompts[PROFILES[0], s][0] for s in SLOTS}) == 3
    assert len({prompts[PROFILES[1], s][0] for s in SLOTS}) == 1
    for profile in PROFILES:
        a = report['profiles'][profile]
        assert a['proposal_schema_valid'] == a['proposal_static_pass'] == {'numerator':3, 'denominator':3}
        assert a['worker_ast_diversity'] == {'numerator':2, 'denominator':3}
        assert a['worker_ast_duplicates'] == {'numerator':1, 'denominator':3}
        assert a['independent_proposal_instances'] == 3
        assert a['replay_completed'] == {'numerator':3, 'denominator':3}
        assert a['training_adoption']['denominator'] == 3
        assert a['family_slots'] == list(SLOTS)
        assert set(a['invocations_by_role']) == {'diagnose', *roles_for(profile).values(), 'curator', 'meta'}
        assert a['mock_cost']['attempts'] == 6 and a['mock_cost']['limit_usd'] == '0.2'
        assert a['independent_holdout'] == 'UNKNOWN'
        assert (root/profile/'learning'/'initial-memory.json').is_file()
        assert list((root/profile).rglob('observations.sqlite')) == [root/profile/'round'/'observations.sqlite']
        round_ = root/profile/'round'
        bindings = [json.loads((round_/slot/'binding.json').read_bytes()) for slot in SLOTS]
        assert len({b['candidate_id'] for b in bindings}) == 3
        assert bindings[0]['worker_sha256'] == bindings[1]['worker_sha256'] != bindings[2]['worker_sha256']
        assert all(len({g['dimensions']['role_profile']}) == 1 for g in a['analytics']['groups'])
    pair_books = [json.loads((root/p/'learning'/'initial-memory.json').read_bytes())['entries']
                  for p in PROFILES]
    assert pair_books[0] and pair_books[0] == pair_books[1]
    sources = [read_event_log(root/p/'round'/'observations.sqlite') for p in PROFILES]
    mixed = compute_metrics(project_sources(sources))['groups']
    assert {g['dimensions']['role_profile'] for g in mixed} == set(PROFILES)
    assert all(g['portfolio']['proposal_instances'] in (None, 3) for g in mixed)
    assert all(prompts[PROFILES[0], slot] == prompts[PROFILES[1], slot]
               for slot in ('diagnose','curator','meta'))


@pytest.mark.parametrize('control,count', [('abstain', 2), ('invalid', 2), ('ast', 2), ('infra', 3)])
def test_pair_negative_controls_keep_fixed_family(tmp_path, monkeypatch, control, count):
    report, _, _, calls, seen, _ = run(tmp_path, monkeypatch, control)
    assert report['paired_input_matched']
    for profile in PROFILES:
        arm = report['profiles'][profile]
        assert arm['family_slots'] == list(SLOTS)
        assert arm['replay_completed']['denominator'] == count
        assert arm['replay_completed']['numerator'] == 2
        if control == 'abstain':
            assert arm['invocation_outcomes']['ABSTAINED'] == 3
        if control == 'invalid':
            assert arm['invocation_outcomes']['CONTRACT_ERROR'] == 2
            assert seen.count((profile, SLOTS[0])) == 2
        if control in ('ast', 'infra'):
            assert arm['failures']


def test_drift_denies_any_dispatch_or_state(tmp_path, monkeypatch):
    args, response, calls, seen, _ = fixture(tmp_path, monkeypatch)
    frozen = freeze_pair(**args)
    root = tmp_path/'pair'
    args['slot_models']['diagnose'] = 'gpt-6.1-sol'
    with pytest.raises(ValueError, match='PAIR_FREEZE_CHANGED'):
        run_offline_pair(directory=root, frozen=frozen, response_script=response,
                         capsule_factory=lambda _: Capsule(), **args)
    assert not root.exists() and not seen and not calls


def test_incomplete_holm_family_denied():
    with pytest.raises(ValueError, match='FAMILY_EXACT_THREE_SLOTS_REQUIRED'):
        make_family([{'angle':slot} for slot in SLOTS[:2]])



def test_evaluation_fields_cannot_reach_transport(tmp_path, monkeypatch):
    args, response, calls, seen, _ = fixture(tmp_path, monkeypatch)
    args['public']['expected_answers'] = ['SYNTHETIC_PRIVATE_LABEL']
    report = run_offline_pair(directory=tmp_path/'pair', frozen=freeze_pair(**args),
                              response_script=response, capsule_factory=lambda _: Capsule(), **args)
    assert report['status'] == 'INPUT_MISMATCH'
    assert not calls
    assert all(slot in ('curator', 'meta') for _, slot in seen)
    for profile in PROFILES:
        assert not report['profiles'][profile]['family_slots']
        assert any(e['reason_code'] == 'UNAUTHORIZED_PROJECTION_FIELD'
                   for e in report['profiles'][profile]['failures'])
