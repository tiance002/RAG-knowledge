"""Post-usage USD ledger. No money reservation and no attempt-count ceiling.

Owner policy: allow a request while recorded spend is below USD5; the last call
may overshoot. Unknown usage is never treated as free or silently retried.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import threading

from .events import EventLog,canonical,digest
from .locking import WorkspaceLock


NANO = 1_000_000_000
# USD per million tokens -> nanodollars per token. Conservative cache-write
# input rate is used if the provider doesn't expose cache-write accounting.
PRICES = {'gpt-6-luna': {'input': 100, 'cached': 10, 'write': 125, 'output': 500},
          'gpt-6.1-sol': {'input': 2000, 'cached': 100, 'write': 2500, 'output': 10000},
          'gpt-6.1-terra': {'input': 1000, 'cached': 50, 'write': 1250, 'output': 5000}}
PRICE_VERSION = 'official-model-pages-2026-11-15-standard-short-context'


class Budget:
    def __init__(self, path, *, limit_usd='5'):
        limit = Decimal(limit_usd)
        if not 0 < limit <= 5:
            raise ValueError('task limit cannot exceed owner USD5 authorization')
        self.limit_nano = int(limit * NANO)
        self.log = EventLog(path)
        self.lock = Path(path).with_suffix('.lock')
        self.mutex = threading.RLock()
        self.inflight = set()

    def snapshot(self):
        starts, completed, bounds = {}, {}, {}
        for event in self.log.read():
            payload = event['payload']
            if event['event_type'] == 'MODEL_REQUEST_STARTED':
                starts[payload['request_key']] = payload
            elif event['event_type'] == 'MODEL_REQUEST_COMPLETED':
                completed[payload['request_key']] = payload
            elif event['event_type']=='MODEL_REQUEST_POSTHOC_COST_BOUND':
                bounds[payload['request_key']]=payload
        spent = sum(record.get('cost_nano_upper') or 0 for record in completed.values())
        unknown = [key for key, record in completed.items() if record.get('usage_status') != 'KNOWN']
        bounded={key:bounds[key]['cost_nano_upper'] for key in unknown if key in bounds}
        spent+=sum(bounded.values())
        unbounded=[key for key in unknown if key not in bounded]
        pending = sorted(set(starts) - set(completed))
        return {'attempts': len(starts), 'completed': len(completed), 'recorded_cost_nano_upper': spent,
                'recorded_usd_upper': str(Decimal(spent) / NANO), 'limit_usd': str(Decimal(self.limit_nano) / NANO),
                'unknown_usage_requests': unknown, 'pending_request_keys': pending,
                'unbounded_usage_requests':unbounded,'bounded_unknown_usage_costs_nano':bounded,
                'total_cost_status': 'UNKNOWN' if unbounded or pending else 'BOUNDED_USAGE_ESTIMATE' if bounded else 'USAGE_ESTIMATED',
                'reservation_usd': '0', 'count_limit': None,
                'provider_invoiced_charge': 'UNKNOWN', 'price_version': PRICE_VERSION}

    def start(self, key, role, model, request_hash):
        if model not in PRICES or 'astra' in model.casefold():
            raise ValueError('MODEL_NOT_ALLOWED')
        with self.mutex, WorkspaceLock(self.lock, 'budget-start'):
            state = self.snapshot()
            if state['unbounded_usage_requests'] or set(state['pending_request_keys']) - self.inflight:
                raise RuntimeError('BILLING_OR_REQUEST_STATUS_UNKNOWN')
            if state['recorded_cost_nano_upper'] >= self.limit_nano:
                raise RuntimeError('BUDGET_EXHAUSTED')
            # No money is reserved. Near the ceiling dispatch one at a time so
            # only the final permitted request can cross it. Far from it, the
            # three independent requests may run concurrently.
            max_call = max(r['write'] * 16_384 + r['output'] * 4096 for r in PRICES.values())
            if self.inflight and self.limit_nano - state['recorded_cost_nano_upper'] <= 3 * max_call:
                raise RuntimeError('BUDGET_SERIALIZE_REQUIRED')
            if key in state['pending_request_keys'] or any(
                    event['payload'].get('request_key') == key for event in self.log.read()
                    if event['event_type'] == 'MODEL_REQUEST_STARTED'):
                raise RuntimeError('REQUEST_ALREADY_DISPATCHED_USE_RECEIPT')
            self.log.append('request-start:' + key, 'MODEL_REQUEST_STARTED',
                            {'request_key': key, 'role': role, 'model': model, 'request_hash': request_hash,
                             'price_version': PRICE_VERSION, 'money_reserved': False})
            self.inflight.add(key)

    def complete(self, key, usage, *, status, provider_request_id=None):
        with self.mutex, WorkspaceLock(self.lock, 'budget-complete'):
            starts = [event['payload'] for event in self.log.read() if event['event_type'] == 'MODEL_REQUEST_STARTED'
                      and event['payload']['request_key'] == key]
            if len(starts) != 1:
                raise RuntimeError('UNBOUND_REQUEST_RECEIPT')
            model, cost, known = starts[0]['model'], None, isinstance(usage, dict)
            if known:
                known = all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens'))
            basis = None
            if known:
                rates = PRICES[model]
                details = usage.get('input_tokens_details') or {}
                cached = details.get('cached_tokens', 0)
                write = details.get('cache_write_tokens')
                known = type(cached) is int and 0 <= cached <= usage['input_tokens']
                if write is not None:
                    known = known and type(write) is int and 0 <= write <= usage['input_tokens'] - cached
                if known:
                    if write is None:
                        cost = (usage['input_tokens'] - cached) * rates['write'] + cached * rates['cached']
                        basis = 'post-usage conservative input cache-write upper rate; actual invoice unavailable'
                    else:
                        cost = ((usage['input_tokens'] - cached - write) * rates['input']
                                + cached * rates['cached'] + write * rates['write'])
                        basis = 'provider usage including reported cache writes, standard published prices'
                    cost += usage['output_tokens'] * rates['output']
            self.log.append('request-complete:' + key, 'MODEL_REQUEST_COMPLETED',
                            {'request_key': key, 'model': model, 'usage': usage, 'status': status,
                             'provider_request_id': provider_request_id, 'usage_status': 'KNOWN' if known else 'UNKNOWN',
                             'cost_nano_upper': cost, 'price_basis': basis, 'price_version': PRICE_VERSION})
            self.inflight.discard(key)
            return self.snapshot()

    def bound_completed_unknown(self,key,request):
        """After failure only: retain UNKNOWN usage and charge a conservative bound.

        No money is reserved. A byte-size-limited, tools-free official API request
        with a hard output-token limit has a finite charge bound. The exact
        request must match its pre-dispatch hash. Unbounded/pending calls still
        stop dispatch. This neither declares actual usage nor resends a call.
        """
        with self.mutex,WorkspaceLock(self.lock,'budget-posthoc-bound'):
            events=self.log.read()
            starts=[e['payload'] for e in events if e['event_type']=='MODEL_REQUEST_STARTED' and e['payload']['request_key']==key]
            completed=[e['payload'] for e in events if e['event_type']=='MODEL_REQUEST_COMPLETED' and e['payload']['request_key']==key]
            if (len(starts)!=1 or len(completed)!=1 or completed[0]['usage_status']=='KNOWN'
                    or digest(request)!=starts[0]['request_hash']):raise ValueError('UNBOUND_POSTHOC_BILLING_ESTIMATE')
            model=starts[0]['model'];maximum=request.get('max_output_tokens')
            if (request.get('model')!=model or model not in PRICES or request.get('tools')
                    or len(canonical(request))>16384 or type(maximum) is not int or not 0<maximum<=4096):
                raise ValueError('UNBOUNDED_REQUEST_CONTRACT')
            upper=16384*PRICES[model]['write']+maximum*PRICES[model]['output']
            self.log.append('posthoc-bound:'+key,'MODEL_REQUEST_POSTHOC_COST_BOUND',{
                'request_key':key,'request_hash':starts[0]['request_hash'],'cost_nano_upper':upper,
                'input_tokens_upper':16384,'output_tokens_upper':maximum,'usage_status':'UNKNOWN_BOUNDED',
                'basis':'post-failure request-contract bound, not actual usage or invoice; no request resent',
                'price_version':PRICE_VERSION,'money_reserved':False})
            return self.snapshot()
