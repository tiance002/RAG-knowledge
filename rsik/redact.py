"""Build agent inputs from explicit public fields; never copy feedback.details."""
from __future__ import annotations

import base64
import html
import json
import re
import unicodedata
from urllib.parse import unquote
import uuid

from .contracts import ContractError


COUNTS = {'questions', 'source_groups', 'delivered_members', 'seed_members', 'nonseed_members',
          'context_chars', 'paired', 'blocked', 'failed', 'favorable', 'unfavorable', 'ties',
          'rounds', 'proposal_instances', 'qualified_proposals', 'abstentions', 'adoptions', 'validated', 'verified', 'unknown',
          'pdf_members','other_members','duplicate_key_eligible_members','exact_content_duplicate_pairs',
          'seed_content_chars','nonseed_content_chars','nonseed_members_le100','nonseed_members_le200','nonseed_members_gt200',
          'dependency_edges','related_member_pairs'}
CATEGORIES = {'CONTEXT_LENGTH', 'NONSEED_EXPANSION', 'REPLAY_SUMMARY', 'UNKNOWN'}
FORMATS = {'PDF', 'DOCX', 'XLSX', 'OTHER'}


def normalized(value):
    for _ in range(2):
        value = re.sub(r'\\+u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), value)
        value = unquote(html.unescape(value))
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if unicodedata.category(c) != 'Cf')


class LeakScanner:
    def __init__(self, private_values=()):
        self.forbidden = []
        for value in private_values:
            if isinstance(value, str) and len(value) >= 16:
                self.forbidden.extend([normalized(value), normalized(base64.b64encode(value.encode()).decode())])

    def check(self, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False)
        public = normalized(body)
        if any(secret and secret in public for secret in self.forbidden):
            raise ContractError('LEAK_DETECTED')
        if re.search(r'sk-[A-Za-z0-9_-]{15,}|-----BEGIN[ A-Z]*PRIVATE KEY', body):
            raise ContractError('SECRET_DETECTED')
        return value


def project_summary(category, counts, *, format='OTHER', include_count_evidence=False):
    if category not in CATEGORIES or format not in FORMATS or not isinstance(counts, dict) or set(counts) - COUNTS:
        raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
    reference = 'observation-' + uuid.uuid4().hex
    result = {'observation_ref': reference, 'category': category, 'stage': 'CONTEXT_SELECTION',
              'format': format, 'problem_layer': 'SELECTION', 'counts': dict(counts),
              'member_refs': [], 'safe_evidence': []}
    if include_count_evidence:
        # This snippet contains only allowed aggregate counts, never source text.
        quote = '; '.join(f'{name}={counts[name]}' for name in sorted(counts))
        result['safe_evidence'] = [{'evidence_ref': 'evidence-' + uuid.uuid4().hex, 'text': quote}]
    return result


def validate_handbook_summaries(entries, entry_ids):
    if not isinstance(entries,list) or len(entries)>3 or not isinstance(entry_ids,list) or len(entry_ids)>3:
        raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
    from .handbook.contracts import validate_entry
    for entry in entries:
        keys={'entry_id','section','applicable_format','conditions','action','explanation','falsification','compatibility_fingerprint'}
        if not isinstance(entry,dict) or set(entry)!=keys or entry['entry_id'] not in entry_ids:
            raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
        validate_entry({**{k:v for k,v in entry.items() if k!='entry_id'},'support_refs':[],'supersedes':[]})
    if len({e['entry_id'] for e in entries})!=len(entries):raise ContractError()


def make_visible(observations, *, parent_code, round_id=None, handbook_entry_ids=(), handbook_entries=(), scanner=None):
    scanner = scanner or LeakScanner()
    for obs in observations:
        if not isinstance(obs, dict) or set(obs) != {'observation_ref', 'category', 'stage', 'format', 'problem_layer', 'counts',
                       'member_refs', 'safe_evidence'} or obs['category'] not in CATEGORIES:
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        if (obs['stage'] != 'CONTEXT_SELECTION' or obs['format'] not in FORMATS
                or obs['problem_layer'] != 'SELECTION'
                or not re.fullmatch(r'observation-[0-9a-f]{32}', obs['observation_ref'])
                or not isinstance(obs['counts'], dict) or set(obs['counts']) - COUNTS
                or any(type(v) is not int or v < 0 for v in obs['counts'].values())
                or obs['member_refs'] != [] or not isinstance(obs['safe_evidence'], list)):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        for evidence in obs['safe_evidence']:
            expected_quote = '; '.join(f'{name}={obs["counts"][name]}' for name in sorted(obs['counts']))
            if (not isinstance(evidence, dict) or set(evidence) != {'evidence_ref', 'text'}
                    or not re.fullmatch(r'evidence-[0-9a-f]{32}', evidence['evidence_ref'])
                    or evidence['text'] != expected_quote):
                raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
    visible = {'round_id': round_id or 'round-' + uuid.uuid4().hex,
               'parent_alias': 'parent-' + uuid.uuid4().hex,
               'observations': observations, 'observation_refs': [o['observation_ref'] for o in observations],
               'evidence_quotes': {e['evidence_ref']: e['text'] for o in observations for e in o['safe_evidence']},
               'parent_code': parent_code, 'handbook_entry_ids': list(handbook_entry_ids)}
    if handbook_entries:
        validate_handbook_summaries(list(handbook_entries),visible['handbook_entry_ids'])
        visible['handbook_entries']=list(handbook_entries)
    return scanner.check(visible)
