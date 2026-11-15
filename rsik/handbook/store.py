"""Workflow-only commit, conflict isolation, activation and deterministic retrieval."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from ..contracts import ContractError, object_fields, string_list, text
from ..events import EventLog, canonical, digest
from ..locking import atomic_write
from ..redact import LeakScanner
from .contracts import conflicting, conditions_match, validate_entry
from .projection import project


class Handbook:
    def __init__(self, directory, *, scanner=None, evidence_mode="REAL_REPLAY"):
        if evidence_mode not in {'REAL_REPLAY','SYNTHETIC_CONTROL'}:raise ValueError('UNKNOWN_EVIDENCE_MODE')
        self.evidence_mode=evidence_mode
        self.directory = Path(directory)
        scope_path=self.directory/'evidence-mode.json'
        expected=canonical({'evidence_mode':evidence_mode})
        if scope_path.exists():
            if scope_path.read_bytes()!=expected:raise ValueError('HANDBOOK_EVIDENCE_MODE_MISMATCH')
        else:atomic_write(scope_path,expected,exclusive=True)
        self.log = EventLog(self.directory / 'events.sqlite')
        self.scanner = scanner or LeakScanner()

    def get(self, entry_id):
        if len(entry_id) != 64 or any(c not in '0123456789abcdef' for c in entry_id):
            raise ValueError('invalid handbook content ID')
        data = (self.directory / 'entries' / entry_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry_id:
            raise RuntimeError('HANDBOOK_ENTRY_HASH_MISMATCH')
        return {'entry_id': entry_id, **json.loads(data)}

    def view(self):
        return project(self.log.read(), evidence_mode=self.evidence_mode)

    def bind_evidence(self,entry_id,context):
        """Archive private alias bindings immutably; retrieval never exports them."""
        entry=self.get(entry_id)
        if (entry['compatibility_fingerprint']!=context['compatibility_fingerprint']
                or set(entry['support_refs'])-set(context['refs'])):
            raise ContractError('UNTRACEABLE_HANDBOOK_EVIDENCE')
        refs={key:context['refs'][key] for key in entry['support_refs']}
        for ref in refs.values():
            if any(not isinstance(ref.get(k),str) or not ref[k] for k in
                   ('observation_id','candidate_id','parent_id','run_id','result_hash','source_group')):
                raise ContractError('UNTRACEABLE_HANDBOOK_EVIDENCE')
            if ref.get('result_path') and hashlib.sha256(Path(ref['result_path']).read_bytes()).hexdigest()!=ref['result_hash']:
                raise ContractError('UNTRACEABLE_HANDBOOK_EVIDENCE')
        bundle={'entry_id':entry_id,'refs':refs,'verified_candidates':context.get('verified_candidates',{}),
                'compatibility_fingerprint':context['compatibility_fingerprint']}
        data=canonical(bundle);key=hashlib.sha256(data).hexdigest();path=self.directory/'evidence'/key
        if path.exists():
            if path.read_bytes()!=data:raise RuntimeError('HANDBOOK_EVIDENCE_HASH_MISMATCH')
        else:atomic_write(path,data,exclusive=True)
        self.log.append('evidence:'+entry_id+':'+key,'ENTRY_EVIDENCE_BOUND',{'entry_id':entry_id,'evidence_bundle_sha256':key})
        return key

    def commit_delta(self, delta, context, *, idempotency_key):
        """context is built from trusted archived evidence, never model arguments.

        refs maps opaque visible aliases to private evidence metadata;
        verified_candidates is derived from completed deterministic holdout.
        """
        if context.get('evidence_mode',self.evidence_mode)!=self.evidence_mode:raise ContractError('HANDBOOK_EVIDENCE_MODE_MISMATCH')
        self.scanner.check(delta)
        object_fields(delta, ('schema_version', 'op', 'target_entry_id', 'proposed_entry', 'evidence_refs', 'reason'))
        if delta['schema_version'] != 'rsi-v2.2' or delta['op'] not in {'ADD', 'REVISE', 'DEPRECATE'}:
            raise ContractError()
        text(delta['reason']);string_list(delta['evidence_refs'])
        if not delta['evidence_refs'] or set(delta['evidence_refs']) - set(context['refs']):
            raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
        for reference in delta['evidence_refs']:
            record = context['refs'][reference]
            if any(not isinstance(record.get(k), str) or not record[k] for k in
                   ('observation_id', 'candidate_id', 'parent_id', 'run_id', 'result_hash', 'source_group')):
                raise ContractError('UNTRACEABLE_HANDBOOK_EVIDENCE')
        delta_hash = digest(delta)
        previous = next((e for e in self.log.read() if e['idempotency_key'] == 'delta:' + idempotency_key), None)
        if previous:
            if previous['payload']['delta_hash'] != delta_hash:
                raise RuntimeError('IDEMPOTENCY_CONFLICT')
            entry_id=previous['payload']['entry_id']
            if delta['op']!='DEPRECATE':
                self.bind_evidence(entry_id,context)
                self.quarantine_conflicts(entry_id)
                self.activate_if_qualified(entry_id,context)
            return entry_id
        target = delta['target_entry_id']
        if delta['op'] == 'DEPRECATE':
            self.get(target)
            if delta['proposed_entry'] is not None:
                raise ContractError()
            self.log.append('delta:' + idempotency_key, 'DEPRECATION_REQUESTED',
                            {'entry_id': target, 'delta_hash': delta_hash, 'reason': delta['reason'],
                             'evidence_refs': delta['evidence_refs']})
            return target
        entry = validate_entry(delta['proposed_entry'])
        if 'allowed_actions' in context and entry['action'] not in context['allowed_actions']:
            raise ContractError('UNSUPPORTED_HANDBOOK_INFERENCE')
        if (entry['compatibility_fingerprint'] != context['compatibility_fingerprint']
                or set(entry['applicable_format']) - set(context['allowed_formats'])):
            raise ContractError('INCOMPATIBLE_HANDBOOK_SCOPE')
        if not entry['support_refs'] or set(entry['support_refs']) - set(delta['evidence_refs']):
            raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
        if delta['op'] == 'ADD' and (target is not None or entry['supersedes']):
            raise ContractError()
        if delta['op'] == 'REVISE':
            self.get(target)
            if entry['supersedes'] != [target]:
                raise ContractError()
        body = {**entry, 'evidence_mode':self.evidence_mode, 'created_at': datetime.now(timezone.utc).isoformat()}
        data = canonical(body);entry_id = hashlib.sha256(data).hexdigest()
        atomic_write(self.directory / 'entries' / entry_id, data, exclusive=True)
        self.log.append('delta:' + idempotency_key, 'ENTRY_CREATED' if delta['op'] == 'ADD' else 'ENTRY_REVISED',
                        {'entry_id': entry_id, 'delta_hash': delta_hash, 'evidence_refs': delta['evidence_refs'],
                         'supersedes': entry['supersedes']})
        self.bind_evidence(entry_id,context)
        self.quarantine_conflicts(entry_id)
        self.activate_if_qualified(entry_id, context)
        return entry_id

    def quarantine_conflicts(self, entry_id):
        entry = self.get(entry_id)
        states = self.view()['states']
        for other_id, state in states.items():
            if other_id == entry_id or state == 'DEPRECATED':
                continue
            if conflicting(entry, self.get(other_id)):
                pair = sorted([entry_id, other_id])
                for target in pair:
                    self.log.append('conflict:' + ':'.join(pair) + ':' + target, 'ENTRY_QUARANTINED',
                                    {'entry_id': target, 'conflicting_entries': pair, 'reason': 'FINITE_ACTION_CONFLICT'})

    def activate_if_qualified(self, entry_id, context):
        if self.view()['states'].get(entry_id) != 'DRAFT':
            return False
        entry = self.get(entry_id)
        refs = [context['refs'][ref] for ref in entry['support_refs'] if ref in context['refs']
                and conditions_match(entry, context['refs'][ref].get('features', {}))]
        groups = {r['source_group'] for r in refs if r.get('source_group')}
        candidates = {r['candidate_id'] for r in refs if r.get('candidate_id')}
        verified_map = context.get('verified_candidates', {})
        verified = {candidate for candidate in candidates if isinstance(verified_map, dict)
                    and isinstance(verified_map.get(candidate), str) and len(verified_map[candidate]) == 64}
        if len(groups) < 2 or not verified:
            return False
        self.log.append('activate:' + entry_id + ':' + digest(sorted(verified)), 'ENTRY_ACTIVATED',
                        {'entry_id': entry_id, 'source_group_count': len(groups),
                         'verified_candidate_ids': sorted(verified), 'evidence_refs': entry['support_refs']})
        return True

    def retrieve(self, section, format, features, compatibility_fingerprint, *, limit=3):
        if not 0 <= limit <= 3:
            raise ValueError('at most three compatible active entries may be injected')
        result = []
        for entry_id, state in sorted(self.view()['states'].items()):
            if state != 'ACTIVE':
                continue
            entry = self.get(entry_id)
            if entry.get('evidence_mode','REAL_REPLAY')!=self.evidence_mode:continue
            if (entry['section'] == section and format in entry['applicable_format']
                    and entry['compatibility_fingerprint'] == compatibility_fingerprint
                    and conditions_match(entry, {**features, 'format': format})):
                public = {key: entry[key] for key in ('entry_id', 'section', 'applicable_format', 'conditions',
                          'action', 'explanation', 'falsification', 'compatibility_fingerprint')}
                result.append(self.scanner.check(public))
        return result[:limit]
