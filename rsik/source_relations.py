"""Versioned exact-support evaluator, separate from legacy seed predicates.

This checks pre-reviewed AND/OR support rules and exact delivery/citation spans;
it does not infer whether an arbitrary natural-language fact list is complete.
"""
from pathlib import Path
import hashlib
import json
from .events import canonical, digest
from .exposure import REVIEW_POLICIES

PROFILE = 'source-relations/v1'
CATEGORIES = {'NONSEED_NECESSITY', 'CROSS_MEMBER_RELATION', 'NEGATION_CONDITION',
              'UNIT_SCOPE_OWNERSHIP', 'MULTI_SOURCE_SUPPORT', 'CITATION_SUPPORT'}
COMPONENTS = {'entity', 'subject', 'object', 'value', 'unit', 'condition',
              'relation', 'scope', 'negation'}
PARTITIONS = {'CONTROL', 'DEV', 'ITERATIVE_VALIDATION', 'FINAL_HOLDOUT'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _interval(text, start, end, exact):
    return (type(start) is int and type(end) is int and 0 <= start < end <= len(text)
            and text[start:end] == exact)


def validate_spec(case, spec, source_bytes):
    """Validate fixture assertions, including explicit complete alternatives."""
    if spec.get('profile') != PROFILE or not spec.get('lineage_id'):
        raise ValueError('RELATIONS_PROFILE_REQUIRED')
    source_records = spec['sources']
    if not source_records or set(source_records) != set(source_bytes):
        raise ValueError('RELATIONS_SOURCE_SET_MISMATCH')
    texts = {}
    for sid, source in source_records.items():
        if (sha(source_bytes[sid]) != source['content_sha256'] or not source.get('source_family_id')
                or not source.get('document_id') or not source.get('version')
                or source.get('relation') not in {'UNKNOWN', 'ORIGINAL', 'VERSION_OF', 'REPRINT_OF', 'EXCERPT_OF', 'DERIVED_FROM'}
                or not isinstance(source.get('related_source_ids'), list)
                or source.get('exposure_status') not in {'KNOWN_HISTORY', 'EXPOSURE_UNKNOWN'}
                or source.get('review_status') not in {'UNREVIEWED', 'AI_REVIEWED', 'HUMAN_REVIEWED'}):
            raise ValueError('RELATIONS_SOURCE_VERSION_MISMATCH')
        if source['relation'] == 'UNKNOWN' and (source['related_source_ids']
                or source['exposure_status'] != 'EXPOSURE_UNKNOWN'):
            raise ValueError('RELATIONS_UNKNOWN_SOURCE_HISTORY_MISMATCH')
        texts[sid] = source_bytes[sid].decode('utf-8')
    chunks = {c['id']: c for c in case['chunks']}
    files = {f['id']: f for f in case['files']}
    if len(chunks) != len(case['chunks']) or set(chunks) != set(spec['member_spans']):
        raise ValueError('RELATIONS_MEMBER_SET_MISMATCH')
    for mid, loc in spec['member_spans'].items():
        c = chunks[mid]; source = source_records[loc['source_id']]
        if (not _interval(texts[loc['source_id']], loc['start'], loc['end'], c['content'])
                or sha(c['content'].encode()) != c['content_sha256']
                or c['file_id'] != source['file_id'] or c['file_id'] not in files
                or files[c['file_id']]['kb_id'] != source['kb_id']
                or source['kb_id'] not in case['allowed_kb_ids']):
            raise ValueError('RELATIONS_MEMBER_SOURCE_MISMATCH')
    if not spec.get('atoms') or len({a['atom_id'] for a in spec['atoms']}) != len(spec['atoms']):
        raise ValueError('RELATIONS_ATOMS_REQUIRED')
    for atom in spec['atoms']:
        components = atom['components']
        if (not components or set(components) - COMPONENTS or not components.get('entity')
                or any(not isinstance(v, str) or not v for v in components.values())
                or not atom['categories'] or set(atom['categories']) - CATEGORIES
                or atom.get('equivalence_rule') != 'EXACT_REVIEWED_SUPPORT_ALTERNATIVES'):
            raise ValueError('RELATIONS_ATOM_INVALID')
        review = atom['review']
        subject = {k: v for k, v in atom.items() if k != 'review'}
        if (review.get('status') not in {'AI_REVIEWED', 'HUMAN_REVIEWED', 'UNREVIEWED'}
                or review.get('subject_sha256') != digest(subject)
                or not review.get('reviewer') or not review.get('reviewed_at')):
            raise ValueError('RELATIONS_REVIEW_BINDING_INVALID')
        supports = atom['supports']
        if not supports or len({s['support_id'] for s in supports}) != len(supports):
            raise ValueError('RELATIONS_SUPPORT_INVALID')
        by_id = {s['support_id']: s for s in supports}
        for support in supports:
            loc = spec['member_spans'].get(support['member_id'])
            if (not loc or support['source_id'] != loc['source_id']
                    or not loc['start'] <= support['start'] < support['end'] <= loc['end']
                    or not _interval(texts[support['source_id']], support['start'], support['end'], support['text'])
                    or not support['binds'] or set(support['binds']) - set(components)
                    or any(components[key] not in support['text'] for key in support['binds'])):
                raise ValueError('RELATIONS_SUPPORT_SOURCE_MISMATCH')
        alternatives = atom['support_alternatives']
        if not alternatives:
            raise ValueError('RELATIONS_COMPLETE_ALTERNATIVE_REQUIRED')
        referenced = set()
        for alternative in alternatives:
            if not alternative or len(set(alternative)) != len(alternative) or set(alternative) - set(by_id):
                raise ValueError('RELATIONS_COMPLETE_ALTERNATIVE_REQUIRED')
            referenced.update(alternative)
            selected = [by_id[s] for s in alternative]
            if set().union(*(set(s['binds']) for s in selected)) != set(components):
                raise ValueError('RELATIONS_PARTIAL_COMPONENT_ALTERNATIVE')
            members = {s['member_id'] for s in selected}
            families = {source_records[s['source_id']]['source_family_id'] for s in selected}
            cats = atom['categories']
            if ('NONSEED_NECESSITY' in cats and not members - set(case['seed_ids'])
                    or 'CROSS_MEMBER_RELATION' in cats and (len(members) < 2
                        or not {'subject','object','relation'} <= set(components))
                    or 'NEGATION_CONDITION' in cats and 'condition' not in components
                    or 'UNIT_SCOPE_OWNERSHIP' in cats and not {'entity','value','unit','scope'} <= set(components)
                    or 'MULTI_SOURCE_SUPPORT' in cats and len(families) < 2):
                raise ValueError('RELATIONS_CATEGORY_NOT_ESTABLISHED')
        if referenced != set(by_id):
            raise ValueError('RELATIONS_UNREFERENCED_SUPPORT')
        if 'CITATION_SUPPORT' in atom['categories'] and atom.get('citation_required') is not True:
            raise ValueError('RELATIONS_CITATION_REQUIRED')
    return texts


def assess(case, capture, spec, source_bytes):
    """Evaluate frozen assertions against actual delivered bytes and positions."""
    if not isinstance(capture, dict):
        return {'status':'UNKNOWN','reason':'RELATIONS_DELIVERY_INCOMPLETE','atoms':[], 'profile':PROFILE}
    try:
        validate_spec(case, spec, source_bytes)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return {'status':'UNKNOWN','reason':'RELATIONS_FIXTURE_INVALID','atoms':[], 'profile':PROFILE}
    members, proofs = capture.get('members', []), capture.get('proof', [])
    chunks = {c['id']: c for c in case['chunks']}
    context = capture.get('context', '')
    messages = capture.get('messages', [])
    if (capture.get('status') != 'OK' or not isinstance(members, list) or not isinstance(proofs, list)
            or not isinstance(messages, list) or not messages or not isinstance(messages[0], dict)
            or not isinstance(messages[0].get('content'), str) or not isinstance(context, str)
            or len(members) != len(proofs) or any(not isinstance(m, dict) for m in members)
            or len({m.get('id') for m in members}) != len(members)):
        return {'status':'UNKNOWN','reason':'RELATIONS_DELIVERY_INCOMPLETE','atoms':[], 'profile':PROFILE}
    valid = set()
    for member, proof in zip(members, proofs, strict=True):
        if not isinstance(member, dict) or not isinstance(proof, dict):
            return {'status':'UNKNOWN','reason':'RELATIONS_DELIVERY_SOURCE_MISMATCH','atoms':[], 'profile':PROFILE}
        mid = member.get('id'); chunk = chunks.get(mid)
        try:
            if (not chunk or proof['id'] != mid or member['content'] != chunk['content']
                    or member['file_id'] != chunk['file_id']
                    or member['kb_id'] != spec['sources'][spec['member_spans'][mid]['source_id']]['kb_id']
                    or not _interval(context, proof['context_start'], proof['context_end'], member['content'])
                    or not _interval(messages[0]['content'], proof['prompt_start'], proof['prompt_end'], member['content'])):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return {'status':'UNKNOWN','reason':'RELATIONS_DELIVERY_SOURCE_MISMATCH','atoms':[], 'profile':PROFILE}
        valid.add(mid)
    results = []
    for atom in spec['atoms']:
        if atom['review']['status'] == 'UNREVIEWED':
            results.append({'atom_id':atom['atom_id'],'status':'UNKNOWN','reason':'ATOM_UNREVIEWED'})
            continue
        supports = {s['support_id']: s for s in atom['supports']}
        present = [alt for alt in atom['support_alternatives']
                   if all(supports[s]['member_id'] in valid for s in alt)]
        status, reason = ('PRESENT','FROZEN_COMPLETE_SUPPORT_PRESENT') if present else ('ABSENT','REQUIRED_SUPPORT_MISSING')
        if not present:
            visible = [context] + [m.get('content', '') for m in messages if isinstance(m, dict)]
            unbound = [supports[s] for alt in atom['support_alternatives'] for s in alt
                       if supports[s]['member_id'] not in valid
                       and any(supports[s]['text'] in text for text in visible if isinstance(text, str))]
            if unbound:
                status, reason = 'UNKNOWN', 'UNBOUND_SOURCE_SUPPORT_TEXT_VISIBLE'
        if present and atom.get('citation_required'):
            citations = capture.get('citations', [])
            supported = False
            for alternative in present:
                if all(any(isinstance(c, dict) and c.get('atom_id') == atom['atom_id'] and c.get('support_id') == sid
                           and all(c.get(k) == supports[sid][k] for k in ('source_id','member_id','start','end'))
                           for c in citations) for sid in alternative):
                    supported = True
            if not supported:
                status, reason = 'ABSENT','CITATION_SUPPORT_MISMATCH'
        results.append({'atom_id':atom['atom_id'],'status':status,'reason':reason})
    status = ('ABSENT' if any(r['status']=='ABSENT' for r in results) else
              'UNKNOWN' if any(r['status']=='UNKNOWN' for r in results) else 'PRESENT')
    return {'status':status, 'atoms':results, 'profile':PROFILE,
            'claim_scope':'only pre-reviewed exact source/component/citation predicates; no answer-quality claim'}


def _verify_final_snapshot(manifest, specs, *, review_policy='production-source-family/v1'):
    snapshot = manifest.get('exposure_snapshot')
    experiment_id = manifest.get('experiment_id')
    if not isinstance(snapshot, dict) or not experiment_id:
        raise ValueError('RELATIONS_FINAL_EXPOSURE_SNAPSHOT_REQUIRED')
    source_ids = sorted({sid for spec in specs.values() for sid in spec['sources']})
    from .exposure import ExposureLedger
    current = ExposureLedger.verify_snapshot(snapshot, source_ids=source_ids,
        experiment_id=experiment_id, review_policy=review_policy)
    if not current['eligible']:
        raise ValueError('RELATIONS_FINAL_SOURCE_EXPOSED_OR_UNKNOWN')
    ledger_sources = {source['source_id']: source for source in current['sources']}
    for spec in specs.values():
        for source_id, source in spec['sources'].items():
            ledger = ledger_sources[source_id]
            if (ledger['content_sha256'] != source['content_sha256']
                    or ledger['document_id'] != source['document_id']
                    or ledger['version'] != source['version']
                    or ledger['source_family_id'] != source['source_family_id']
                    or ledger['relation'] != source['relation']
                    or ledger['related_source_ids'] != source['related_source_ids']
                    or ledger['exposure_status'] != source['exposure_status']
                    or ledger['review_status'] != source['review_status']):
                raise ValueError('RELATIONS_FINAL_SOURCE_HISTORY_MISMATCH')


def check_fixture(directory, *, expected_count=None,
                  review_policy='production-source-family/v1'):
    if not isinstance(review_policy, str) or review_policy not in REVIEW_POLICIES:
        raise ValueError('RELATIONS_REVIEW_POLICY_INVALID')
    directory = Path(directory).resolve()
    manifest = _load(directory/'manifest.json')
    if (manifest.get('profile') != PROFILE or not manifest.get('lineage_id')
            or manifest.get('partition') not in PARTITIONS):
        raise ValueError('RELATIONS_PROFILE_REQUIRED')
    cases = _load(directory/'cases.json'); specs = _load(directory/'relations.restricted.json')
    if (expected_count is not None and len(cases) != expected_count) or not 1 <= len(cases) <= 20:
        raise ValueError('RELATIONS_BUNDLE_SIZE_INVALID')
    if len({i['qid'] for i in cases}) != len(cases) or set(specs) != {i['qid'] for i in cases}:
        raise ValueError('RELATIONS_CASE_SET_MISMATCH')
    if (set(manifest.get('artifact_hashes', {})) != {'cases.json','relations.restricted.json'}
            or any(sha((directory/name).read_bytes()) != h
                   for name,h in manifest['artifact_hashes'].items())):
        raise ValueError('RELATIONS_FIXTURE_CHANGED')
    for item in cases:
        spec = specs[item['qid']]
        if spec['lineage_id'] != manifest['lineage_id']:
            raise ValueError('RELATIONS_LINEAGE_CHANGED')
        sources = {}
        for sid, source in spec['sources'].items():
            path = (directory/source['relative_path']).resolve()
            if not path.is_relative_to(directory):
                raise ValueError('RELATIONS_SOURCE_ESCAPES_FIXTURE')
            sources[sid] = path.read_bytes()
        validate_spec(item['case'], spec, sources)
    exposure_snapshot = None
    if manifest['partition'] == 'FINAL_HOLDOUT':
        exposure_snapshot = _verify_final_snapshot(manifest, specs, review_policy=review_policy)
        if any(atom['review']['status'] not in REVIEW_POLICIES[review_policy]
               for spec in specs.values() for atom in spec['atoms']):
            raise ValueError('RELATIONS_FINAL_ATOMS_REVIEW_POLICY_NOT_MET')
    family_ids = {s['source_family_id'] for spec in specs.values() for s in spec['sources'].values()}
    exposure_statuses = {s['exposure_status'] for spec in specs.values() for s in spec['sources'].values()}
    review_statuses = {s['review_status'] for spec in specs.values() for s in spec['sources'].values()}
    atom_review_statuses = {atom['review']['status'] for spec in specs.values() for atom in spec['atoms']}
    high_risk_review_pending = any(status != 'HUMAN_REVIEWED'
        for status in review_statuses | atom_review_statuses)
    return {'profile':PROFILE,'lineage_id':manifest['lineage_id'],'questions':len(cases),
            'artifact_hashes':{n:sha((directory/n).read_bytes()) for n in
                               ('manifest.json','cases.json','relations.restricted.json')},
            'partition':manifest['partition'],
            'review_policy':review_policy,
            'production_review_eligible':(exposure_snapshot['production_review_eligible']
                if exposure_snapshot is not None else False),
            'production_qualified':False,
            'high_risk_review_pending':high_risk_review_pending,
            'production_blocking_reasons':(exposure_snapshot['production_blocking_reasons']
                if exposure_snapshot is not None else ['FINAL_HOLDOUT_NOT_QUALIFIED']),
            'source_family_ids':sorted(family_ids),
            'exposure_statuses':sorted(exposure_statuses),
            'review_statuses':sorted(review_statuses),
            'production_qualified':False, 'review_scope':'explicit assertions only; coverage adjudication separately required'}


def audit_run(run_directory, fixture_directory):
    """Adapter for prepare/deliver captures; no new candidate code seam."""
    run, fixture = Path(run_directory), Path(fixture_directory)
    freeze = check_fixture(fixture)
    cases = _load(fixture/'cases.json'); specs = _load(fixture/'relations.restricted.json')
    rows = []
    for index, item in enumerate(cases, 1):
        spec = specs[item['qid']]
        sources = {sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
        arms = {}
        for arm in ('A','B'):
            path = run/f'case-{index:03}'/arm/'capture.json'
            arms[arm] = assess(item['case'],_load(path),spec,sources) if path.is_file() else {
                'status':'UNKNOWN','reason':'CAPTURE_MISSING','atoms':[],'profile':PROFILE}
        rows.append({'qid':item['qid'],'arms':arms})
    return {'profile':PROFILE,'fixture':freeze,'rows':rows,
            'all_required_present':all(r['arms'][a]['status']=='PRESENT' for r in rows for a in ('A','B'))}


def family_clusters(cases, specs):
    """Cluster by overlapping declared families, including transitive overlap."""
    parents = {item['qid']: item['qid'] for item in cases}
    def root(q):
        while parents[q] != q:
            q = parents[q]
        return q
    owners = {}
    for item in cases:
        q = item['qid']
        for source in specs[q]['sources'].values():
            family_ids = ((source['source_family_id'], '__UNKNOWN_SOURCE_FAMILY__')
                if source['relation'] == 'UNKNOWN' else (source['source_family_id'],))
            for family in family_ids:
                if family in owners:
                    a,b = sorted((root(q),root(owners[family])))
                    parents[b] = a
                owners[family] = q
    return {q:root(q) for q in parents}


def _resolve_record_path(value, base):
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def recompute_coverage(record):
    """Recompute the six-category evidence matrix from frozen fixtures/captures.

    Positive families come only from exact, reviewed source support alternatives.
    A negative control must pair a hash-bound complete positive capture with a
    capture that removes required members and consequently fails the same atom.
    Saved status booleans in the record are ignored.
    """
    if isinstance(record, (str, Path)):
        record_path = Path(record).resolve()
        record_data = _load(record_path)
        base = record_path.parent
    elif isinstance(record, dict):
        record_data = record
        base = Path(record_data.get('base_dir', '.')).resolve()
    else:
        raise ValueError('RELATIONS_COVERAGE_RECORD_INVALID')
    if record_data.get('schema') != 'rsi-source-relations-coverage/v1':
        raise ValueError('RELATIONS_COVERAGE_SCHEMA_REQUIRED')
    review_policy = record_data.get('review_policy', 'production-source-family/v1')
    if not isinstance(review_policy, str) or review_policy not in REVIEW_POLICIES:
        raise ValueError('RELATIONS_REVIEW_POLICY_INVALID')
    fixture_paths = record_data.get('fixtures')
    controls = record_data.get('controls')
    if not isinstance(fixture_paths, list) or not fixture_paths or not isinstance(controls, list):
        raise ValueError('RELATIONS_COVERAGE_INPUTS_REQUIRED')

    fixtures = {}
    freezes = {}
    positive = {category: {} for category in CATEGORIES}
    for fixture_value in fixture_paths:
        fixture = _resolve_record_path(fixture_value, base)
        freeze = check_fixture(fixture, review_policy=review_policy)
        freezes[str(fixture)] = freeze
        cases = _load(fixture / 'cases.json')
        specs = _load(fixture / 'relations.restricted.json')
        fixtures[str(fixture)] = (cases, specs)
        for item in cases:
            qid = item['qid']; spec = specs[qid]
            for atom in spec['atoms']:
                if atom['review']['status'] == 'UNREVIEWED':
                    continue
                families = {spec['sources'][support['source_id']]['source_family_id']
                            for support in atom['supports']}
                example = {'fixture':str(fixture), 'qid':qid, 'atom_id':atom['atom_id'],
                           'review_status':atom['review']['status'],
                           'source_family_ids':sorted(families)}
                for category in atom['categories']:
                    positive[category][(str(fixture), qid, atom['atom_id'])] = example

    negative = {category: {} for category in CATEGORIES}
    control_evidence = []
    for control in controls:
        fixture = _resolve_record_path(control.get('fixture', ''), base)
        fixture_key = str(fixture)
        if fixture_key not in fixtures:
            raise ValueError('RELATIONS_CONTROL_FIXTURE_UNREGISTERED')
        cases, specs = fixtures[fixture_key]
        qid, atom_id = control.get('qid'), control.get('atom_id')
        if qid not in specs:
            raise ValueError('RELATIONS_CONTROL_CASE_UNREGISTERED')
        spec = specs[qid]
        atoms = {atom['atom_id']: atom for atom in spec['atoms']}
        atom = atoms.get(atom_id)
        if atom is None or atom['review']['status'] == 'UNREVIEWED':
            raise ValueError('RELATIONS_CONTROL_ATOM_UNREVIEWED')
        case = next(item['case'] for item in cases if item['qid'] == qid)
        sources = {sid:(fixture / source['relative_path']).read_bytes()
                   for sid, source in spec['sources'].items()}
        positive_path = _resolve_record_path(control.get('positive_capture', ''), base)
        negative_path = _resolve_record_path(control.get('negative_capture', ''), base)
        positive_bytes = positive_path.read_bytes()
        negative_bytes = negative_path.read_bytes()
        if (sha(positive_bytes) != control.get('positive_sha256')
                or sha(negative_bytes) != control.get('negative_sha256')):
            raise ValueError('RELATIONS_CONTROL_CAPTURE_CHANGED')
        positive_capture = json.loads(positive_bytes.decode('utf-8'))
        negative_capture = json.loads(negative_bytes.decode('utf-8'))
        positive_result = assess(case, positive_capture, spec, sources)
        negative_result = assess(case, negative_capture, spec, sources)
        positive_ids = {member.get('id') for member in positive_capture.get('members', [])}
        negative_ids = {member.get('id') for member in negative_capture.get('members', [])}
        removed = control.get('removed_member_ids')
        if (positive_result['status'] != 'PRESENT' or negative_result['status'] != 'ABSENT'
                or not isinstance(removed, list) or not removed
                or len(set(removed)) != len(removed)
                or not set(removed) <= positive_ids - negative_ids):
            raise ValueError('RELATIONS_NEGATIVE_CONTROL_FAILED')
        family_ids = sorted({spec['sources'][support['source_id']]['source_family_id']
                             for support in atom['supports']})
        for category in atom['categories']:
            if (fixture_key, qid, atom_id) in positive[category]:
                negative[category][(fixture_key, qid, atom_id)] = {
                    'fixture':fixture_key, 'qid':qid, 'atom_id':atom_id,
                    'source_family_ids':family_ids,
                    'positive_capture_sha256':control['positive_sha256'],
                    'negative_capture_sha256':control['negative_sha256'],
                    'removed_member_ids':sorted(removed)}
        control_evidence.append({'fixture':fixture_key,'qid':qid,'atom_id':atom_id,
            'positive_status':positive_result['status'],'negative_status':negative_result['status'],
            'positive_capture_sha256':control['positive_sha256'],
            'negative_capture_sha256':control['negative_sha256']})

    matrix = {}
    for category in sorted(CATEGORIES):
        pos = positive[category]; neg = negative[category]
        positive_families = sorted({family for row in pos.values() for family in row['source_family_ids']})
        negative_families = sorted({family for row in neg.values() for family in row['source_family_ids']})
        missing_negative = sorted(set(positive_families) - set(negative_families))
        missing_examples = sorted([{'fixture':row['fixture'],'qid':row['qid'],'atom_id':row['atom_id']}
            for key,row in pos.items() if key not in neg], key=lambda row: (row['fixture'],row['qid'],row['atom_id']))
        ready = len(pos) >= 2 and len(positive_families) >= 2 and not missing_negative and not missing_examples
        matrix[category] = {'status':'PASS' if ready else 'BLOCKED_DATA',
            'positive_example_count':len(pos), 'positive_source_family_ids':positive_families,
            'negative_control_count':len(neg), 'negative_source_family_ids':negative_families,
            'positive_families_missing_negative_control':missing_negative,
            'positive_examples_missing_negative_control':missing_examples}
    status = 'PASS' if all(row['status'] == 'PASS' for row in matrix.values()) else 'BLOCKED_DATA'
    return {'schema':'rsi-source-relations-coverage-result/v1','status':status,
        'matrix':matrix,'fixture_freezes':freezes,'verified_negative_controls':control_evidence,
        'review_policy':review_policy,
        'production_qualified':False,
        'claim_scope':'source-interval support and member-removal controls only; no answer-quality claim'}


def replay_artifacts(capsule, fixture, artifacts, output, *, deadline, schema=PROFILE, scope='source-relations replay'):
    """Run unchanged prepare/deliver stages, then the explicit new evaluator.

    The caller performs launch admission and capsule setup. This adapter adds no
    tools or code-edit seam; the protected structural assessor still runs with
    no legacy fact claims, while new relational predicates supply the facts.
    """
    import time
    import uuid
    from .experiment import executor
    from .locking import atomic_write
    from .execution_evidence import resolve_policy
    fixture,output = Path(fixture),Path(output)
    frozen = check_fixture(fixture)
    policy = resolve_policy(getattr(capsule,'evidence_policy',None))
    if set(artifacts) != {'A','B'}:
        raise ValueError('TWO_ARTIFACT_ARMS_REQUIRED')
    output.mkdir(parents=True,exist_ok=False)
    manifest = {'schema':schema,'evaluator_profile':PROFILE,'fixture':frozen,
                'run_id':'relations-'+uuid.uuid4().hex,'scope':scope,
                'fixed':executor.FIXED,'arms':{},'execution_policy':policy.to_dict()}
    for arm, artifact in artifacts.items():
        atomic_write(output/(arm+'_worker.py'),artifact['worker'],exclusive=True)
        atomic_write(output/(arm+'.json'),artifact['config'],exclusive=True)
        manifest['arms'][arm] = {'candidate_id':artifact['candidate_id'],
            'worker_sha256':sha(artifact['worker']),'sha256':sha(artifact['config'])}
    atomic_write(output/'manifest.json',canonical(manifest),exclusive=True)
    rows = []
    for index,item in enumerate(_load(fixture/'cases.json'),1):
        if time.monotonic() >= deadline:
            break
        directory = output/f'case-{index:03}';directory.mkdir()
        case = executor.checked_case(item['case'])
        atomic_write(directory/'input.json',canonical(case),exclusive=True)
        capsule.deadline = min(deadline,time.monotonic()+300)
        raw = capsule.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory,
                            business_root=capsule.business_root)
        snapshot = executor.execution_view(raw)
        atomic_write(directory/'arm-input.json',canonical(snapshot),exclusive=True)
        for arm in ('A','B'):
            arm_dir = directory/arm;arm_dir.mkdir()
            capture = capsule.child('deliver',[directory/'arm-input.json',output/(arm+'.json'),arm_dir/'capture.json'],
                arm_dir,worker=output/(arm+'_worker.py'),business_root=capsule.business_root)
            if capture.get('upstream_sha256') != executor.fingerprint(snapshot):
                raise ValueError('RELATIONS_UPSTREAM_CAPTURE_MISMATCH')
        rows.append({'qid':item['qid'],'status':'PAIRED'})
    atomic_write(output/'results.json',canonical({'rows':rows}),exclusive=True)
    b = manifest['arms']['B']
    return read_decision(output,fixture,b['candidate_id'],{'angle':'P2_BUDGET','proposal_id':schema},
                         b['worker_sha256'],b['sha256'],evidence=policy)


def read_decision(run_directory, fixture_directory, candidate_id, proposal, worker_hash, config_hash, *, evidence=None):
    """Recompute relational and structural gates from hash-bound stage receipts."""
    from .experiment import executor, ROOT
    from .execution_evidence import resolve_policy,check_receipt
    from .errors import StageFailure
    from .adjudicate import evaluate_candidate
    run,fixture = Path(run_directory),Path(fixture_directory)
    freeze = check_fixture(fixture)
    manifest = _load(run/'manifest.json');policy = resolve_policy(evidence)
    b = manifest['arms']['B']
    if (manifest.get('evaluator_profile') != PROFILE or manifest['fixture'] != freeze
            or b != {'candidate_id':candidate_id,'worker_sha256':worker_hash,'sha256':config_hash}
            or manifest['execution_policy'] != policy.to_dict()):
        raise StageFailure('EVALUATION_ERROR','INVALID_PAIR',global_stop=True)
    cases = _load(fixture/'cases.json');specs = _load(fixture/'relations.restricted.json')
    clusters = family_clusters(cases,specs)
    stored = _load(run/'results.json')['rows']
    if len(stored) != len(cases) or [r['qid'] for r in stored] != [i['qid'] for i in cases]:
        raise StageFailure('EVALUATION_ERROR','PARTIAL_REPLAY',global_stop=True)
    runtime = None;rows = []
    for index,item in enumerate(cases,1):
        directory = run/f'case-{index:03}';case = item['case'];spec = specs[item['qid']]
        checked = executor.checked_case(case)
        if _load(directory/'input.json') != checked:
            raise StageFailure('EVALUATION_ERROR','INVALID_PAIR',global_stop=True)
        runtime = check_receipt(policy,directory/'execution-receipt.json',stage='prepare',
            worker_hash=sha((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),input_path=directory/'input.json',
            capture_path=directory/'snapshot.json',runtime_identity=runtime)
        snapshot = executor.execution_view(_load(directory/'snapshot.json'))
        if snapshot != _load(directory/'arm-input.json'):
            raise StageFailure('EVALUATION_ERROR','PREPARE_OUTPUT_MISMATCH',global_stop=True)
        row = {'qid':item['qid'],'source_group':clusters[item['qid']], 'pair_status':'PAIRED',
            'pair_identity':True,'execution':'COMPLETED','parent_faults':[],'candidate_faults':[],
            'fault_origin':'NONE','upstream_hash':executor.fingerprint(snapshot)}
        sources = {sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
        for arm,prefix in [('A','parent'),('B','candidate')]:
            cap = directory/arm/'capture.json'
            runtime = check_receipt(policy,cap.parent/'execution-receipt.json',stage='deliver',
                worker_hash=manifest['arms'][arm]['worker_sha256'],input_path=directory/'arm-input.json',
                capture_path=cap,config_path=run/(arm+'.json'),canonical_config_hash=manifest['arms'][arm]['sha256'],runtime_identity=runtime)
            capture = _load(cap)
            if capture.get('upstream_sha256') != row['upstream_hash']:
                raise StageFailure('EVALUATION_ERROR','INVALID_PAIR',global_stop=True)
            structural = executor.assess(case,capture,{'facts':[],'forbidden':[]},arm)
            relational = assess(case,capture,spec,sources)
            row[prefix+'_facts'] = {a['atom_id']:a['status'] for a in relational['atoms']}
            row[prefix+'_faults'] = structural['faults']
            row['provenance_'+arm] = relational['status']
            row['chars_'+arm] = structural['context_chars']
            row['context_'+arm+'_sha256'] = structural['context_sha256']
        if row['candidate_faults']:
            row['fault_origin'] = 'CANDIDATE'
        if runtime:
            row['execution_environment_hash'] = digest(runtime)
        rows.append(row)
    decision = evaluate_candidate({'angle':proposal['angle'],'proposal_id':proposal['proposal_id'],
        'candidate_id':candidate_id,'worker_hash':worker_hash,'config_hash':config_hash},rows,
        [i['qid'] for i in cases],total_questions=len(cases))
    decision.update(evaluator_profile=PROFILE,evidence_mode=policy.mode,real_eligible=policy.real_eligible,
        execution_policy=policy.to_dict(),fixture=freeze,
        execution_evidence_hash=digest({p.relative_to(run).as_posix():sha(p.read_bytes())
                                      for p in run.rglob('execution-receipt.json')}))
    return decision
