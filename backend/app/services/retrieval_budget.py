"""Request-local retrieval work limits; no model, index or scoring changes.

Candidate output depths are whole-request totals, not physical work limits.
Initial lexical depth is shared by all base
queries and targets; supplemental depth is shared by all selected variant/target
pairs. Hybrid defaults preserve deployed150 vector hits per target and configured
lexical depth per target/base query. Vector-only uses the caller top_k per target.
Both use an8192-candidate initial route cap.
Physical page operations and KNN requested rows are independently bounded.
Explicit environment depths still override whole-request totals.
Environment names are deliberately separate from response ``top_k``.
Default supplemental pair capacity covers at least one variant across targets
up to the hard pair cap64; total supplemental depth50 remains independent.
Explicit caps retain their exact meaning and incomplete coverage is reported.
"""
import asyncio
import contextvars
from dataclasses import dataclass, field
import math
import os
import time
import threading
import weakref

active_retrieval_budget = contextvars.ContextVar('active_retrieval_budget', default=None)
_legacy_vector_gates = weakref.WeakKeyDictionary()
_legacy_vector_gate_lock = threading.Lock()
_LEGACY_VECTOR_INFLIGHT = 2
_MAX_INITIAL_ROUTE_CANDIDATES = 8192


async def bounded_sync_vector_query(operation):
    """Offload the opted-out sync driver; cancellation retains real capacity."""
    loop = asyncio.get_running_loop()
    with _legacy_vector_gate_lock:
        reference = _legacy_vector_gates.get(loop)
        gate = reference() if reference is not None else None
        if gate is None:
            gate = asyncio.Semaphore(_LEGACY_VECTOR_INFLIGHT)
            _legacy_vector_gates[loop] = weakref.ref(gate)
    async with gate:
        task = asyncio.create_task(asyncio.to_thread(operation))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            result = task.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


class RetrievalBudgetExpired(RuntimeError):
    pass


def _number(name, default, minimum, maximum, *, integer=True):
    try:
        value = int(os.getenv(name, str(default))) if integer else float(os.getenv(name, str(default)))
    except (ValueError, TypeError) as exc:
        raise ValueError('INVALID_' + name) from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('INVALID_' + name)
    return value


