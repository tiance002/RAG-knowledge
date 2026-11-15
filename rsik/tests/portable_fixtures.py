"""Portable synthetic fixtures. Nothing here is a real model/container receipt."""
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from rsik.foundation import canonical, digest, check_split, load
from rsik.holdout import replay_artifacts
from rsik.experiment import executor
from test_foundation_and_provenance import builder


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def build_split(path, count=20, singleton=False):
    items, sources = [], {}
    for index in range(count):
        # Deliberately invented content, source identities and questions.
        text = f'SYNTHETIC source {index}: required value {index}. ' + ('x' * 850 if not singleton or index else '')
        data = text.encode(); sha = digest(data)
        quote = f'required value {index}.'; start = text.index(quote)
        item = {'content': {'qid': f'SYNTHETIC-{index}', 'question': f'SYNTHETIC question {index}',
            'evidence': [{'quote': quote, 'original_location': {'char_start_0based': start, 'char_end_exclusive_0based': start + len(quote)}}]},
            'core_sha256': digest(f'SYNTHETIC-{index}'.encode()), 'content_review': {'decision': 'PASS', 'reviewer': 'SYNTHETIC_NOT_REAL_REVIEW'}}
        source = {'sha256': sha, 'file_id': f'synthetic-file-{index}', 'kb_id': 'synthetic-kb',
            'name': f'synthetic-{index}.md', 'source_path': 'SYNTHETIC_NOT_A_REAL_SOURCE'}
        items.append((item, source)); sources[sha] = data
    builder.build_split(items, sources, path, 'SYNTHETIC')
    write(path / 'freeze.json', check_split(path, expected_count=count))
    return path


def stress_split(source, output, *, query=False, direction='NEXT_ELSE_PREVIOUS', count=20):
    from scripts.build_rsi_neighbor_stress import neighbor_seed
    # These are tiny generated sources, not corpus/index copies.
    output.mkdir(parents=True)
    for path in source.rglob('*'):
        if path.is_file():
            dest=output/path.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(path.read_bytes())
    cases=load(output/'cases.json'); proof=load(output/'provenance.restricted.json'); manifest=load(output/'manifest.json')
    origins={}
    for index,item in enumerate(cases):
        case=item['case']; p=proof[item['qid']]
        if query:
            p['query_stress_original_seed_ids']=list(case['seed_ids'])
            case['question']=f'SYNTHETIC alternate question {index}'
            p['selector_input_sha256']=digest(canonical({k:v for k,v in case.items() if k!='question'}))
            if index<17:
                origins[item['qid']]={'core_sha256':p['origin_core_sha256'],'content':{'question':case['question'],
                    'evidence':[{'quote': f['text']} for f in p['facts']]}}
        else:
            p['stress_original_seed_ids']=list(case['seed_ids'])
            case['seed_ids']=neighbor_seed(case,case['seed_ids'],direction,True)
            case['expected_seed_count']=len(case['seed_ids'])
    manifest['cases']['sha256']=digest(canonical(cases))
    manifest['v22_scope'].update(mode='SOURCE_QUERY_STRESS' if query else 'SOURCE_LAYOUT_STRESS',
        seed_shift_direction=direction,single_chunk_policy='KEEP_ORIGINAL')
    if query:
        write(output/'qa_origins.restricted.json',origins)
        manifest['v22_scope']['qa_origins_sha256']=digest((output/'qa_origins.restricted.json').read_bytes())
    for name,value in [('cases.json',cases),('provenance.restricted.json',proof),('manifest.json',manifest)]:write(output/name,value)
    write(output/'freeze.json',check_split(output,expected_count=count))
    return output


class SyntheticCapsule:
    """Deterministic capture double; does not execute a worker or backend."""
    from rsik.execution_evidence import ExecutionEvidence
    evidence_policy=ExecutionEvidence.synthetic_control()
    business_root=Path('SYNTHETIC_NO_BACKEND')
    deadline=None
    def child(self, stage, paths, cwd, **kwargs):
        snapshot=load(paths[0])
        if stage=='prepare':
            files={f['id']:f for f in snapshot['files']}
            members=[{**c,'kb_id':files[c['file_id']]['kb_id']} for c in snapshot['chunks']]
            result={**snapshot,'status':'OK','expanded':[{'context_group':{'source_members':members}}]}
        else:
            members=snapshot['expanded'][0]['context_group']['source_members']
            context='\n'.join(m['content'] for m in members); proof=[];offset=0
            for member in members:
                end=offset+len(member['content'])
                proof.append({'id':member['id'],'context_start':offset,'context_end':end,'prompt_start':offset,'prompt_end':end})
                offset=end+1
            result={'status':'OK','members':members,'context':context,'messages':[{'content':context}],
                'request_calls':1,'proof':proof,'upstream_sha256':executor.fingerprint(snapshot)}
        write(paths[-1],result)
        return result


@pytest.fixture
def synthetic_split(tmp_path):
    return build_split(tmp_path/'fixture')


@pytest.fixture
def synthetic_replay(tmp_path, synthetic_split):
    worker=(ROOT/'scripts/rsi_delivery_worker.py').read_bytes()
    artifacts={a:{'candidate_id':digest(('SYNTHETIC-'+a).encode()),'worker':worker,'config':canonical({'selection':'all'})} for a in ('A','B')}
    run=tmp_path/'run'
    decision=replay_artifacts(SyntheticCapsule(),synthetic_split,artifacts,run,deadline=10**12,
        schema='rsi-safety-engineering/v1',scope='SYNTHETIC_CAPTURE_DOUBLE_NOT_REAL_EXECUTION')
    return {'run':run,'fixture':synthetic_split,'artifacts':artifacts,'decision':decision}
