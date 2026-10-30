"""E2.1 experiment-only selector; not connected to HTTP, Agent or production.

Only verified, explicitly out-of-scope noninitial expansions may be excluded.
No Gold, quality labels, qids or token estimates are accepted by this module.
"""
import copy
import hashlib
import re

RULE_VERSION = 'e21-explicit-source-conservative-v1'


def select_members(question, citations, initial_candidate_ids):
    """Preserve order and every initial candidate; uncertainty means retain."""
    initial = set(initial_candidate_ids)
    hint = re.search(r'^\s*In the (?:transportation )?report ([a-f0-9]{12,64}),\s*what is\b', question, re.I)
    if re.search(r'\b(compare|versus|vs|both|between|except|not|other reports?|reports)\b', question, re.I):
        hint = None
    identities = {}
    conflict = False
    verified = {}
    for c in citations:
        mid = c.get('chunk_id')
        identity = (c.get('fileId'), c.get('content'), c.get('page_number'))
        if mid in identities and identities[mid] != identity:
            conflict = True
        identities[mid] = identity
        fid = re.fullmatch(r'office-([a-f0-9]{64})', str(c.get('fileId', '')))
        filename = re.split(r'[/\\]', str(c.get('fileName', '')))[-1]
        valid = (bool(mid and fid) and filename.rsplit('.', 1)[0].lower() == fid[1]
                 and c.get('source_mapping_status') == 'VERIFIED'
                 and isinstance(c.get('content'), str)
                 and c.get('content_hash') == hashlib.sha256(c['content'].encode()).hexdigest())
        verified[mid] = bool(valid)
    matches = {c['fileId'] for c in citations if hint and verified[c.get('chunk_id')]
               and c['fileId'].removeprefix('office-').startswith(hint[1].lower())}
    target = next(iter(matches)) if len(matches) == 1 else None
    decisions, selected = [], []
    for c in citations:
        mid = c.get('chunk_id')
        if conflict:
            keep, reason = True, 'SOURCE_CONFLICT_CONSERVATIVE_KEEP'
        elif mid in initial:
            keep, reason = True, 'INITIAL_CANDIDATE_KEEP'
        elif not target:
            keep, reason = True, 'UNKNOWN_SCOPE_KEEP'
        elif not verified[mid]:
            keep, reason = True, 'UNKNOWN_PROVENANCE_KEEP'
        elif c['fileId'] == target:
            keep, reason = True, 'TARGET_SOURCE_KEEP'
        else:
            keep, reason = False, 'EXPLICIT_OTHER_SOURCE_NONINITIAL'
        decisions.append({'member_id':mid,'selected':keep,'reason_code':reason})
        if keep:
            selected.append(copy.deepcopy(c))
    return selected, {'selection_rule_version':RULE_VERSION,
                      'scope_status':'VERIFIED_EXPLICIT_SOURCE' if target and not conflict else 'UNKNOWN',
                      'target_source_id':target,'decisions':decisions,
                      'selected_evidence_ids':[c.get('chunk_id') for c in selected],
                      'excluded_evidence_ids':[d['member_id'] for d in decisions if not d['selected']]}


def grounded_messages(citations, history):
    """Equivalent input-only structure from generate_grounded_candidate.

    No Provider client/Generation is created. Frozen B must exactly match its
    previously captured actual messages before C can be evaluated.
    """
    evidence = '\n\n'.join(
        f"[{i}] 来源：{c.get('fileName', '未知')} {c.get('location', '')}\n{c['content']}"
        for i, c in enumerate(citations, 1))
    prompt = '请根据以下参考资料回答用户问题。资料不足时请如实说明，并适当引用来源。\n\n资料：\n' + evidence
    return [{'role':'system','content':prompt}] + copy.deepcopy(history)