def fair_depths(total, keys):
    """Allocate deterministically; input order defines the documented remainder."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    depth, remainder = divmod(total, len(keys))
    return {key: depth + (index < remainder) for index, key in enumerate(keys)}


class RequestQueryVectors(dict):
    """Never persisted/shared; the authorization scope is part of memo identity."""
    def __init__(self, kb_ids):
        super().__init__()
        self.authorization_scope = tuple(sorted(set(kb_ids)))
        self.target_dimensions = {}


@dataclass
class RetrievalBudget:
    vector_depth: int = 150
    lexical_initial_depth: int = 50
    lexical_supplement_depth: int = 50
    max_variants: int = 5
    max_variant_target_pairs: int = 8
    target_inflight: int = 2
    timeout_seconds: float = 30.0
    started: float = field(default_factory=time.perf_counter)
    deadline_exhausted: bool = False
    authorization_manifests: dict = field(default_factory=dict, repr=False)
    target_count: int = 1
    base_query_count: int = 1
    vector_baseline_depth_per_target: int = 150
    lexical_baseline_depth_per_pair: int = 50
    vector_max_page_ops: int = 64
    vector_max_knn_rows: int = 8192
    vector_work_exhausted: bool = False
    _work_lock: object = field(default_factory=threading.Lock, repr=False)
    _vector_work: dict = field(default_factory=lambda: dict(pages=0, requested=0, returned=0,
        retained_peak=0, results=0, result_rows=0, consistency_unknown=0), repr=False)
    _supplement_work: dict = field(default_factory=lambda: {phase: set() for phase in
        ('scheduled', 'started', 'completed', 'failed', 'cancelled')}, repr=False)
    _physical_work: dict = field(default_factory=lambda: dict(ops=0, rows=0, denied=0, by_stage={},
        get_ops=0, get_requested=0, get_returned=0, quota_denied_with_global_capacity=0, query_granularity_unallocated=0), repr=False)
    _target_limits: dict = field(default_factory=dict, repr=False)
    _target_usage: dict = field(default_factory=dict, repr=False)
    _target_query_depths: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_environment(cls, target_count=1, base_query_count=1, lexical_depth_per_pair=50,
                         vector_depth_per_target=150):
        return cls(
            vector_depth=_number('RAG_RETRIEVAL_VECTOR_DEPTH',
                min(vector_depth_per_target * target_count, _MAX_INITIAL_ROUTE_CANDIDATES), 1, _MAX_INITIAL_ROUTE_CANDIDATES),
            lexical_initial_depth=_number('RAG_RETRIEVAL_BM25_INITIAL_DEPTH',
                min(lexical_depth_per_pair * target_count * base_query_count, _MAX_INITIAL_ROUTE_CANDIDATES),
                1, _MAX_INITIAL_ROUTE_CANDIDATES),
            lexical_supplement_depth=_number('RAG_RETRIEVAL_BM25_SUPPLEMENT_DEPTH', 50, 0, 1000),
            max_variants=_number('RAG_RETRIEVAL_MAX_VARIANTS', 5, 0, 5),
            max_variant_target_pairs=_number('RAG_RETRIEVAL_MAX_VARIANT_TARGET_PAIRS',
                min(64, max(8, target_count)), 0, 64),
            target_inflight=_number('RAG_RETRIEVAL_TARGET_INFLIGHT', 2, 1, 8),
            timeout_seconds=_number('RAG_RETRIEVAL_DEADLINE_SECONDS', 30, .01, 300, integer=False),
            target_count=target_count,
            base_query_count=base_query_count,
            lexical_baseline_depth_per_pair=lexical_depth_per_pair,
            vector_baseline_depth_per_target=vector_depth_per_target,
            vector_max_page_ops=_number('RAG_RETRIEVAL_VECTOR_MAX_PAGE_OPS', 64, 1, 4096),
            vector_max_knn_rows=_number('RAG_RETRIEVAL_VECTOR_MAX_KNN_ROWS', 8192, 1, 1000000),
        )

    def configure_vector_targets(self, plans, order=None):
        """Max-min fair fixed quotas from conservative known scope demand.

        Fixed quotas do not borrow from unfinished targets. A denial can leave
        global capacity unused; counters expose that instead of claiming full use.
        """
        keys = list(order) if order is not None else sorted(plans)
        def allocate(total, field, demands=None):
            demands = demands if demands is not None else {key: plans[key][field] for key in keys}
            remaining = total
            allocated = {key: 0 for key in keys}
            pending = [key for key in keys if demands[key] > 0]
            while remaining and pending:
                shares = fair_depths(remaining, pending)
                for key in pending:
                    grant = min(shares[key], demands[key] - allocated[key])
                    allocated[key] += grant
                    remaining -= grant
                pending = [key for key in pending if allocated[key] < demands[key]]
            return allocated
        with self._work_lock:
            if self._physical_work['ops']:
                raise RuntimeError('VECTOR_PHYSICAL_PLAN_ALREADY_STARTED')
            ops = allocate(self.vector_max_page_ops, 'ops')
            quanta = {key: max(1, int(plans[key].get('query_depth', 1))) for key in keys}
            # A KNN query cannot spend fractional n_results. Legacy plans without
            # an explicit quantum retain unit-row compatibility.
            demand = {key: min(plans[key]['rows'], ops[key] * quanta[key])
                      if 'query_depth' in plans[key] else plans[key]['rows'] for key in keys}
            continuous = allocate(self.vector_max_knn_rows, 'rows', demand)
            rows = {key: (continuous[key] // quanta[key]) * quanta[key] for key in keys}
            remaining = self.vector_max_knn_rows - sum(rows.values())
            while remaining:
                pending = [key for key in keys if demand[key] - rows[key] >= quanta[key]
                           and quanta[key] <= remaining]
                if not pending:
                    break
                shares = fair_depths(remaining, pending)
                for key in pending:
                    quantum = quanta[key]
                    grant = min(max(shares[key], quantum) // quantum,
                                (demand[key] - rows[key]) // quantum,
                                remaining // quantum) * quantum
                    rows[key] += grant
                    remaining -= grant
            self._physical_work['query_granularity_unallocated'] = sum(continuous.values()) - sum(rows.values())
            self._target_query_depths = quanta
            self._target_limits = {key: (ops[key], rows[key]) for key in keys}

    def reserve_vector_page(self, kb_id, requested_depth, stage='knn_query'):
        if stage not in ('knn_query', 'scope_get', 'reconcile_get'):
            raise ValueError('UNKNOWN_VECTOR_PHYSICAL_STAGE')
        if type(requested_depth) is not int or requested_depth < 0:
            raise ValueError('INVALID_VECTOR_PHYSICAL_DEPTH')
        row_cost = requested_depth if stage == 'knn_query' else 0
        with self._work_lock:
            used = self._target_usage.setdefault(kb_id, [0, 0])
            limit = self._target_limits.get(kb_id, (self.vector_max_page_ops, self.vector_max_knn_rows))
            global_ok = (self._physical_work['ops'] < self.vector_max_page_ops
                         and self._physical_work['rows'] + row_cost <= self.vector_max_knn_rows)
            target_ok = used[0] < limit[0] and used[1] + row_cost <= limit[1]
            if not global_ok or not target_ok or self.deadline_exhausted:
                self.vector_work_exhausted = True
                self._physical_work['denied'] += 1
                if global_ok and not target_ok:
                    self._physical_work['quota_denied_with_global_capacity'] += 1
                return False
            used[0] += 1
            used[1] += row_cost
            self._physical_work['ops'] += 1
            self._physical_work['rows'] += row_cost
            stages = self._physical_work['by_stage']
            stages[stage] = stages.get(stage, 0) + 1
            return True

    def record_vector_get(self, stage, requested_count, returned_count):
        with self._work_lock:
            self._physical_work['get_ops'] += 1
            self._physical_work['get_requested'] += requested_count
            self._physical_work['get_returned'] += returned_count

    @property
    def deadline(self):
        return self.started + self.timeout_seconds

    def remaining(self):
        remaining = max(0.0, self.deadline - time.perf_counter())
        if not remaining:
            self.deadline_exhausted = True
        return remaining

    def check(self):
        if not self.remaining():
            raise RetrievalBudgetExpired('RETRIEVAL_DEADLINE_EXHAUSTED')

    async def wait_for(self, awaitable):
        """Cancelling a caller never releases an executor's actual worker slot."""
        try:
            return await asyncio.wait_for(awaitable, self.remaining())
        except asyncio.TimeoutError as exc:
            self.deadline_exhausted = True
            raise RetrievalBudgetExpired('RETRIEVAL_DEADLINE_EXHAUSTED') from exc

    def record_vector_page(self, requested_depth, returned_count, retained_count):
        with self._work_lock:
            self._vector_work['pages'] += 1
            self._vector_work['requested'] += requested_depth
            self._vector_work['returned'] += returned_count
            self._vector_work['retained_peak'] = max(self._vector_work['retained_peak'], retained_count)

    def record_vector_result(self, count):
        with self._work_lock:
            self._vector_work['results'] += 1
            self._vector_work['result_rows'] += count

    def record_vector_consistency_unknown(self):
        with self._work_lock:
            self._vector_work['consistency_unknown'] += 1

    def record_supplement_pair(self, phase, pair):
        with self._work_lock:
            self._supplement_work[phase].add(pair)

    def work_snapshot(self):
        with self._work_lock:
            return dict(self._vector_work), {phase: set(pairs) for phase, pairs in self._supplement_work.items()}

    def metrics(self):
        vector, supplement = self.work_snapshot()
        with self._work_lock:
            physical = {**self._physical_work, 'by_stage': dict(self._physical_work['by_stage'])}
            allocations = {key: list(value) for key, value in self._target_limits.items()}
            query_depths = dict(self._target_query_depths)
        return {
            'vector_request_depth': self.vector_depth,
            'bm25_initial_request_depth': self.lexical_initial_depth,
            'bm25_supplement_request_depth': self.lexical_supplement_depth,
            'max_expansion_queries': self.max_variants,
            'max_variant_target_pairs': self.max_variant_target_pairs,
            'target_inflight_limit': self.target_inflight,
            'retrieval_deadline_seconds': self.timeout_seconds,
            'retrieval_deadline_exhausted': self.deadline_exhausted,
            'retrieval_default_policy': 'baseline_depths_bounded_initial_v3',
            'initial_route_candidate_hard_cap': _MAX_INITIAL_ROUTE_CANDIDATES,
            'vector_baseline_depth_per_target': self.vector_baseline_depth_per_target,
            'bm25_baseline_depth_per_pair': self.lexical_baseline_depth_per_pair,
            'vector_baseline_required_depth_total': self.vector_baseline_depth_per_target * self.target_count,
            'bm25_baseline_required_depth_total': self.lexical_baseline_depth_per_pair * self.target_count * self.base_query_count,
            'retrieval_target_count': self.target_count,
            'retrieval_base_query_count': self.base_query_count,
            'vector_physical_page_queries': vector['pages'],
            'vector_page_counter_phase': 'COMPLETED_PAGE_RESPONSE_SNAPSHOT',
            'vector_page_requested_depth_total': vector['requested'],
            'vector_page_rows_returned_total': vector['returned'],
            'vector_page_rows_retained_peak': vector['retained_peak'],
            'vector_queries_completed': vector['results'],
            'vector_result_rows_total': vector['result_rows'],
            'vector_consistency_unknown_count': vector['consistency_unknown'],
            'vector_max_physical_page_ops': self.vector_max_page_ops,
            'vector_max_requested_knn_rows': self.vector_max_knn_rows,
            'vector_physical_page_ops_reserved': physical['ops'],
            'vector_requested_knn_rows_reserved': physical['rows'],
            'vector_physical_ops_by_stage': physical['by_stage'],
            'vector_physical_reservations_denied': physical['denied'],
            'vector_work_budget_exhausted': self.vector_work_exhausted,
            'vector_quota_denials_with_unused_global_capacity': physical['quota_denied_with_global_capacity'],
            'vector_aux_gets_completed': physical['get_ops'],
            'vector_aux_ids_requested_total': physical['get_requested'],
            'vector_aux_ids_returned_total': physical['get_returned'],
            'vector_physical_target_allocations': allocations,
            'vector_physical_allocation_policy': 'scope_demand_query_granular_fixed_no_borrow_v2',
            'vector_row_quota_unallocated_due_to_query_granularity': physical['query_granularity_unallocated'],
            'vector_physical_target_query_depths': query_depths,
            **{'supplement_pairs_' + phase: len(pairs) for phase, pairs in supplement.items()},
            'supplement_pairs_not_started': len(supplement['scheduled'] - supplement['started']),
            **{'supplement_variants_' + phase: len({pair[0] for pair in pairs})
               for phase, pairs in supplement.items()},
            'retrieval_work_counter_scope': 'request_return_snapshot_workers_may_finish_later',
            'supplement_cancelled_counter_semantics': 'cancelled_waiters_may_overlap_started_completed_work',
        }
