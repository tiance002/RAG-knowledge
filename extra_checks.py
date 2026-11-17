"""Separate engineering fixtures; does not replace the inherited manifest/rubric."""
import copy, json, pathlib, sys, uuid, hashlib
ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
import rsi_candidates as c
import run_rsi_offline as r
from repair_round import save

def main():
    base=ROOT/'var/repair-round'
    bundle=base/'bundle'
    manifest=c.read_json(bundle/'manifest.json')
    workers=c.verify_arm_sources(manifest,bundle)
    original=c.read_json(bundle/'cases.json')[0]['case']
    different=copy.deepcopy(original)
    header, row=copy.deepcopy(original['chunks'][:2])
    second=copy.deepcopy(header); second.update(id='another-header',original_index=1)
    second['source_locator']['bbox']=[20,90,300,110]
    row['original_index']=2
    different['chunks']=[header,second,row]
    different['question']='Public synthetic location preservation check.'
    missing=copy.deepcopy(original); missing['files']=[]
    cases=[{'qid':'different-location','case':different},{'qid':'missing-owner','case':missing}]
    expected={'different-location':{'status':'PAIRED','facts':c.read_json(bundle/'evaluation.json')['engineering-selection']['facts'],'members':{'A':['row'],'B':['header','another-header','row']}},'missing-owner':{'status':'BLOCKED','missing_file_id':'f1'}}
    out=base/(sys.argv[1] if len(sys.argv)>1 else 'independent-engineering'); out.mkdir(exist_ok=False)
    run_id=uuid.uuid4().hex
    save(out/'fixture.json',{'scope':'Independent engineering checks; inherited original fixture unchanged','run_id':run_id,'parent':manifest['r1']['parent'],'child':manifest['r1']['child'],'cases':cases,'evaluation':expected,'budget':{'external_calls':0,'attempts_per_arm':1}})
    rows=[]
    for i,item in enumerate(cases,1):
        directory=out/f'case-{i:03}';directory.mkdir()
        case=r.checked_case(item['case']);save(directory/'input.json',case)
        snapshot=r.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory)
        row={'qid':item['qid'],'arms':{},'status':snapshot['status']}
        if snapshot['status']=='BLOCKED':
            row['reason']=snapshot['reason']; row['expected_block']=expected[item['qid']]['missing_file_id'] in snapshot['reason']
            assert row['expected_block']
            assert not (directory/'A').exists() and not (directory/'B').exists()
        else:
            snapshot=r.execution_view(snapshot);save(directory/'arm-input.json',snapshot)
            for arm in ('A','B'):
                ad=directory/arm;ad.mkdir()
                capture=r.child('deliver',[directory/'arm-input.json',bundle/(arm+'.json'),ad/'capture.json'],ad,worker=workers[arm],helper_dir=ROOT/'scripts')
                assert capture['status']=='OK'
                assert capture['upstream_sha256']==r.fingerprint(snapshot)
                score=r.assess(case,capture,expected[item['qid']],arm)
                row['arms'][arm]=score
                # Parent fact loss is preserved in scores; assert only independently frozen sequences.
                assert score['member_ids']==expected[item['qid']]['members'][arm]
                if arm=='B': assert not score['faults']
            row['status']='PAIRED'
        save(directory/'result.json',row);rows.append(row)
    save(out/'results.json',{'run_id':run_id,'rows':rows,'total':2,'paired':1,'expected_blocked':1,'actual_external_model_calls':0})
    restored=out/'restored-bundle'
    identity=c.read_json(base/'identity.json')
    c.materialize(base/'registry',identity['parent'],identity['child'],restored,bundle/'cases.json',bundle/'evaluation.json')
    assert {p.name:p.read_bytes() for p in restored.iterdir()}=={p.name:p.read_bytes() for p in bundle.iterdir()}
    save(base/'restore-check.json',{'status':'PASS','files':len(list(bundle.iterdir())),'rerun_model_calls':0})
    print(json.dumps({'run_id':run_id,'paired':1,'blocked':1,'restore':'PASS'}))

if __name__=='__main__':main()
