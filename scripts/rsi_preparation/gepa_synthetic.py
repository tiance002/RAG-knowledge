"""SYNTHETIC: pinned official GEPA optimize, no model/credentials/network.

Run with Python -I, --source pointing at the existing pinned GEPA src directory.
Only generated synthetic artifacts and standard-library socket denial are used.
"""
import argparse
import hashlib
import json
import socket
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--guard-resume', action='store_true')
    ap.add_argument('--resume-only', action='store_true')
    args = ap.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=args.resume_only)
    blocked = []

    def deny(*a, **kw):
        blocked.append('NETWORK_ATTEMPT')
        raise RuntimeError('SYNTHETIC: network disabled')

    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    sys.path.insert(0, str(Path(args.source).resolve()))
    from gepa import optimize
    from gepa.core.adapter import EvaluationBatch
    from gepa.core.state import GEPAState

    class Pause(Exception):
        pass

    class Unknown(Exception):
        pass

    class Log:
        def __init__(self):
            self.lines = []

        def log(self, text):
            self.lines.append(str(text))
            (output / 'latest-engine.log').write_text('\n'.join(self.lines), encoding='utf8')

    scenarios = []
    started = time.perf_counter()
    for mode in ['normal', 'human_pause', 'unknown']:
        root = output / mode
        root.mkdir(exist_ok=args.resume_only)
        ledger_path = root / 'request-ledger.json'
        ledger = {}
        calls = {'synthetic_executions': 0, 'cache_hits': 0, 'proposals': 0, 'proposal_executions': 0, 'proposal_cache_hits': 0, 'restores': 0}
        events = []
        log = Log()

        class Adapter:
            propose_new_texts = None
            def get_adapter_state(self):
                return {'ledger_reference': str(ledger_path), 'marker': 'checkpoint-before-iteration'}

            def set_adapter_state(self, state):
                calls['restores'] += 1
                events.append({'adapter_restored': state})

            def evaluate(self, batch, candidate, capture_traces=False):
                data = []
                fresh = 0
                for inst in batch:
                    key = hashlib.sha256(json.dumps([candidate, inst, 'synthetic-dataset-v1'], sort_keys=True).encode()).hexdigest()
                    if key in ledger:
                        calls['cache_hits'] += 1
                        entry = ledger[key]
                        if entry['status'] == 'UNKNOWN':
                            raise Unknown('Persisted UNKNOWN; no repeated execution')
                        if entry['status'] == 'PENDING_REVIEW':
                            raise Pause('Human review still pending')
                    else:
                        calls['synthetic_executions'] += 1
                        fresh += 1
                        score = 1.0 if candidate['instruction'] == 'P1' else 0.25
                        status = 'DONE'
                        if mode in ['human_pause', 'unknown'] and candidate['instruction'] == 'P1':
                            status = 'PENDING_REVIEW' if mode == 'human_pause' else 'UNKNOWN'
                        entry = {'qid': inst['id'], 'candidate': candidate, 'status': status, 'output': 'SYNTHETIC answer', 'score': None if status == 'UNKNOWN' else score}
                        ledger[key] = entry
                        ledger_path.write_text(json.dumps(ledger, indent=2), encoding='utf8')
                        if status == 'PENDING_REVIEW':
                            raise Pause('Output persisted before human review pause')
                        if status == 'UNKNOWN':
                            raise Unknown('Synthetic uncertain outcome persisted; no score assigned')
                    data.append(entry)
                events.append({'evaluate': candidate, 'ids': [x['id'] for x in batch], 'capture_traces': capture_traces, 'fresh': fresh})
                return EvaluationBatch(outputs=[r['output'] for r in data], scores=[r['score'] for r in data], trajectories=[{'feedback': 'SYNTHETIC known failure', 'qid': r['qid']} for r in data] if capture_traces else None, objective_scores=[{'core': r['score'], 'citation': r['score']} for r in data], num_metric_calls=fresh)

            def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
                return {key: [{'Feedback': 'SYNTHETIC improve citation coverage', 'Outputs': eval_batch.outputs}] for key in components_to_update}

        def proposal(candidate, reflective_dataset, components_to_update, *, metadata=None):
            calls['proposals'] += 1
            events.append({'proposal_metadata': metadata, 'components': components_to_update})
            cache_path = root / 'proposal-ledger.json'
            proposals = json.loads(cache_path.read_text()) if cache_path.exists() else {}
            key = hashlib.sha256(json.dumps([candidate, reflective_dataset, components_to_update], sort_keys=True).encode()).hexdigest()
            if args.guard_resume and key in proposals:
                calls['proposal_cache_hits'] += 1
                return proposals[key]
            calls['proposal_executions'] += 1
            proposed = {'instruction': 'P1'}
            proposals[key] = proposed
            cache_path.write_text(json.dumps(proposals, indent=2))
            return proposed

        def run(seed=None):
            if ledger_path.exists():
                persisted = json.loads(ledger_path.read_text(encoding='utf8'))
                ledger.clear()
                ledger.update(persisted)
            if args.guard_resume and any(r['status'] == 'UNKNOWN' for r in ledger.values()):
                raise Unknown('Run has unresolved UNKNOWN; blocked before official engine/proposer')
            return optimize(seed_candidate=seed or {'instruction': 'P0'}, trainset=[{'id': 't0'}, {'id': 't1'}], valset=[{'id': 'v0'}, {'id': 'v1'}], adapter=Adapter(), custom_candidate_proposer=proposal, reflection_lm=None, reflection_minibatch_size=2, skip_perfect_score=False, use_merge=False, max_metric_calls=20, stop_callbacks=lambda state: len(state.program_candidates) >= 2 or state.i >= 3, run_dir=str(root / 'run'), logger=log, cache_evaluation=True, seed=7, raise_on_exception=True)

        if args.resume_only:
            terminal = 'COMPLETED'
            try:
                run()
            except Unknown:
                terminal = 'BLOCKED_UNKNOWN'
            assert calls['synthetic_executions'] == calls['proposal_executions'] == 0
            scenarios.append({'scenario': mode, 'status': 'PASS', 'terminal': terminal, 'counters': calls.copy()})
            continue

        first = 'COMPLETED'
        try:
            run()
        except (Pause, Unknown) as exc:
            first = type(exc).__name__
        checkpoint = root / 'run/gepa_state.bin'
        assert checkpoint.exists()
        before = calls.copy()
        saved = GEPAState.load(str(root / 'run'))
        saved_pool = len(saved.program_candidates)
        if mode == 'human_pause':
            # A human has completed the review; this is only a synthetic approval.
            for row in ledger.values():
                if row['status'] == 'PENDING_REVIEW':
                    row['status'] = 'DONE'
            ledger_path.write_text(json.dumps(ledger, indent=2), encoding='utf8')
            # On resume, permit subsequent synthetic child examples without pausing.
            mode_for_adapter = mode
            mode = 'human_reviewed'
            run()
            mode = mode_for_adapter
            assert calls['cache_hits'] > before['cache_hits']
            assert all(row['status'] == 'DONE' for row in ledger.values())
        elif mode == 'unknown':
            try:
                run()
                raise AssertionError('UNKNOWN should stop')
            except Unknown:
                pass
            assert calls['synthetic_executions'] == before['synthetic_executions']
            assert any(row['status'] == 'UNKNOWN' for row in ledger.values())
        else:
            run()
            assert calls['synthetic_executions'] == before['synthetic_executions']
        final = GEPAState.load(str(root / 'run'))
        assert len(final.program_candidates) == (1 if mode == 'unknown' else 2)
        assert not blocked
        record = {'scenario': mode, 'status': 'PASS', 'first_exit': first, 'saved_pool_before_resume': saved_pool, 'final_pool': len(final.program_candidates), 'counters_before_resume': before, 'counters_after_resume': calls.copy(), 'unknown_records': sum(r['status'] == 'UNKNOWN' for r in ledger.values()), 'checkpoint_adapter_state': saved.adapter_state}
        scenarios.append(record)
        (root / 'events.json').write_text(json.dumps(events, indent=2), encoding='utf8')
        (root / 'engine.log').write_text('\n'.join(log.lines), encoding='utf8')
    summary = {'status': 'PASS', 'kind': 'SYNTHETIC', 'external_model_calls': 0, 'network_attempts': len(blocked), 'duration_seconds': time.perf_counter() - started, 'scenarios': scenarios}
    (output / ('resume-summary.json' if args.resume_only else 'summary.json')).write_text(json.dumps(summary, indent=2), encoding='utf8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
