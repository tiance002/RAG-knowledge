"""Versioned metric definitions. Missing evidence is never a measured zero."""

GROUP_DIMENSIONS = ('experiment_id', 'epoch', 'role_profile', 'role_profile_sha256', 'evidence_mode',
                    'compatibility_fingerprint', 'evaluator_version', 'profile')

_DEFINITIONS = {
 'A1': ('Agent Invocation Success Rate', 'successful contract-valid attempts', 'actual invocation attempts', 'ratio'),
 'A2': ('Agent Contract Valid Rate', 'contract-valid received responses', 'received responses', 'ratio'),
 'A3': ('Agent Abstention Rate', 'explicit abstentions', 'actual invocation attempts', 'ratio'),
 'A4': ('Agent Failure Rate', 'failed attempts excluding explicit abstention', 'actual invocation attempts', 'ratio'),
 'P1': ('Proposal Valid Rate', 'schema-valid proposal instances', 'proposal attempts', 'ratio'),
 'P2': ('Static Pass Rate', 'static-passing proposal instances', 'schema-valid proposal instances', 'ratio'),
 'P3': ('Proposal Diversity', 'distinct normalized AST strategies', 'static-passing proposal instances', 'ratio'),
 'P4': ('Duplicate Rate', 'static-passing instances minus distinct normalized AST strategies', 'static-passing proposal instances', 'ratio'),
 'P5': ('Replay Completion Rate', 'candidates with completed paired replay in complete fixed-three families', 'candidates entering replay in complete fixed-three families', 'ratio'),
 'S1': ('Training Adoption Rate', 'unique training-adopted candidates', 'fully adjudicated candidates in complete fixed-three families', 'ratio'),
 'S2': ('Holdout Verification Rate', 'unique independently verified candidates', 'unique candidates completing independent verification', 'ratio'),
 'S3': ('Parent Diversity', 'distinct explicitly selected parents', 'explicit parent selection events', 'ratio'),
 'S4': ('Branch Survival', 'branches eligible at predeclared window end', 'branches eligible at predeclared window start', 'ratio'),
 'S5': ('Search Depth', 'maximum validated lineage generation', 'not applicable', 'generation'),
 'S6': ('Search Concentration', 'sum of squared explicit parent selection shares', 'not applicable', 'HHI'),
 'M1': ('Memory Retrieval Count', 'explicit raw retrieval events', 'not applicable', 'count'),
 'M2': ('Memory Injection Rate', 'explicit injected experience uses', 'explicit retrieved experience uses', 'ratio'),
 'M3': ('Memory Reference Rate', 'distinct injected experiences referenced per round', 'distinct injected experiences per round', 'ratio'),
 'M4': ('Verified Association Rate', 'experience use episodes associated with independently verified candidates', 'experience use episodes with completed independent verification', 'ratio'),
 'M5': ('Memory Conflict Rate', 'conflicting co-retrieved pairs', 'explicit co-retrieved pairs', 'ratio'),
 'M6': ('Memory Staleness', 'observed entries mismatching explicit current fingerprint', 'entries with both entry and current fingerprints', 'ratio'),
 'C1': ('Total Model Cost', 'sum of actual provider invoiced nanodollars', 'not applicable', 'USD'),
 'C2': ('Cost per Valid Proposal', 'actual provider invoiced nanodollars', 'valid proposal instances', 'USD/proposal'),
 'C3': ('Cost per Completed Replay', 'actual provider invoiced nanodollars', 'unique completed candidate replays in complete families', 'USD/replay'),
 'C4': ('Cost per Verified Improvement', 'actual provider invoiced nanodollars', 'unique independently verified improvements', 'USD/improvement'),
 'R1': ('Stage Failure Rate', 'explicit failed stage attempts', 'explicit stage attempts', 'ratio'),
 'R2': ('Recovery Success Rate', 'successful explicit recovery attempts', 'explicit recovery attempts', 'ratio'),
 'R3': ('Duplicate Execution Rate', 'extra actual executions for one action identity', 'actual executions', 'ratio'),
 'R4': ('Infrastructure Failure Rate', 'attempts explicitly classified infrastructure failures', 'actual invocation attempts', 'ratio'),
 'R5': ('Rollback Rate', 'completed rollback operations', 'completed promotion operations', 'ratio'),
 'R6': ('Incomplete Evidence Rate', 'entities failing declared evidence completeness contract', 'entities assessed against declared evidence completeness contract', 'ratio'),
}

METRICS = {key: {
    'metric_id': key, 'description': name, 'numerator': numerator,
    'denominator': denominator, 'unit': unit, 'aggregation_level': 'compatibility partition',
    'evidence_source': 'validated immutable event or explicitly bound metadata projection',
    'eligibility_rule': 'explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit',
    'unknown_policy': 'UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros',
    'compatibility_scope': list(GROUP_DIMENSIONS),
} for key, (name, numerator, denominator, unit) in _DEFINITIONS.items()}


# Separate versioned observation protocol; M0 metric IDs/definitions stay frozen.
OBSERVATION_SCHEMA = 'rsi-observation/v1'
COMPLETENESS_SCHEMA = 'rsi-observation-completeness/v1'

METRIC_CONTRACT_VERSION = 'rsi-metrics/m1a1'
MEMORY_METRIC_DEFINITIONS = {
    'M1': {'numerator': 'unique explicit retrieval_id operations including empty results',
           'denominator': 'not applicable'},
    'M2': {'numerator': 'distinct explicit injected retrieval_id and entry_id pairs present in retrieval results',
           'denominator': 'distinct explicit retrieved retrieval_id and entry_id pairs'},
    'M3': {'numerator': 'distinct injected round_id and entry_id pairs explicitly referenced',
           'denominator': 'distinct explicit injected round_id and entry_id pairs'},
}

# v1 operation denominator semantics; frozen M0 definitions above remain intact.
SELECTION_METRIC_DEFINITIONS = {
    'S3': {'numerator': 'distinct actual parents from completed valid selection operations',
           'denominator': 'completed valid selection operations including empty results'},
    'S6': {'numerator': 'sum of squared actual parent counts',
           'denominator': 'square of completed valid selection operations including empty results'},
}
