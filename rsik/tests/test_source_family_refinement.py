"""Invented canonical records validate versioned source metadata engineering."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from scripts.review_rsi_source_families import build,validate_refinement
from rsik.events import canonical,digest
from rsik.exposure import ExposureLedger


def review(subject):
    return {'status':'AI_REVIEWED','reviewer':'SYNTHETIC_ONLY','reviewed_at':'2026-11-16T00:00:00Z','subject_sha256':digest(subject)}


def setup(tmp_path):
    title='CONTROL title';text='CONTROL original evidence.';canonical_text=(title+'\n\n'+text).encode()
    raw=b'# '+canonical_text;raw_hash=hashlib.sha256(raw).hexdigest()
    raw_path=tmp_path/'raw.md';raw_path.write_bytes(raw)
    old=ExposureLedger(tmp_path/'old.sqlite')
    for sid,h in (('raw-old',raw_hash),('remain-unknown','f'*64)):
        s={'source_id':sid,'document_id':sid,'version':'old-v1','content_sha256':h,
           'source_family_id':'UNRESOLVED','origin':'SYNTHETIC','relation':'UNKNOWN','related_source_ids':[],
           'exposure_status':'EXPOSURE_UNKNOWN'}
        old.register(s,review=review(s))
    old.expose(['raw-old'],partition='DEV',experiment_id='old-exp',actor='old-actor',evidence_ref='old-ref',occurred_at='2026-11-01T00:00:00Z',event_key='old')
    doc={'_id':'42#1','title':title,'text':text};raw_record=canonical(doc);record_hash=hashlib.sha256(raw_record).hexdigest();h=hashlib.sha256(canonical_text).hexdigest()
    corpus_hash='e'*64;revisions={'MIRACL corpus':'a'*40}
    manifest={'dataset':'miracl-zh','dataset_version':'CONTROL-pinned-v1','source_revisions':revisions,'files':[{'path':'corpus.jsonl','sha256':corpus_hash}]}
    mp=tmp_path/'manifest.json';mp.write_bytes(canonical(manifest));mh=hashlib.sha256(mp.read_bytes()).hexdigest()
    version_ref={'dataset_version':manifest['dataset_version'],'source_revisions':revisions,'corpus_sha256':corpus_hash,'manifest_sha256':mh}
    pid='upstream:miracl-zh:doc:42#1';family='miracl-zh:article-component:42'
    parent={'source_id':pid,'document_id':'canonical-42#1','version':'sha256:'+digest(version_ref),'content_sha256':h,
            'source_family_id':family,'origin':'SYNTHETIC_CANONICAL','relation':'ORIGINAL','related_source_ids':[],
            'exposure_status':'KNOWN_HISTORY'}
    proof={'local_source_path':str(raw_path),'local_source_sha256':raw_hash,'canonical_parent_id':pid,
           'dataset':'miracl-zh','doc_id':'42#1',**version_ref,'version_ref':version_ref,
           'corpus_path':'SYNTHETIC_READ_ONLY_CORPUS_CAPTURE',
           'manifest_path':str(mp),'manifest_corpus_sha256':corpus_hash,'local_canonical_sha256':h,
           'corpus_canonical_sha256':h,'corpus_record_sha256':record_hash,'corpus_line':1,
           'canonicalization':'markdown-h1-title-text/v1'}
    join={'dataset':'miracl-zh',**version_ref,'corpus_path':proof['corpus_path'],'manifest_corpus_sha256':corpus_hash,
          'exact_content_matches':[{'source_sha256':raw_hash,'doc_id':'42#1','local_canonical_sha256':h,
              'corpus_canonical_sha256':h,'corpus_record_sha256':record_hash,'corpus_line':1,'match':'CANONICAL_EXACT'}]}
    jp=tmp_path/'scan.json';jp.write_bytes(canonical([join]));proof['corpus_join_evidence']={'path':str(jp),'sha256':hashlib.sha256(jp.read_bytes()).hexdigest()}
    proposal={'schema':'rsi-source-family-refinement/v1','history_completeness':'INCOMPLETE_UNKNOWN',
              'parents':[{'source':parent,'review':review(parent)}],
              'mappings':{raw_hash:{'source_family_id':family,'relation':'DERIVED_FROM','related_source_ids':[pid],
                                     'exposure_status':'KNOWN_HISTORY','review_status':'AI_REVIEWED'}},
              'proofs':{raw_hash:proof},'canonical_records':{pid:{'raw_record_json':raw_record.decode()}}}
    proposal['review']=review(proposal)
    return old,proposal,raw_hash


def test_versioned_refinement_preserves_every_old_event_and_unknown(tmp_path):
    old,p,h=setup(tmp_path);original=old.log.path.read_bytes();events=old.log.read()
    pp=tmp_path/'proposal.json';pp.write_bytes(canonical(p));out=tmp_path/'new'
    receipt=build(old.log.path,pp,out)
    assert old.log.path.read_bytes()==original and old.log.read()==events
    assert receipt['history_completeness']=='INCOMPLETE_UNKNOWN' and not receipt['freshness_inferred']
    current=ExposureLedger(out/'exposure-events.sqlite');sources=current.sources()
    assert sources['remain-unknown']==old.sources()['remain-unknown']
    assert sources['raw-old']['content_sha256']==h and sources['raw-old']['relation']=='DERIVED_FROM'
    assert sources['raw-old']['exposure_status']=='KNOWN_HISTORY'
    preserved=[e['payload']['original_event'] for e in current.log.read() if e['event_type']=='HISTORICAL_EVENT_PRESERVED']
    assert preserved==events
    projected=[e for e in current.log.read() if e['event_type']=='SOURCE_EXPOSED']
    assert len(projected)==1 and projected[0]['payload']['experiment_id']=='old-exp'
    assert not current.final_eligibility(['raw-old'],experiment_id='future')['eligible']
    with pytest.raises(ValueError,match='FRESH_SOURCE_LEDGER_DIRECTORY_REQUIRED'):build(old.log.path,pp,out)


@pytest.mark.parametrize('bad,reason',[
    ('missing_proof','CORPUS_PROOF_REQUIRED'),('manifest','PINNED_CORPUS_MANIFEST_MISMATCH'),
    ('crossfamily','CROSS_FAMILY_PARENT'),('record','CANONICAL_RECORD_HASH_CHANGED'),
    ('version','CANONICAL_VERSION_BINDING_MISMATCH'),('review','REVIEW_BINDING_REQUIRED'),
    ('completeness','EXPOSURE_COMPLETENESS_MUST_REMAIN_UNKNOWN'),
    ('collision','CANONICAL_PARENT_COLLISION'),
])
def test_refinement_rejects_incomplete_or_conflicting_proofs(tmp_path,bad,reason):
    old,p,h=setup(tmp_path)
    if bad=='missing_proof':p['proofs']={}
    elif bad=='manifest':p['proofs'][h]['manifest_corpus_sha256']='0'*64
    elif bad=='crossfamily':p['mappings'][h]['source_family_id']='other'
    elif bad=='record':next(iter(p['canonical_records'].values()))['raw_record_json']='{}'
    elif bad=='version':p['proofs'][h]['version_ref']={}
    elif bad=='review':p['review']['subject_sha256']='wrong'
    elif bad=='collision':p['parents'].append(deepcopy(p['parents'][0]))
    else:p['history_completeness']='COMPLETE'
    if bad!='review':p['review']=review({k:v for k,v in p.items() if k!='review'})
    with pytest.raises(ValueError,match=reason):validate_refinement(p,old.sources(),old.log.read())
