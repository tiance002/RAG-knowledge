"""Freeze paired parse outputs with identical pre-existing VLM captions."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--parses',required=True)
    ap.add_argument('--indexed',required=True)
    ap.add_argument('--cases',required=True)
    ap.add_argument('--output',required=True)
    ap.add_argument('--reuse-before-input')
    args=ap.parse_args()
    output=Path(args.output)
    if output.exists():raise SystemExit('Do not overwrite frozen experiment input')
    files=json.loads(Path(args.indexed).read_text(encoding='utf-8'))['files']
    captions={}
    names={}
    for file in sorted(files,key=lambda f:(f['kb_name']!='prod-mmlongbench-doc', f['id'])):
        sha=file['source_sha256'];names[sha]=file['name']
        for chunk in file['chunks']:
            if chunk['content_type'].lower()=='image' and not chunk['content'].startswith('[图片:'):
                captions.setdefault((sha,chunk['page_number']),chunk['content'])
    arms={}
    for arm in ('before','after'):
        chunks=[]
        for path in sorted((Path(args.parses)/arm).glob('*.json')):
            data=json.loads(path.read_text(encoding='utf-8'))
            sha=data['source_sha256']
            for index,chunk in enumerate(data['chunks']):
                value=dict(chunk)
                if chunk['content_type']=='image' and (sha,chunk['page_number']) in captions:
                    value['content']=captions[(sha,chunk['page_number'])]
                value.update(source_sha256=sha, file_name=names[sha], original_index=index)
                chunks.append(value)
        arms[arm]=chunks
    if args.reuse_before_input:
        previous=json.loads(Path(args.reuse_before_input).read_text(encoding='utf-8'))['arms']['before']
        def stable(chunk):
            value={**chunk,'metadata':dict(chunk.get('metadata') or {})}
            value['metadata'].pop('timestamp',None)
            return value
        if [stable(c) for c in previous]!=[stable(c) for c in arms['before']]:
            raise ValueError('Baseline differs beyond non-inference image timestamp')
        # Preserve the exact old bytes, including original volatile metadata.
        arms['before']=previous
    cases=[json.loads(l) for l in Path(args.cases).read_text(encoding='utf-8').splitlines()]
    # The inference process has no answer keys. Relevance is scored only after retrieval.
    cases=[{k:c[k] for k in ('ordinal','qid','question','expected_source_sha256','expected_pages')} for c in cases]
    payload={'schema':'pdf-parser-paired/v1','arms':arms,'cases':cases,
        'fixed':{'top_k':50,'score_threshold':0.0,'search_mode':'hybrid','vector_weight':.7,
                 'bm25_weight':.3,'rerank_enabled':True,'rerank_top_k':20,'context_window':1,
                 'temperature':.7,'max_output_tokens':2000,
                 'image_policy':'identical_existing_caption_text_no_new_VLM_calls',
                 'sources':'same_three_ECS_source_hashes_both_arms'},
        'bounds':{'embedding_inputs':1024,'rerank_calls':64,'generation_calls':16},
        'baseline_note':'New paired baseline, not historical production score. Production PDF KB lacks one of these three sources.'}
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
    output.write_bytes(raw)
    print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'arm_chunks':{k:len(v) for k,v in arms.items()},
                      'unique_embedding_texts':len({c['content'] for cs in arms.values() for c in cs})+len(cases),
                      'cases':len(cases),'bounds':payload['bounds']}))


if __name__=='__main__':main()
