"""Compile accepted DEV Gold into exact native INDEX_PROJECTION fixtures.

Offline source-curator engineering, never admission or a reproduction of SSE.
Compilation gaps preserve the accepted semantic claims without weakening the
unchanged source-relations literal-support contract.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsik.events import canonical, digest
from rsik import source_relations as relations
from rsik import fixed_history_profile as profile
from scripts.build_rsi_qa500_history_sidecars import read_bound_line

CATEGORIES = {'邻近证据':'NONSEED_NECESSITY','跨块关系':'CROSS_MEMBER_RELATION',
              '否定与条件':'NEGATION_CONDITION','多来源支持':'MULTI_SOURCE_SUPPORT',
              '数值与归属':'UNIT_SCOPE_OWNERSHIP','引用支持':'CITATION_SUPPORT'}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def binding(path):
    path=Path(path).resolve(); raw=path.read_bytes()
    return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}


def bound(ref):
    if binding(ref['path'])['sha256'] != ref['sha256']:
        raise ValueError('COMPILER_INPUT_CHANGED')
    return load(ref['path'])


def projection(rows):
    """Exact native strings in native index order; no original raw-byte claim."""
    if len({r['id'] for r in rows})!=len(rows): raise ValueError('DUPLICATE_NATIVE_ID')
    if len({r['original_index'] for r in rows})!=len(rows): raise ValueError('NATIVE_INDEX_COLLISION')
    text='';spans={}
    for row in sorted(rows,key=lambda r:r['original_index']):
        if type(row['original_index']) is not int or row['original_index']<0:
            raise ValueError('NATIVE_INDEX_INVALID')
        content=row['content']
        if hashlib.sha256(content.encode()).hexdigest()!=row['content_sha256']:
            raise ValueError('NATIVE_CONTENT_TAMPER')
        if text:text+='\n\n'
        start=len(text);text+=content;spans[row['id']]={'start':start,'end':len(text)}
    return text.encode('utf-8'),spans


def exact_quote_matches(record, rows):
    """No quote normalization, inferred fragments, ID or index invention."""
    matches=[]
    for quote in record['quote_fields']:
        text=quote['text']
        if hashlib.sha256(text.encode()).hexdigest()!=quote['text_sha256']:
            raise ValueError('GOLD_QUOTE_TAMPER')
        if not text:continue
        for row in rows:
            for surface,transform in ((text,None),(text.replace('\n','\\n'),'newline-to-literal-backslash-n/v1')):
                if transform and surface==text:continue
                start=row['content'].find(surface)
                if start>=0:matches.append({'member_id':row['id'],'start':start,'end':start+len(surface),
                    'text':surface,'field_pointer':quote['field_pointer'],'quote_sha256':quote['text_sha256'],
                    'literal_transform':transform,'surface_sha256':hashlib.sha256(surface.encode()).hexdigest()})
    return matches


def literal_components(atom):
    original=atom['components'];out={}
    for key,value in original.items():
        if value is None or value=={}:continue
        if key in relations.COMPONENTS and isinstance(value,str) and value:out[key]=value
        elif key=='ownership':out['scope']=value
        elif key=='qualifiers':
            # Concatenating independent qualifiers would invent a literal source span.
            if value:raise ValueError('QUALIFIERS_REQUIRE_REVIEWED_LITERAL_SCOPE')
        else:raise ValueError('UNSUPPORTED_TYPED_COMPONENT')
    return out


def compile_atom(atom, evidence, spans, sources, case, now):
    gaps=[];supports=[];alternatives=[]
    try:components=literal_components(atom)
    except ValueError as exc:return None,[{'code':str(exc)}]
    for alternative in atom['support_alternatives']:
        if alternative.get('join')!='AND':raise ValueError('GOLD_ALTERNATIVE_NOT_AND')
        selected=[]
        for ref in alternative['evidence_refs']:
            matches=evidence.get(ref['record_ref'],[])
            if not matches:
                gaps.append({'code':'EXACT_NATIVE_QUOTE_MISSING','record_ref':ref['record_ref']});continue
            # One exact match is an evidence alternative, not a requirement to retain
            # redundant parent+child copies. Prefer actual shorter child evidence.
            match=min(matches,key=lambda m:(m['native_length'],m['member_id']))
            loc=spans[match['member_id']]
            binds=[key for key,value in components.items() if value in match['text']]
            if not binds:
                gaps.append({'code':'NO_LITERAL_COMPONENT_IN_GOLD_QUOTE','record_ref':ref['record_ref']});continue
            support={'support_id':'s'+str(len(supports)),'member_id':match['member_id'],
                'source_id':loc['source_id'],'start':loc['start']+match['start'],
                'end':loc['start']+match['end'],'text':match['text'],'binds':binds,
                'gold_record_ref':ref['record_ref'],'gold_quote_sha256':match['quote_sha256']}
            supports.append(support);selected.append(support['support_id'])
        union={key for s in supports if s['support_id'] in selected for key in s['binds']}
        missing=sorted(set(components)-union)
        if missing:gaps.append({'code':'TYPED_COMPONENT_NOT_LITERAL_IN_AND_SUPPORT','components':missing})
        alternatives.append(selected)
    candidate={'atom_id':atom['atom_id'],'components':components,
        'categories':[CATEGORIES[c] for c in atom['coverage_categories']],
        'equivalence_rule':'EXACT_REVIEWED_SUPPORT_ALTERNATIVES','supports':supports,
        'support_alternatives':alternatives,'citation_required':True,
        'accepted_gold_claim':deepcopy(atom),'semantic_authority':'USER_ACCEPTED_GOLD',
        'engineering_review_scope':'Exact compilation only; no invented semantic review'}
    candidate['review']={'status':'AI_REVIEWED','reviewer':'DEV Gold exact fixture compiler',
        'reviewed_at':now,'subject_sha256':digest(candidate)}
    if not gaps:
        try:relations.validate_spec(case,{'profile':relations.PROFILE,'lineage_id':'compile',
            'sources':{k:v[0] for k,v in sources.items()},'member_spans':spans,'atoms':[candidate]},
            {k:v[1] for k,v in sources.items()})
        except ValueError as exc:gaps.append({'code':str(exc)})
    return candidate,gaps


def compile_literal_claim(canonical_atom, claim, native, spans, qid, records, now):
    """Reviewed source predicates AND-linked to unchanged canonical Gold claim."""
    if claim.get('canonical_predicate_join')!='AND' or not claim.get('predicates'):
        raise ValueError('CANONICAL_CLAIM_PREDICATE_AND_REQUIRED')
    out=[]
    for definition in claim['predicates']:
        atom={'atom_id':canonical_atom['atom_id']+'::'+definition['predicate_id'],
            'components':deepcopy(definition['components']),
            'categories':[CATEGORIES.get(c,c) for c in definition['coverage_categories']],
            'supports':[],'support_alternatives':deepcopy(definition['presence_clauses']),
            'equivalence_rule':'EXACT_REVIEWED_SUPPORT_ALTERNATIVES','citation_required':True,
            'canonical_claim_id':canonical_atom['atom_id'],'canonical_claim_sha256':digest(canonical_atom),
            'canonical_predicate_join':'AND','accepted_gold_claim':deepcopy(canonical_atom)}
        for support in definition['supports']:
            mid=support['member_id'];row=native[mid];start=support['start'];end=support['end']
            if row['content'][start:end]!=support['text'] or not 0<=start<end<=len(row['content']):
                raise ValueError('LITERAL_MAPPING_NATIVE_SPAN_CHANGED')
            loc=spans[mid]
            atom['supports'].append({**deepcopy(support),'source_id':loc['source_id'],
                'start':loc['start']+start,'end':loc['start']+end})
        # Literal anchors may complement a quoted support, but every attributed
        # Gold evidence ordinal must keep its whole source-surface quote present.
        by_id={s['support_id']:s for s in atom['supports']}
        for clause in atom['support_alternatives']:
            selected=[by_id[sid] for sid in clause]
            for ordinal in {o for s in selected for o in s.get('evidence_ordinals',[])}:
                ref=f'{qid}:evidence[{ordinal}]';record=records[ref]
                if not any(m['text'] in s['text'] for s in selected
                        for m in exact_quote_matches(record,[native[s['member_id']]])):
                    raise ValueError('LITERAL_MAPPING_DROPS_COMPLETE_GOLD_QUOTE')
        atom['review']={'status':'AI_REVIEWED','reviewer':'Accepted Gold literal-source curator + exact compiler',
            'reviewed_at':now,'subject_sha256':digest(atom)}
        out.append(atom)
    return out


def expanded_native_ids(case, seeds):
    """Static mirror of frozen whole-member expansion, cap4000 PER group."""
    rows=case['chunks'];by_id={r['id']:r for r in rows};included=set()
    for mid in seeds:
        row=by_id[mid];siblings=sorted([r for r in rows if row.get('parent_id')
            and r.get('parent_id')==row.get('parent_id') and r['file_id']==row['file_id']],key=lambda r:r['original_index'])
        chosen=[row] if not siblings else siblings
        if len('\n'.join(r['content'] for r in chosen))>4000:
            chosen=[]
            if len(row['content'])<=4000:
                chosen=[row];used=len(row['content']);index=siblings.index(row) if siblings else 0
                for other in sorted((r for r in siblings if r['id']!=mid),key=lambda r:(abs(siblings.index(r)-index),r['original_index'])):
                    if used+len(other['content'])+1<=4000:chosen.append(other);used+=len(other['content'])+1
        if not chosen:continue
        included.update(r['id'] for r in chosen)
        ordered=sorted((r for r in rows if r['file_id']==row['file_id']),key=lambda r:r['original_index'])
        index=ordered.index(row)
        included.update(ordered[i]['id'] for i in (index-1,index+1) if 0<=i<len(ordered))
    return included


def deliverable_seed_ids(case, spec, preferred):
    """Choose real native delivery seeds that expand to every required support.

    Preferred Gold-map seeds can be parents above the protected 4000-char group
    cap or can leave a second source file unseeded.  Prefer a real non-support
    sibling/neighbour as seed so the fact chunk remains non-seed and removable;
    never split, copy or rewrite a native member.
    """
    needed={s['member_id'] for a in spec['atoms'] for s in a['supports']}
    by_id={r['id']:r for r in case['chunks']}
    seeds={s for s in preferred if s in by_id}
    if not needed:return sorted(seeds),'NO_SUPPORT_MEMBERS'
    def covers(seedset):return needed<=expanded_native_ids(case,sorted(seedset))
    if covers(seeds) and not any(s in needed for s in seeds):return sorted(seeds),'PREFERRED_SEEDS_DELIVER_ALL_NONSEED'
    for fid in sorted({by_id[m]['file_id'] for m in needed}):
        need_fid={m for m in needed if by_id[m]['file_id']==fid}
        file_seeds={s for s in seeds if by_id[s]['file_id']==fid}
        if covers(seeds) and not (file_seeds&need_fid):continue
        rows=sorted((r for r in case['chunks'] if r['file_id']==fid),key=lambda r:r['original_index'])
        candidates=[r for r in rows if len(r['content'])<=4000]
        candidates.sort(key=lambda r:(r['id'] in needed,r['original_index']))
        for candidate in candidates:
            trial=(seeds-file_seeds)|{candidate['id']}
            if need_fid<=expanded_native_ids(case,sorted(trial)):seeds=trial;break
    if covers(seeds):
        return sorted(seeds),'REAL_NATIVE_DELIVERABLE_SEED'
    return sorted(preferred),'NO_DELIVERABLE_SEED'


def native_seed_variant(case, spec, direction):
    """Only real prior/later members; conservative fallback recorded explicitly."""
    needed={s['member_id'] for a in spec['atoms'] for s in a['supports']}
    by_id={r['id']:r for r in case['chunks']};seeds=set()
    for mid in sorted(needed):
        row=by_id[mid];ordered=sorted((r for r in case['chunks'] if r['file_id']==row['file_id']),key=lambda r:r['original_index'])
        index=ordered.index(row);other=index-1 if direction=='forward' else index+1
        if not 0<=other<len(ordered):return case['seed_ids'],'NO_REAL_'+direction.upper()+'_ANCHOR'
        seed=ordered[other]
        if len(seed['content'])>4000:return case['seed_ids'],'ANCHOR_EXCEEDS_PER_GROUP_CAP'
        seeds.add(seed['id'])
    proposal=sorted(seeds)
    if not needed<=expanded_native_ids(case,proposal):return case['seed_ids'],'REQUIRED_SUPPORT_NOT_REACHABLE'
    if any('NONSEED_NECESSITY' in a['categories'] and any(
            not {s['member_id'] for s in a['supports'] if s['support_id'] in alt}-seeds
            for alt in a['support_alternatives']) for a in spec['atoms']):
        return case['seed_ids'],'REQUIRED_NONSEED_NECESSITY_LOST'
    return proposal,'REAL_NATIVE_ADJACENCY_CHANGED' if proposal!=case['seed_ids'] else 'UNCHANGED_NATIVE_ANCHORS'


def emit_suites(output,cases,specs,histories,projections,paraphrases,refs,lineage,control_ids,now):
    reviewed=load(paraphrases)
    if reviewed.get('schema')!='rsi-frozen-dev-query-paraphrases/v1':raise ValueError('PARAPHRASE_PROFILE_INVALID')
    variants={r['item_id']:r for r in reviewed['variants']}
    if set(variants)!={r['qid'] for r in cases}:raise ValueError('PARAPHRASE_SELECTED20_CHANGED')
    receipts={}
    for suite in ('main','question-paraphrase','forward-seed','backward-seed'):
        directory=output/suite;directory.mkdir();suite_cases=deepcopy(cases);suite_specs=deepcopy(specs)
        derivations={};geometry=[];controls=[];missing_negative=[]
        for item in suite_cases:
            qid=item['qid'];case=item['case']
            if suite=='question-paraphrase':
                variant=variants[qid];review=variant['review'];base=hashlib.sha256(case['question'].encode()).hexdigest()
                current=hashlib.sha256(variant['question'].encode()).hexdigest()
                if (variant['base_question_sha256']!=base or variant['current_question_sha256']!=current
                        or review['subject_sha256']!=current or review['status'] not in {'AI_REVIEWED','HUMAN_REVIEWED'}):
                    raise ValueError('PARAPHRASE_REVIEW_BINDING_CHANGED')
                derivation={'profile':'reviewed-fixed-query-variant/v1','base_question_sha256':base,
                    'current_question_sha256':current,'original_review':review,'origin':binding(paraphrases),
                    'original_variant_sha256':digest(variant)}
                derivation['review']={'status':'AI_REVIEWED','subject_sha256':digest(derivation),
                    'reviewer':'Exact wrapper of reviewed DEV question variant','reviewed_at':now}
                derivations[qid]=derivation;case['question']=variant['question']
            if qid in control_ids:continue
            if suite in ('forward-seed','backward-seed'):
                before=case['seed_ids'];seeds,reason=native_seed_variant(case,suite_specs[qid],suite.split('-')[0])
                case['seed_ids']=seeds;case['expected_seed_count']=len(seeds)
                geometry.append({'qid':qid,'changed':before!=seeds,'reason':reason,
                    'original_seeds':before,'variant_seeds':seeds,
                    'direction_claim':'REAL_NATIVE_ANCHOR_SIDE_ONLY; parent siblings may also reach supports'})
            for atom in suite_specs[qid]['atoms']:
                nonseed={s['member_id'] for s in atom['supports']}-set(case['seed_ids'])
                by_id={s['support_id']:s for s in atom['supports']}
                if not nonseed or any(not {by_id[s]['member_id'] for s in alt}&nonseed
                        for alt in atom['support_alternatives']):
                    missing_negative.append({'qid':qid,'atom_id':atom['atom_id'],
                        'reason':'REQUIRED_PREDICATE_PRESENT_IN_MANDATORY_SEED_ALTERNATIVE'})
                else:controls.append({'qid':qid,'atom_id':atom['atom_id'],'removed_member_ids':sorted(nonseed),
                    'operation':'REAL_DELIVER_MEMBER_REMOVAL','expected_positive':'PRESENT','expected_negative':'ABSENT'})
        for name,data in projections.items():(directory/name).parent.mkdir(exist_ok=True);(directory/name).write_bytes(data)
        for ref in histories.values():
            dest=directory/ref['path'];dest.parent.mkdir(exist_ok=True);dest.write_bytes((output/ref['path']).read_bytes())
        mapping={qid:{claim['atom_id']:[p['atom_id'] for p in suite_specs[qid]['atoms']
            if p['canonical_claim_id']==claim['atom_id']] for claim in
            {p['canonical_claim_id']:p['accepted_gold_claim'] for p in suite_specs[qid]['atoms']}.values()} for qid in suite_specs}
        artifacts={'cases.json':suite_cases,'relations.restricted.json':suite_specs,
            'control-spec.restricted.json':controls,'canonical-claim-source-predicates.restricted.json':mapping,
            'seed-geometry.restricted.json':geometry,'negative-control-gaps.restricted.json':missing_negative,
            'preregistration.restricted.json':{'scope':'EXPLICIT_GOLD_NATIVE_SOURCE_ONLY_REGRESSION',
                'historical_sse_reproduction':False,'production_retrieval_reproduction':False,'partition':'DEV',
                'lineage_id':lineage,'inputs':refs,'query_review':binding(paraphrases),'same20_origins':True,
                'candidate_strategy_authored':False,'execution_status':'NOT_EXECUTED'}}
        for name,value in artifacts.items():(directory/name).write_bytes(canonical(value))
        hashes={p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}
        manifest={'profile':profile.PROFILE,'history_profile':profile.HISTORY_PROFILE,'partition':'DEV',
            'lineage_id':lineage,'suite_variant':suite,'history_sidecars':histories,'query_derivations':derivations,
            'business_controls':{qid:{'expected_status':'INSUFFICIENT_EVIDENCE','expected_provider_requests':0} for qid in sorted(control_ids)},
            'artifact_hashes':hashes,'case_domain_counts':{'retrieval_questions':17,'business_controls':3,
                'actual_history_questions':6,'empty_history_questions':14},'review_status':'AI_REVIEWED',
            'production_qualified':False,'independent_question_origins':20,
            'fixture_scope':'EXPLICIT_GOLD_NATIVE_SOURCE_ONLY_REGRESSION_NOT_HISTORICAL_SSE_REPRODUCTION'}
        (directory/'manifest.json').write_bytes(canonical(manifest))
        freeze=profile.check_fixture(directory,review_policy='research-source-family/v1')
        receipts[suite]={'path':str(directory),'freeze':freeze,'negative_control_gaps':len(missing_negative),
            'seed_changed_questions':sum(r['changed'] for r in geometry),'manifest_sha256':binding(directory/'manifest.json')['sha256']}
    return receipts


def build(inputs, output, paraphrases=None):
    """Build fresh four-suite lineage, or an explicit preserved20 compiler gap."""
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    refs={k:binding(v) for k,v in inputs.items() if k!='histories'}
    atoms=load(inputs['atoms']);normalized=load(inputs['support']);native_map=load(inputs['map'])
    literal=load(inputs['literal']) if 'literal' in inputs and Path(inputs['literal']).exists() else None
    literal_items={r['item_id']:r for r in literal['items']} if literal else {}
    if literal:
        if literal.get('schema')!='rsi-gold-literal-source-components/v1':raise ValueError('LITERAL_PROFILE_INVALID')
        for ref in literal.get('inputs',{}).values():
            if isinstance(ref,dict) and 'path' in ref and binding(ref['path'])['sha256']!=ref['sha256']:
                raise ValueError('LITERAL_INPUT_CHANGED')
    acceptance=load(inputs['acceptance'])
    if acceptance.get('authority')!='USER_CONFIRMED_ALREADY_CHECKED_USE_DIRECTLY':
        raise ValueError('USER_ACCEPTED_GOLD_REQUIRED')
    for document in (atoms,normalized,native_map):
        for ref in document.get('origin_bindings',{}).values():
            if (isinstance(ref,dict) and 'path' in ref and 'sha256' in ref
                    and binding(ref['path'])['sha256']!=ref['sha256']):
                raise ValueError('COMPILER_INPUT_CHANGED')
    audit=bound(normalized['origin_bindings']['passive_audit'])
    requests={r['item_id']:r for r in load(inputs['requests'])['request_rows']}
    ids=normalized['items']
    if len(ids)!=20 or set(ids)!=set(audit['item_ids']):raise ValueError('SELECTED_DEV20_CHANGED')
    native={};files={};capture_provenance={}
    for key in ('enterprise','legacy'):
        capture=load(inputs[key])
        for file in capture['file_documents']:
            fid=file.get('file_id',file.get('id'));files[fid]={**file,'id':fid}
        for row in capture['document_chunks']:
            if row['id'] in native and native[row['id']]!=row:raise ValueError('NATIVE_CAPTURE_COLLISION')
            native[row['id']]=row;capture_provenance[row['id']]={'capture':refs[key],'row_sha256':digest(row)}
    map_records={r['support_record_ref']:r for r in native_map['records']}
    support_records={f"{r['item_id']}:evidence[{r['evidence_ordinal']}]":r for r in normalized['support_records']}
    definitions={r['item_id']:r for r in atoms['retrieval_items']}
    control_ids={r['item_id'] for r in atoms['business_controls']}
    if set(definitions)|control_ids!=set(ids) or len(definitions)!=17 or len(control_ids)!=3:
        raise ValueError('DEV_DOMAIN_COUNTS_CHANGED')
    now=datetime.now(timezone.utc).isoformat();lineage='qa300-gold-native-'+digest(refs)[:16]
    cases=[];specs={};gaps=[];projections={};history_refs={};evidence_receipts=[];seed_receipts=[]
    for qid in ids:
        sidepath=Path(inputs['histories'])/(qid+'.history.restricted.json');sidecar=load(sidepath)
        question=read_bound_line(sidecar['current_input'])['question']
        profile.validate_history(sidecar,qid=qid,question=question)
        history_refs[qid]={'path':'histories/'+sidepath.name,'sha256':binding(sidepath)['sha256']}
        (output/'histories').mkdir(exist_ok=True);(output/history_refs[qid]['path']).write_bytes(sidepath.read_bytes())
        case={'question':question,'allowed_kb_ids':[], 'files':[],'chunks':[], 'seed_ids':[], 'expected_seed_count':0}
        if qid in control_ids:
            case['allowed_kb_ids']=list(requests[qid]['kb_ids'])
            if not case['allowed_kb_ids']:raise ValueError('BUSINESS_CONTROL_KB_SCOPE_REQUIRED')
            cases.append({'qid':qid,'case':case});continue
        item=next(i for i in audit['items'] if i['item_id']==qid);selected_files=set();evidence={}
        literal_claims={c['canonical_atom_id']:c for c in literal_items.get(qid,{}).get('claims',[])}
        if literal and set(literal_claims)!={a['atom_id'] for a in definitions[qid]['atoms']}:
            raise ValueError('LITERAL_MAPPING_DROPS_CANONICAL_CLAIM')
        selected_files.update(native[s['member_id']]['file_id'] for claim in literal_claims.values()
            for predicate in claim['predicates'] for s in predicate['supports'])
        required={r['record_ref'] for a in definitions[qid]['atoms'] for alt in a['support_alternatives'] for r in alt['evidence_refs']}
        for ref in sorted(required):
            record=support_records[ref];mapped=map_records[ref]
            file_ids={r['file_id'] for r in mapped['native_capture_refs']}
            # Accepted passive source hash is a binding fallback for sourceDoc aliases.
            eid=record['gold_identity_fields'].get('evidence_id')
            raw_hashes={s['local_binding']['sha256'] for s in item['sources']
                if s.get('evidence_id')==eid and s.get('local_binding')}
            file_ids|={fid for fid,file in files.items() if file.get('sha256') in raw_hashes}
            candidates=[r for r in native.values() if r['file_id'] in file_ids]
            matches=exact_quote_matches(record,candidates)
            for match in matches:match['native_length']=len(native[match['member_id']]['content'])
            evidence[ref]=matches;selected_files.update(native[m['member_id']]['file_id'] for m in matches)
            evidence_receipts.append({'qid':qid,'record_ref':ref,'matching_member_ids':[m['member_id'] for m in matches],
                'accepted_raw_source_hashes':sorted(raw_hashes),'matched':bool(matches)})
        spans={};sources={}
        for fid in sorted(selected_files):
            rows=[r for r in native.values() if r['file_id']==fid];data,positions=projection(rows)
            sid='INDEX_PROJECTION:'+fid;rel='sources/'+fid+'.txt';projections[rel]=data
            file=files[fid]
            gold_sources=[s for s in item['sources'] if s.get('file_id')==fid or
                file.get('sha256') and s.get('local_binding',{}).get('sha256')==file['sha256']]
            raw_hashes=sorted({s.get('expected_source_sha256') or s.get('local_binding',{}).get('sha256')
                for s in gold_sources}-{None})
            identities=sorted({s.get('document_id') or s.get('file_id') for s in gold_sources}-{None})
            record={'content_sha256':hashlib.sha256(data).hexdigest(),
                'source_family_id':'accepted-gold-source:'+('|'.join(identities) or fid),'document_id':fid,'version':'sha256:'+refs['enterprise' if file.get('active_build_id') else 'legacy']['sha256'],
                'relation':'DERIVED_FROM','related_source_ids':['raw:'+h for h in raw_hashes] or ['accepted-gold-native:'+fid],
                'exposure_status':'KNOWN_HISTORY','review_status':'AI_REVIEWED','file_id':fid,'kb_id':file['kb_id'],
                'relative_path':rel,'projection_profile':'canonical-utf8-native-index-projection/v1',
                'original_gold_raw_references':gold_sources,'accepted_canonical_source_identities':identities,
                'raw_original_bytes_claim':False,'native_member_bindings':{r['id']:capture_provenance[r['id']] for r in rows}}
            sources[sid]=(record,data)
            for mid,loc in positions.items():spans[mid]={'source_id':sid,**loc}
            case['files'].append({'id':fid,'kb_id':file['kb_id'],'name':file['name']})
            case['chunks'].extend(deepcopy(rows));case['allowed_kb_ids'].append(file['kb_id'])
        case['allowed_kb_ids']=sorted(set(case['allowed_kb_ids']))
        # Preserve actual native starts. Geometry is reported; it cannot manufacture
        # nonseed necessity where an accepted Gold start already contains the fact.
        seed_ids={r['chunk_id'] for ref in required for r in map_records[ref]['native_capture_refs']
            if r['match_method']=='DIRECT_GOLD_NATIVE_CHUNK_ID' and r['chunk_id'] in spans}
        if not seed_ids:
            seed_ids={min((native[m['member_id']] for m in matches),key=lambda r:(len(r['content']),r['original_index']))['id']
                for matches in evidence.values() if matches}
        case['seed_ids']=sorted(seed_ids);case['expected_seed_count']=len(seed_ids)
        compiled=[]
        for atom in definitions[qid]['atoms']:
            if atom['atom_id'] in literal_claims:
                try:
                    candidates=compile_literal_claim(atom,literal_claims[atom['atom_id']],native,spans,qid,support_records,now)
                    compiled.extend(candidates);errors=[]
                except ValueError as exc:errors=[{'code':str(exc)}]
            else:
                candidate,errors=compile_atom(atom,evidence,spans,sources,case,now)
                if candidate:compiled.append(candidate)
            gaps.extend({'qid':qid,'atom_id':atom['atom_id'],**error} for error in errors)
        specs[qid]={'profile':relations.PROFILE,'lineage_id':lineage,'citation_binding_profile':'numbered-delivery/v1',
            'sources':{k:v[0] for k,v in sources.items()},'member_spans':spans,'atoms':compiled}
        cases.append({'qid':qid,'case':case})
        if compiled:
            preferred=list(case['seed_ids'])
            selected,seed_reason=deliverable_seed_ids(case,specs[qid],preferred)
            case['seed_ids']=selected;case['expected_seed_count']=len(selected)
            seed_receipts.append({'qid':qid,'preferred_seed_ids':preferred,'selected_seed_ids':selected,
                'reason':seed_reason,'policy':'REAL_NATIVE_DELIVERABLE_SEED/v1'})
            if seed_reason=='NO_DELIVERABLE_SEED':
                gaps.append({'qid':qid,'code':'MAIN_NO_DELIVERABLE_NATIVE_SEED'})
            try:relations.validate_spec(case,specs[qid],{k:v[1] for k,v in sources.items()})
            except ValueError as exc:gaps.append({'qid':qid,'code':str(exc)})
        if not {s['member_id'] for a in compiled for s in a['supports']}<=expanded_native_ids(case,case['seed_ids']):
            gaps.append({'qid':qid,'code':'MAIN_REQUIRED_SUPPORT_NOT_REACHABLE'})
    if paraphrases is None:gaps.append({'code':'REVIEWED20_QUESTION_PARAPHRASES_NOT_SUPPLIED'})
    for rel,data in projections.items():(output/rel).parent.mkdir(exist_ok=True);(output/rel).write_bytes(data)
    for name,value in [('compiled-cases.restricted.json',cases),('compiled-relations.restricted.json',specs),
            ('evidence-compilation.restricted.json',evidence_receipts)]:
        (output/name).write_bytes(canonical(value))
    result={'schema':'rsi-qa300-fixed-history-build/v1','status':'BLOCKED_COMPILE_GAPS' if gaps else 'COMPILED_NOT_EXECUTED',
        'scope':'EXPLICIT_GOLD_NATIVE_SOURCE_ONLY_REGRESSION_NOT_HISTORICAL_SSE_REPRODUCTION',
        'lineage_id':lineage,'partition':'DEV','selected_ids':ids,'retrieval_questions':17,'business_controls':3,
        'accepted_claim_count':sum(len(r['atoms']) for r in definitions.values()),'inputs':refs,
        'history_sidecars':history_refs,'gaps':gaps,'gap_count':len(gaps),'execution_status':'NOT_EXECUTED',
        'seed_policy':seed_receipts,
        'production_qualified':False,'fixtures_emitted':False,'source_projection_count':len(projections),
        'evidence_refs':len(evidence_receipts),'matched_evidence_refs':sum(r['matched'] for r in evidence_receipts)}
    if not gaps:
        result['suites']=emit_suites(output,cases,specs,history_refs,projections,paraphrases,refs,lineage,control_ids,now)
        result['fixtures_emitted']=True
    (output/'build-receipt.restricted.json').write_bytes(canonical(result));return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input-root',required=True);parser.add_argument('--histories',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--paraphrases');args=parser.parse_args()
    base=Path(args.input_root);clean=base/'dev-fact-preparation-clean'
    inputs={'atoms':clean/'necessary-fact-atoms.v3.json','support':clean/'gold-support-normalized.v2.json',
        'map':clean/'evidence-native-capture-map.v1.json','acceptance':base/'user-input-acceptance.v1.json',
        'enterprise':base/'dev-parsed-capture/capture.stdout.restricted.json','legacy':base/'gold-native-capture/capture.restricted.json',
        'histories':Path(args.histories),'requests':base/'selection-audit/dev20-request-whitelisted-metadata.v1.json'}
    if (clean/'literal-source-components.v1.json').exists():inputs['literal']=clean/'literal-source-components.v1.json'
    result=build(inputs,args.output,args.paraphrases)
    print(json.dumps({k:result[k] for k in ('status','gap_count','accepted_claim_count','evidence_refs','matched_evidence_refs','fixtures_emitted')}))
    sys.exit(2 if result['gaps'] else 0)
