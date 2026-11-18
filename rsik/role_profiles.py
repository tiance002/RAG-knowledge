"""Explicit role strategy versions, separate from the fixed three test slots."""
from .state import digest
LEGACY = 'legacy/v1'
DEPENDENCY = 'query-k3-dependency/v1'
M1_SPECIALIZED_V1 = 'M1_SPECIALIZED_V1'
M1_INDEPENDENT_GENERALIST_V1 = 'M1_INDEPENDENT_GENERALIST_V1'
M1_PROFILES = (M1_SPECIALIZED_V1, M1_INDEPENDENT_GENERALIST_V1)
PROFILES = {
 M1_SPECIALIZED_V1: {'P1_DEDUP':'P1_DEDUP','P2_BUDGET':'P2_BUDGET','P3_ORDER':'P3_DEPENDENCY'},
 M1_INDEPENDENT_GENERALIST_V1: {'P1_DEDUP':'P1_GENERALIST','P2_BUDGET':'P2_GENERALIST','P3_ORDER':'P3_GENERALIST'},
 LEGACY: {'P1_DEDUP':'P1_DEDUP','P2_BUDGET':'P2_BUDGET','P3_ORDER':'P3_ORDER'},
 DEPENDENCY: {'P1_DEDUP':'P1_DEDUP','P2_BUDGET':'P2_BUDGET','P3_ORDER':'P3_DEPENDENCY'},
}
DEPENDENCY_TASK = ('Cross-member dependencies and conservative fallbacks only. Independently propose a falsifiable selection policy preserving adjacent evidence, relation fragments, source/condition/unit ownership and all seeds. Investigate uncertainty under alternative queries and shifted seeds without seeing those private queries or answers. Preserve original relative order. Do not assume lexical absence proves irrelevance. Do not change the evaluator or surrounding framework. Explain a concrete falsifier and abstain if the seam cannot express the experiment.')
def roles_for(profile):
 if profile not in PROFILES: raise ValueError('UNKNOWN_ROLE_PROFILE')
 return dict(PROFILES[profile])
def profile_manifest(profile):
 if profile in M1_PROFILES:
  return {'version':profile,'statistical_slots':roles_for(profile),'strategy_sha256':digest({'version':profile,'mapping':roles_for(profile),'policy':M1_SAFETY_POLICY,'tasks':m1_tasks(profile)})}
 return {'version':profile,'statistical_slots':roles_for(profile),'strategy_sha256':digest({'version':profile,'mapping':roles_for(profile),'dependency_task':DEPENDENCY_TASK if profile==DEPENDENCY else None})}
def preserves_selection_order(profile):
 return profile == LEGACY


# Constraints only: no reference algorithm, selected formula, thresholds or examples.
M1_SAFETY_POLICY = """You are one isolated role in a deterministic Context Selection experiment.
Use only the supplied public context, aliases and exact evidence strings. Do not infer hidden
answers, invent source facts or claim experiments ran. Confidence is metadata only.
Return the strict JSON envelope. Proposals are falsifiable hypotheses; only frozen deterministic
replay can establish facts, correctness or gain. ABSTAIN if no admissible hypothesis exists.
Only the select_member_indices function body may change. Keep its supplied signature exactly.
No imports, globals, argument mutation, indirect calls, tools, data access or evaluator edits.
No new attributes except get/append. Allowed calls: require, duplicate_key, any, all, range,
len, enumerate, list, tuple, set, sorted, min, max, sum, abs. Append only to a fresh private list.
Binary arithmetic permits only + and -. Never assign through a subscript or attribute.
Handle empty members and empty seed lists without raising. Preserve every seed and original
relative membership order. For the six-argument seam, query is read-only and not supplied in
proposal context; preserve all members when its length is less than two characters.
Do not hardcode dataset identities. Do not weaken source, fact, budget, signature, whitelist,
replay or statistical gates. Report uncertainty and a concrete falsifier. Keep descriptions concise.
"""
M1_GENERALIST_TASK = ('Independently design a falsifiable context-selection policy within the supplied seam. '
 'Choose your own hypothesis from the actual parent, public diagnosis and permitted aggregate history. '
 'No dimension or algorithm is assigned. Preserve the common constraints; explain risk and falsification.')
M1_SHARED_TASKS = {
 'diagnose': 'Identify supported observations and a falsifiable selection-policy hypothesis. Separate code defects, no gain, fact regression, infrastructure failure, statistical insufficiency and evaluation coverage. Record missing evidence and uncertainty; do not prescribe a candidate algorithm. ABSTAIN when unsupported.',
 'curator': 'Read-only curation from traceable completed candidate experiments only. Never infer selection actions from ABSTAIN counts or missing evidence, assign helpful/harmful status, or claim causal algorithmic gains. Follow the supplied schema and exact compatibility/evidence references. ABSTAIN when unsupported.',
 'meta': 'Read-only audit of what supplied aggregate evidence supports. Mark missing distribution, currency and false-pass evidence UNKNOWN. Do not inspect hidden answers, modify fixtures or change deterministic decisions.',
}
M1_SPECIALIZED_TASKS = {
 'P1_DEDUP': 'Independently investigate deduplication policies only. Preserve independent evidence and seed identity. Choose and falsify your own hypothesis; duplicate appearance is not proof of redundant necessary facts.',
 'P2_BUDGET': 'Independently investigate budget allocation policies only. Choose your own falsifiable hypothesis from the supplied context. Nonseed overhead is not proven waste; preserve necessary evidence and explain potential fact loss.',
 'P3_DEPENDENCY': 'Independently investigate cross-member dependencies and conservative fallback policies only. Preserve evidence relations, source/condition/unit ownership, seeds and original order. Lexical absence does not prove irrelevance. Choose your own hypothesis and concrete falsifier.',
}

def m1_tasks(profile):
 if profile not in M1_PROFILES: raise ValueError('M1_ROLE_PROFILE_REQUIRED')
 proposals = M1_SPECIALIZED_TASKS if profile == M1_SPECIALIZED_V1 else {r:M1_GENERALIST_TASK for r in roles_for(profile).values()}
 return {**M1_SHARED_TASKS, **proposals}
