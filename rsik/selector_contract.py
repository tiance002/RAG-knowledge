"""Public code-interface metadata; no dataset identities, source text or Gold."""
import ast
from collections import Counter
from pathlib import Path
import json

ROOT=Path(__file__).resolve().parents[1]
INTENTS={'STRUCTURAL_POLICY_SEARCH','SEED_ONLY_ABLATION','SHORT_NONSEED_BUDGET','BALANCED_NEIGHBOR_BUDGET','SUBSTANTIAL_NONSEED_BUDGET','THREE_QUARTER_NONSEED_BUDGET'}
INTENTS.update({'NINE_TENTHS_NONSEED_BUDGET','FULL_SEED_NONSEED_BUDGET','PRECEDING_NEIGHBOR_BUDGET'})
INTENTS.update({'QUERY_OVERLAP_ABLATION','QUERY_OVERLAP_CONSERVATIVE','QUERY_OVERLAP_ZERO_MATCH_FALLBACK'})
INTENTS.update({'QUERY_SEED_MATCH_FALLBACK','QUERY_TRIGRAM_ZERO_MATCH_FALLBACK','QUERY_NONSEED_MATCH_FALLBACK','QUERY_BIGRAM_RATIO'})


def interface_contract():
    source=(ROOT/'scripts/rsi_delivery_worker.py').read_text(encoding='utf-8')
    helper=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='duplicate_key')
    return {'schema_version':'rsi-selector-interface/v1',
        'members':'ordered list of verified source-member dictionaries; return a list of original integer indices',
        'member_fields':['id','file_id','kb_id','content','original_index','parent_id','relation','heading_text'],
        'chunks':'dictionary keyed by member id; values contain content, file_id, original_index and source_locator',
        'files':'dictionary keyed by file id; values contain id, kb_id and name',
        'seed_ids':'list of independent seed member ids; all seeds must remain',
        'selection':'actual frozen parent configuration, all or exact_duplicate; cannot change config in this seam',
        'allowed_arithmetic':['+','-'],
        'arithmetic_example':'double a length with len(content) + len(content); 2 * len(content) is FORBIDDEN',
        'forbidden_syntax':['multiplication','division','floor division','lambda','keyword call arguments','nested function definitions'],
        'empty_input':'Must return [] for no members/seeds. Guard max() using max(seed_lengths or [0]); never call max([]).',
        'local_mutation':'Only scalar rebinding and append to a fresh private list. All attribute/subscript stores are forbidden, including writes into a newly-created dict. Use a global longest-seed reference, not a mutable per-file map.',
        'duplicate_key_source':ast.get_source_segment(source,helper),
        'learning_scope':'training exploration, not proof of population significance or answer quality'}


def structural_counts(run, arm):
    """Aggregate only actual delivery structure, never evaluation predicates."""
    counts=Counter({'pdf_members':0,'other_members':0,'duplicate_key_eligible_members':0,
        'exact_content_duplicate_pairs':0,'seed_content_chars':0,'nonseed_content_chars':0,
        'nonseed_members_le100':0,'nonseed_members_le200':0,'nonseed_members_gt200':0})
    # Frozen helper is trusted and pure. Do not import the worker module, which
    # installs process-global transport patches and backend imports.
    source=(ROOT/'scripts/rsi_delivery_worker.py').read_text(encoding='utf-8')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='duplicate_key')
    env={};exec(compile(ast.Module(body=[node],type_ignores=[]),'<frozen-pure-helper>','exec'),env)
    for directory in sorted(Path(run).glob('case-*')):
        snapshot=json.loads((directory/'snapshot.json').read_text(encoding='utf-8-sig'))
        capture=json.loads((directory/arm/'capture.json').read_text(encoding='utf-8-sig'))
        chunks={c['id']:c for c in snapshot['chunks']};files={f['id']:f for f in snapshot['files']}
        seeds=set(snapshot['seed_ids']);members=capture['members']
        for member in members:
            file=files[member['file_id']];length=len(member['content'])
            counts['pdf_members' if file['name'].lower().endswith('.pdf') else 'other_members']+=1
            counts['duplicate_key_eligible_members']+=env['duplicate_key'](member,chunks[member['id']],file) is not None
            counts['seed_content_chars' if member['id'] in seeds else 'nonseed_content_chars']+=length
            if member['id'] not in seeds:
                counts['nonseed_members_le100']+=length<=100
                counts['nonseed_members_le200']+=length<=200
                counts['nonseed_members_gt200']+=length>200
        counts['exact_content_duplicate_pairs']+=sum(
            members[i]['file_id']==members[j]['file_id'] and members[i]['kb_id']==members[j]['kb_id']
            and members[i]['content']==members[j]['content']
            for i in range(len(members)) for j in range(i))
    return dict(counts)
