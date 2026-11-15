"""Explicit role strategy versions, separate from the fixed three test slots."""
from .state import digest
LEGACY = 'legacy/v1'
DEPENDENCY = 'query-k3-dependency/v1'
PROFILES = {
 LEGACY: {'P1_DEDUP':'P1_DEDUP','P2_BUDGET':'P2_BUDGET','P3_ORDER':'P3_ORDER'},
 DEPENDENCY: {'P1_DEDUP':'P1_DEDUP','P2_BUDGET':'P2_BUDGET','P3_ORDER':'P3_DEPENDENCY'},
}
DEPENDENCY_TASK = ('Cross-member dependencies and conservative fallbacks only. Independently propose a falsifiable selection policy preserving adjacent evidence, relation fragments, source/condition/unit ownership and all seeds. Investigate uncertainty under alternative queries and shifted seeds without seeing those private queries or answers. Preserve original relative order. Do not assume lexical absence proves irrelevance. Do not change the evaluator or surrounding framework. Explain a concrete falsifier and abstain if the seam cannot express the experiment.')
def roles_for(profile):
 if profile not in PROFILES: raise ValueError('UNKNOWN_ROLE_PROFILE')
 return dict(PROFILES[profile])
def profile_manifest(profile):
 return {'version':profile,'statistical_slots':roles_for(profile),'strategy_sha256':digest({'version':profile,'mapping':roles_for(profile),'dependency_task':DEPENDENCY_TASK if profile==DEPENDENCY else None})}
def preserves_selection_order(profile):
 return profile == LEGACY
