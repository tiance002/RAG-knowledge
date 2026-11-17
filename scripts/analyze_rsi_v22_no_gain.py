"""Structural no-gain analysis using archived delivery inputs, never Gold/holdout."""
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rsik.foundation import verify_boundary


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def analyze(run):
    verify_boundary()
    source=(ROOT/'scripts/rsi_delivery_worker.py').read_text(encoding='utf-8')
    helper=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='duplicate_key')
    env={};exec(compile(ast.Module(body=[helper],type_ignores=[]),'<protected-pure-helper>','exec'),env)
    stats=Counter();extensions=Counter();fieldsets=Counter();lengths={'seed':[],'nonseed':[]}
    pairs=0
    for directory in sorted(Path(run).glob('case-*')):
        snapshot=load(directory/'snapshot.json');capture=load(directory/'A/capture.json')
        chunks={c['id']:c for c in snapshot['chunks']};files={f['id']:f for f in snapshot['files']}
        seeds=set(snapshot['seed_ids']);keys=[]
        for member in capture['members']:
            kind='seed' if member['id'] in seeds else 'nonseed'
            key=env['duplicate_key'](member,chunks[member['id']],files[member['file_id']]);keys.append(key)
            stats['members']+=1;stats[kind]+=1;stats['duplicate_key_eligible']+=key is not None
            lengths[kind].append(len(member['content']))
            extensions[Path(files[member['file_id']]['name']).suffix.lower()]+=1
            fieldsets[tuple(sorted(member))]+=1
        pairs+=sum(keys[i] is not None and keys[i]==keys[j] for i in range(len(keys)) for j in range(i))
        stats['questions']+=1
    return {'counts':dict(stats),'eligible_duplicate_pairs':pairs,'file_extension_member_counts':dict(extensions),
        'length_summary':{kind:{'min':min(values),'max':max(values),'sum':sum(values),'values':sorted(values)}
                          for kind,values in lengths.items()},
        'member_field_sets':[{'fields':list(fields),'members':count} for fields,count in fieldsets.items()],
        'used_evaluator_labels':False,'used_holdout':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True,type=Path);parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();result=analyze(args.run)
    with args.out.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2)
    print(json.dumps(result))
