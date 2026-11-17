"""Full candidate identity controls; no candidate source is executed."""
from rsik.analytics.metrics import compute_metrics
from rsik.analytics.projections import project_sources
from rsik.analytics.reader import read_event_log
from rsik.observability import Observer
from rsik.workflow import execute_round, full_worker_identity
from rsik.tests.test_workflow import FakeRoles, public_context, callbacks


def test_complete_worker_hash_and_structural_identity(tmp_path):
    observer = Observer(tmp_path / 'observations.sqlite')
    sources = [b'x = 1\n', b'x=1 # equivalent syntax\n', b'x = 2\n']
    roles = FakeRoles(same_patch=True)
    def review(proposal):
        from rsik.adjudicate import SLOTS
        return {'worker': sources[SLOTS.index(proposal['angle'])]}
    def replay(proposal, artifact):
        return callbacks()[1](proposal, full_worker_identity(artifact)['worker_sha256'])
    execute_round(public_context(), roles, review, replay, observer=observer)
    records = project_sources([read_event_log(observer.path)])
    group, = compute_metrics(records)['groups']
    instances = group['portfolio']['instances']
    assert len(instances) == 3
    assert len({i['patch_byte_hash'] for i in instances}) == 1
    assert len({i['worker_sha256'] for i in instances}) == 3
    assert len({i['normalized_ast_sha256'] for i in instances}) == 2
    assert all(i['ast_scope'] == 'FULL_WORKER' for i in instances)
    metrics = {m['metric_id']: m for m in group['metrics']}
    for name, numerator in [('P3', 2), ('P4', 1)]:
        assert (metrics[name]['numerator'], metrics[name]['denominator']) == (numerator, 3)


def test_patch_or_unparseable_worker_cannot_supply_ast():
    missing = full_worker_identity({'worker_patch': {'old': 'x=1', 'new': 'x=2'}})
    assert missing == {'worker_sha256': 'UNKNOWN', 'normalized_ast_sha256': 'UNKNOWN'}
    invalid = full_worker_identity({'worker': b'def ('})
    assert invalid['normalized_ast_sha256'] == 'UNKNOWN'
    assert invalid['worker_sha256'] != 'UNKNOWN'
