"""E2.1 frozen B/C context experiment: no retrieval, expansion or Provider calls."""
import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import socket
import sys
import time
import uuid
from unittest.mock import MagicMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from e21_context_selection import RULE_VERSION, grounded_messages, select_members

def canon(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def sha(v):return hashlib.sha256(v if isinstance(v,bytes) else v.encode()).hexdigest()
def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def write(p,value):
    with p.open('x',encoding='utf8') as f:f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def deny(*a,**kw):raise RuntimeError('E21_NO_NETWORK')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output-dir',type=Path,required=True);args=ap.parse_args()
    socket.getaddrinfo=deny;socket.create_connection=deny;socket.socket.connect=deny
    os.environ.update(DATABASE_URL='sqlite://',RAG_OBSERVABILITY_ENABLED='false',RAG_SNAPSHOTS_ENABLED='false')
    sys.modules.setdefault('chromadb',MagicMock());sys.modules.setdefault('chromadb.config',MagicMock())
    from app.services.context_builder import build_chat_context
    # Extract the actual pure nested formatter, rather than replacing source positioning rules.
    tree=ast.parse((ROOT/'backend/app/services/chat.py').read_text(encoding='utf-8-sig'))
    formatter=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='format_citation')
    scope={'PurePath':PurePath};exec(compile(ast.Module(body=[formatter],type_ignores=[]),'<actual-format-citation>','exec'),scope)
    out=args.output_dir;out.mkdir(parents=False,exist_ok=False)
    old=ROOT/'var/e20-20261030'; stage1=ROOT/'var/e2-1'
    frozen=load(old/'research-frozen-contexts.json');original=load(old/'run-delivery-ab-final/evidence.json')
    manifest=load(stage1/'stage1-manifest.json')
    for p,digest in manifest['input_hashes'].items():assert sha((ROOT/p).read_bytes())==digest
    proposals=[json.loads(s) for s in (stage1/'member-quality-ledger.jsonl').read_text(encoding='utf8').splitlines()]
    assert sha((stage1/'member-quality-ledger.jsonl').read_bytes())==manifest['ledger_sha256']
    rows=[];details=[];paired=[]
    for q in frozen['questions']:
        before=next(e for e in original if e['qid']==q['qid'] and e['arm']=='B')
        expanded=copy.deepcopy(before['expanded']);expanded_hash=sha(canon(expanded))
        initial=[c['id'] for c in q['candidates']]
        snapshot_hash=sha(canon({'question':q['question'],'initial_candidates':q['candidates'],'expanded':expanded}))
        citations,b_summary=build_chat_context(expanded,scope['format_citation'])
        assert citations==before['citations'], 'B citation/provenance regression'
        assert grounded_messages(citations,before['provider_messages'][1:])==before['provider_messages'], 'B prompt regression'
        # Freeze all currently observable target-source structure spans before selecting C.
        targets={r['member_id']:r for r in proposals if r['qid']==q['qid'] and r['source_file_hash']==q['source_sha256']}
        structural=[{'member_id':r['member_id'],**s} for r in targets.values() for s in r['structure_features']]
        unknown={r['member_id'] for r in proposals if r['qid']==q['qid'] and 'UNKNOWN' in r['relevance_labels']}
        gold_ids={r['member_id'] for r in targets.values() if r['gold_evidence_units']}
        arms={}
        for arm in ['B','C']:
            started=time.perf_counter()
            if arm=='B':
                selected=copy.deepcopy(citations);decision={'selection_rule_version':'e20-expanded-content-baseline',
                  'scope_status':'BASELINE','selected_evidence_ids':[c['chunk_id'] for c in selected],
                  'excluded_evidence_ids':[], 'decisions':[{'member_id':c['chunk_id'],'selected':True,'reason_code':'BASELINE_KEEP'} for c in selected]}
            else:selected,decision=select_members(q['question'],citations,initial)
            messages=grounded_messages(selected,before['provider_messages'][1:]);delivered={c['chunk_id']:c for c in selected}
            message_text='\n'.join(m['content'] for m in messages)
            assert all(c['content'] in message_text for c in selected)
            retained_structures=[s for s in structural if s['member_id'] in delivered and
                delivered[s['member_id']]['content'][s['start']:s['end']]==s['excerpt']]
            all_source_preserved=all(mid in delivered and delivered[mid]['content_hash']==r['content_hash'] for mid,r in targets.items())
            seeds_kept=all(mid in delivered for mid in initial)
            unknown_kept=unknown.issubset(delivered)
            covered=gold_ids.intersection(delivered)
            assert all_source_preserved and seeds_kept and unknown_kept and covered==gold_ids
            assert len(retained_structures)==len(structural)
            row={'qid':q['qid'],'dataset_version':'e20-frozen-e1-office-replay-v1','evaluation_version':'e21-bc-context-v1',
                 'run_id':'e21-'+uuid.uuid4().hex,'experiment_id':'e21-bc-zero-model-v1','arm':arm,
                 'code_hash':manifest['frozen_code_hash'],'index_fingerprint':manifest['index_fingerprint'],
                 'index_fingerprint_scope':manifest['index_scope'],'input_snapshot_hash':snapshot_hash,
                 'expanded_snapshot_hash':expanded_hash,'context_hash':sha('\n\n'.join(c['content'] for c in selected)),
                 'messages_hash':sha(canon(messages)),'seed_evidence_ids':[g['id'] for g in expanded],
                 'initial_candidate_ids':initial,'expanded_evidence_ids':b_summary['expanded_evidence_ids'],
                 'selected_evidence_ids':[c['chunk_id'] for c in selected],
                 'delivered_evidence_ids':[c['chunk_id'] for c in selected],
                 'excluded_evidence_ids':decision['excluded_evidence_ids'], 'decisions':decision['decisions'],
                 'selection_rule_version':decision['selection_rule_version'],'scope_status':decision['scope_status'],
                 'budget_rule_version':'NOT_RUN_D_DEFERRED','source_mapping_status':b_summary['source_mapping_status'],
                 'delivery_status':'PREPARED_NOT_SENT','context_char_count':sum(len(c['content']) for c in selected),
                 'context_bytes':sum(len(c['content'].encode()) for c in selected),
                 'context_joined_char_count':len('\n\n'.join(c['content'] for c in selected)),
                 'provider_message_char_count':sum(len(m['content']) for m in messages),
                 'provider_message_bytes':sum(len(m['content'].encode()) for m in messages),
                 'context_token_count':None,'complete_request_token_count':None,'token_count_status':'UNKNOWN',
                 'gold_unit_granularity':q['unit_granularity'],'gold_units_required':len(q['mandatory_units']),
                 'gold_units_present':int(bool(covered)),'context_evidence_recall':1.0 if covered else 0.0,
                 'initial_candidate_preservation':seeds_kept,'target_source_entire_text_preserved':all_source_preserved,
                 'observed_structure_features_required':len(structural),'observed_structure_features_preserved':len(retained_structures),
                 'complete_human_structure_gold_status':'UNKNOWN','unknown_members':len(unknown),'unknown_members_preserved':len(unknown.intersection(delivered)),
                 'first_loss_stage':None,'gold_lost_by_selection':False,'gold_lost_by_budget':'NOT_APPLICABLE',
                 'answer_correctness':'NOT_RUN','generation_status':'NOT_RUN','external_model_calls':0,'trace_id':None,
                 'local_context_build_ms':round((time.perf_counter()-started)*1000,3)}
            rows.append(row);arms[arm]=row;details.append({'qid':q['qid'],'arm':arm,'run_id':row['run_id'],
              'provider_messages':messages,'citations':selected,'structures':structural,'selection':decision})
        assert sha(canon(expanded))==expanded_hash
        paired.append({'qid':q['qid'],'gold_paired':'B_AND_C_PRESERVED','structure_paired':'ALL_OBSERVED_TARGET_STRUCTURES_PRESERVED',
                       'members_B':len(arms['B']['selected_evidence_ids']),'members_C':len(arms['C']['selected_evidence_ids']),
                       'chars_reduced':arms['B']['context_char_count']-arms['C']['context_char_count'],
                       'bytes_reduced':arms['B']['context_bytes']-arms['C']['context_bytes']})
    write(out/'details.json',details);write(out/'results.json',rows)
    with (stage1/'bcd-results.jsonl').open('x',encoding='utf8') as f:
        for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    write(out/'summary.json',{'status':'PASS','case_count':6,'rows':12,'pair_results':paired,
          'D':'BLOCKED_TOKENIZER_DEFERRED_BY_OWNER','external_model_calls':0,'generation_observations':0,
          'input_snapshot_matches':True,'B_equals_E20_actual_messages':True,'selection_rule_version':RULE_VERSION,
          'stage1_ledger_hash_unchanged':True,'complete_necessary_structure_gold_status':'UNKNOWN',
          'rule_module_sha256':sha((ROOT/'scripts/e21_context_selection.py').read_bytes()),
          'runner_sha256':sha(Path(__file__).read_bytes())})
    print(json.dumps(load(out/'summary.json'),ensure_ascii=False))
if __name__=='__main__':main()
