"""Pure, opt-in rule router. Defaults are uncalibrated, not quality guarantees.

No provider calls, answer validation, Gold labels or model identity inference live
here. Missing observations remain UNKNOWN and contribute an interval to the score.
"""
from dataclasses import dataclass, field
import math
import os
import re
import json
from typing import Mapping, Optional


FEATURE_NAMES = ('T', 'C', 'J', 'D', 'L', 'M')
MODES = ('OFF', 'DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY')
UNKNOWN_POLICIES = ('BASELINE', 'CHEAP', 'EXPENSIVE')
FEATURE_STATUSES = ('OBSERVED', 'ESTIMATED', 'UNKNOWN')
RULE_VERSION = 'rule-router-v1.2-reviewed-signals-uncalibrated'
AFFINITY_VERSION = 'rule-router-v1.3-task-interactions-experimental'
RISK_V4_VERSION = 'rule-router-v1.5-cheap-default-risk-v4-estimated'
RISK_V5_VERSION = 'rule-router-v1.6-cheap-default-risk-v5-d-observe-length'
RISK_V6_VERSION = 'rule-router-v1.7-cheap-default-risk-v6-c-semantic-refinement'
RISK_E_VERSION = 'rule-router-v9-e-three-slots'
RISK_VERSIONS = {'RISK_V4': RISK_V4_VERSION, 'RISK_V5': RISK_V5_VERSION,
                 'RISK_V6': RISK_V6_VERSION, 'RISK_E': RISK_E_VERSION}
QUALITY_FIRST_VERSION = 'rule-router-v1.4-quality-first-estimated-uncalibrated'
# Weak initial prior, not fitted/calibrated weights. Long or tabular context
# alone must not be treated as evidence of a difficult reasoning task.
DEFAULT_WEIGHTS = {'T': 0.25, 'C': 0.20, 'J': 0.25, 'D': 0.10, 'L': 0.10, 'M': 0.10}


def _unit(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(name + ' must be a finite number in [0, 1]')
    return float(value)


def _boolean(value, name):
    if str(value).lower() not in ('true', 'false'):
        raise ValueError(name + ' must be true or false')
    return str(value).lower() == 'true'


@dataclass(frozen=True)
class RouterConfig:
    mode: str = 'OFF'
    cheap_model: str = 'Qwen/Qwen3.5-4B'
    expensive_model: str = 'zai-org/GLM-5.3-Flash'
    threshold: float = 0.5
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    unknown_policy: str = 'BASELINE'
    escalate_on_quality_fail: bool = True
    escalate_on_truncated: bool = False
    escalate_on_provider_fallback: bool = False
    escalate_on_unsupported: bool = False
    selection_policy: str = 'WEIGHTED_V1'
    task_risk_floor: float = 0.55

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError('unknown router mode')
        if self.unknown_policy not in UNKNOWN_POLICIES:
            raise ValueError('unknown unknown-feature policy')
        _unit(self.threshold, 'threshold')
        _unit(self.task_risk_floor, 'task_risk_floor')
        if self.selection_policy not in ('WEIGHTED_V1', 'AFFINITY_V2', 'QUALITY_FIRST_V3', *RISK_VERSIONS):
            raise ValueError('unknown selection policy')
        if set(self.weights) != set(FEATURE_NAMES):
            raise ValueError('weights must specify T,C,J,D,L,M exactly')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in self.weights.values()) or not math.isfinite(sum(self.weights.values())) or sum(self.weights.values()) <= 0:
            raise ValueError('weights must be finite, nonnegative and have positive total')
        for name in ('escalate_on_quality_fail', 'escalate_on_truncated', 'escalate_on_provider_fallback', 'escalate_on_unsupported'):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(name + ' must be boolean')
        if not self.cheap_model.strip() or not self.expensive_model.strip():
            raise ValueError('model roles must have nonempty identifiers')
        if self.cheap_model == self.expensive_model:
            raise ValueError('cheap and expensive roles must be distinct')

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        prefix = 'RAG_MODEL_ROUTER_'
        return cls(
            mode=env.get(prefix + 'MODE', 'OFF').strip().upper(),
            cheap_model=env.get(prefix + 'CHEAP_MODEL', 'Qwen/Qwen3.5-4B').strip(),
            expensive_model=env.get(prefix + 'EXPENSIVE_MODEL', 'zai-org/GLM-5.3-Flash').strip(),
            threshold=float(env.get(prefix + 'THRESHOLD', '0.45' if env.get(prefix + 'SELECTION_POLICY', '').strip().upper() in ('RISK_V5', 'RISK_V6', 'RISK_E') else '0.5')),
            selection_policy=env.get(prefix + 'SELECTION_POLICY', 'WEIGHTED_V1').strip().upper(),
            task_risk_floor=float(env.get(prefix + 'TASK_RISK_FLOOR', '0.55')),
            weights={k: float(env.get(prefix + 'WEIGHT_' + k, str(DEFAULT_WEIGHTS[k]))) for k in FEATURE_NAMES},
            unknown_policy=env.get(prefix + 'UNKNOWN_POLICY', 'BASELINE').strip().upper(),
            **{name: _boolean(env.get(prefix + key, default), prefix + key) for name, key, default in (
                ('escalate_on_quality_fail', 'ESCALATE_ON_QUALITY_FAIL', 'true'),
                ('escalate_on_truncated', 'ESCALATE_ON_TRUNCATED', 'false'),
                ('escalate_on_provider_fallback', 'ESCALATE_ON_PROVIDER_FALLBACK', 'false'),
                ('escalate_on_unsupported', 'ESCALATE_ON_UNSUPPORTED', 'false'))})


@dataclass(frozen=True)
class FeatureValue:
    value: Optional[float] = None
    status: str = 'UNKNOWN'
    provenance: str = 'NOT_CAPTURED'

    def __post_init__(self):
        if self.status not in FEATURE_STATUSES:
            raise ValueError('unknown feature status')
        if self.status == 'UNKNOWN':
            if self.value is not None:
                raise ValueError('UNKNOWN features must have value None')
        else:
            _unit(self.value, 'feature')
            if not self.provenance or self.provenance == 'NOT_CAPTURED':
                raise ValueError('known features require provenance')

    def to_payload(self):
        return {'value': self.value, 'status': self.status, 'method': self.provenance}


@dataclass(frozen=True)
class RouteDecision:
    enabled: bool
    mode: str
    selected_role: str
    selected_model: Optional[str]
    reason: str
    threshold: float
    score_lower: Optional[float] = None
    score_upper: Optional[float] = None
    features: Mapping[str, FeatureValue] = field(default_factory=dict)
    metadata: Mapping = field(default_factory=dict)

    def to_payload(self):
        # Exact enum keys are retained by the existing observation sanitizer.
        return {'enabled': self.enabled, 'mode': self.mode, 'role': self.selected_role,
                'model': self.selected_model, 'reason': self.reason, 'threshold': self.threshold,
                'score_lower': self.score_lower, 'score_upper': self.score_upper,
                'features': {k: v.to_payload() for k, v in self.features.items()},
                'measurements': dict(self.metadata), 'rule_version': self.metadata.get('rule_version', RULE_VERSION),
                'calibration': {'status': 'UNCALIBRATED'}}


def _text(message):
    value = message.get('content') if isinstance(message, dict) else getattr(message, 'content', None)
    return value if isinstance(value, str) else None


def _question_signals(question):
    """General task cues motivated by reviewed failure types, never qid/Gold rules.

    These flags describe the wording, not proof of difficulty or correctness.
    Category flags avoid counting synonymous words as independent requirements.
    """
    patterns = {
        'synthesis': r'综合|推导|推理|分析|解释原因|\b(?:synthesi[sz]e|infer|reason|analy[sz]e)\b',
        'arithmetic': r'计算|相减|差额|剩余|换算|比率|\b(?:calculate|subtract(?:ion)?|ratio|remaining|remained|conversion)\b',
        'comparison': r'比较|对比|差异|区别|高于|低于|\b(?:compare|versus|difference(?:s)?|lower than|higher than)\b',
        'set_operation': r'交集|差集|共同|排除|\b(?:intersection|excluding|both questions)\b',
        'field_alignment': r'分别|口径|对应|各自|横轴.*纵轴|\b(?:respectively|sample size.*sampling error|horizontal.*vertical)\b',
        'ordering': r'升序|降序|排序|按.*顺序|\b(?:ascending|descending|sorted|order)\b',
        'output_constraint': r'必须|至少|至多|仅限|字以内|小数|\b(?:JSON|must|at least|at most|only|format|decimals?|decimal places)\b',
        'multiple_conditions': r'同时|并且|且|\b(?:each|and)\b',
        'numeric_conditions': r'\d+(?:\.\d+)?\s*(?:MHz|GHz|kHz|W|kW|万元|年|%)',
        'history_reference': r'刚才|上[一个轮次]+|上述|它|该项目|这两种|\b(?:its|that project|that report|that same|same state|those|these two)\b',
    }
    return {key: bool(re.search(pattern, question, re.I)) for key, pattern in patterns.items()}


def extract_features(question, citations, messages=None, *, completeness=None, conflict=None, top1_score_kind=None):
    """Measure actual selected context; heuristics are explicitly ESTIMATED.

    T: task complexity; C: multiple conditions/output constraints; J: logical
    reasoning/comparison/induction; D: cross-paragraph/document dependencies;
    L: input/context load; M: multimodal/structured evidence complexity.
    Character counts and modality presence are observed,
    token estimates and heuristic difficulty are not tokenizer measurements.
    Raw retrieval scores never become relevance probabilities.
    """
    question_observed = isinstance(question, str) and bool(question.strip())
    question = question if isinstance(question, str) else ''
    rows = list(citations or ())
    texts = [row.get('content') for row in rows if isinstance(row.get('content'), str)]
    source_keys = [row.get('fileId') or row.get('file_id') or row.get('source_sha256')
                   or (row.get('source_metadata') or {}).get('source_sha256') for row in rows]
    members = [member for row in rows for member in
               (row.get('source_members') or (row.get('context_group') or {}).get('source_members') or [])]
    member_source_keys = [member.get('file_id') for member in members]
    source_keys += member_source_keys
    identities_complete = all(source_keys)
    distinct_sources = len(set(source_keys)) if identities_complete else None
    top = rows[0] if rows else {}
    top_score = top.get('rerank_score') if top.get('rerank_score') is not None else top.get('score')
    score_kind = top1_score_kind or top.get('score_kind')
    if top.get('rerank_score') is not None:
        score_kind = score_kind or 'RERANK_SCORE'
    valid_score = isinstance(top_score, (int, float)) and not isinstance(top_score, bool) and math.isfinite(top_score)
    metadata = {'question_chars': len(question) if question_observed else None,
                'question_status': 'OBSERVED' if question_observed else 'UNKNOWN',
                'evidence_chars': sum(map(len, texts)) if len(texts) == len(rows) else None,
                'observed_evidence_text_chars': sum(map(len, texts)),
                'evidence_chars_status': {'status': 'OBSERVED' if len(texts) == len(rows) else 'UNKNOWN'},
                'evidence_count': len(rows), 'source_count': distinct_sources,
                'source_count_lower_bound': len(set(x for x in source_keys if x)),
                'source_count_status': {'status': 'OBSERVED' if identities_complete else 'UNKNOWN'},
                'completeness': {'value': completeness, 'status': 'OBSERVED' if completeness is not None else 'UNKNOWN',
                                 'method': 'EXPLICIT_CALLER_OBSERVATION' if completeness is not None else 'NOT_CAPTURED'},
                'conflict': {'value': conflict, 'status': 'OBSERVED' if conflict is not None else 'UNKNOWN',
                             'method': 'EXPLICIT_CALLER_OBSERVATION' if conflict is not None else 'NOT_CAPTURED'},
                'top1': {'value': top_score if valid_score else None,
                         'score_kind': score_kind or 'UNKNOWN', 'status': 'OBSERVED' if valid_score else 'UNKNOWN'}}
    estimated = lambda value, provenance: FeatureValue(value, 'ESTIMATED', provenance)
    paragraph_keys = [row.get('chunk_id') or row.get('id') for row in rows]
    paragraph_keys += [member.get('id') for member in members]
    paragraphs_complete = all(paragraph_keys)
    paragraph_count = len(set(paragraph_keys)) if paragraphs_complete else None
    metadata['paragraph_count'] = paragraph_count
    metadata['paragraph_count_status'] = {'status': 'OBSERVED' if paragraphs_complete else 'UNKNOWN'}
    if completeness is not None:
        _unit(completeness, 'completeness')
    if conflict is not None:
        _unit(conflict, 'conflict')
    signals = _question_signals(question)
    metadata['question_signals'] = signals
    features = {
        'T': estimated(min(1.0, sum(signals[k] for k in ('synthesis', 'arithmetic', 'set_operation', 'field_alignment')) / 2), 'TASK_CATEGORY_CUES_V2'),
        'C': estimated(min(1.0, sum(signals[k] for k in ('field_alignment', 'ordering', 'output_constraint', 'multiple_conditions', 'numeric_conditions')) / 3), 'CONSTRAINT_CATEGORY_CUES_V3'),
        'J': estimated(min(1.0, sum(signals[k] for k in ('comparison', 'arithmetic', 'set_operation')) / 2
                            + (0.5 if re.search(r'推导|推理|归纳|因果|为什么|\b(?:infer|deduce|induce|why|causal)\b', question, re.I) else 0)), 'REASONING_CATEGORY_CUES_V2'),
        'D': estimated(min(1.0, max(max(0, distinct_sources - 1) / 3,
                                  max(0, paragraph_count - 1) / 6,
                                  0.5 if re.search(r'跨段|跨文档|多篇|结合.*段|cross.document|across.*paragraph', question, re.I) else 0)),
                       'SELECTED_SOURCE_PARAGRAPH_DEPENDENCY_V1') if distinct_sources is not None and paragraph_count is not None else FeatureValue(),
        'L': estimated(min(1.0, (len(question) + sum(map(len, texts))) / 12000), 'CHARACTER_LOAD_V1') if len(texts) == len(rows) else FeatureValue(),
        'M': FeatureValue(),
    }
    if not question_observed:
        for key in ('T', 'C', 'J'):
            features[key] = FeatureValue(provenance='QUESTION_TEXT_NOT_CAPTURED')
    image_count = sum(row.get('content_type') in ('image', 'image_url', 'image_ocr', 'image_caption') for row in rows)
    structured_count = sum(any((row.get('source_metadata') or {}).get(key) is not None
                               or row.get(key) is not None for key in ('table_bbox', 'sheet', 'cell', 'row', 'table_id'))
                           or row.get('content_type') in ('table', 'spreadsheet')
                           or str(row.get('source_format') or (row.get('source_metadata') or {}).get('source_format') or '').lower().lstrip('.') in ('xlsx', 'xls', 'csv', 'tsv') for row in rows)
    lexical_tables = sum(bool(re.search(r'(?m)^\s*\|.+\|\s*$|\t.+\t', text)) for text in texts)
    modality_captured = messages is not None and len(texts) == len(rows)
    history_chars = 0
    load_chars = len(question) + sum(map(len, texts))
    load_captured = len(texts) == len(rows)
    load_method = 'SELECTED_QUESTION_EVIDENCE_CHARACTERS_V1'
    if messages is not None:
        prior = list(messages)
        # Actual provider messages already contain the question and system
        # evidence. Count those messages once; citations are separate metadata.
        load_chars = 0
        load_captured = True
        load_method = 'ACTUAL_MESSAGE_TEXT_CHARACTERS_V1'
        history_count = 0
        for index, message in enumerate(prior):
            content = message.get('content') if isinstance(message, dict) else getattr(message, 'content', None)
            role = message.get('role') if isinstance(message, dict) else getattr(message, 'type', None)
            is_history = role not in ('system',) and not (index == len(prior) - 1 and _text(message) == question)
            message_chars = 0
            if isinstance(content, list):
                for block in content:
                    block_type = block.get('type') if isinstance(block, dict) else None
                    if block_type in ('image_url', 'input_image', 'image', 'input_audio', 'audio', 'video'):
                        image_count += 1
                        load_captured = False
                    elif block_type in ('text', 'input_text') and isinstance(block.get('text'), str):
                        message_chars += len(block['text'])
                    else:
                        modality_captured = False
                        load_captured = False
            elif isinstance(content, str):
                message_chars = len(content)
            else:
                modality_captured = False
                load_captured = False
            load_chars += message_chars
            calls = message.get('tool_calls') if isinstance(message, dict) else getattr(message, 'tool_calls', None)
            if calls:
                # Tool arguments also consume input; this remains a character
                # estimate, not a tokenizer or a model context-window contract.
                try:
                    call_chars = len(json.dumps(calls, ensure_ascii=False, sort_keys=True))
                    load_chars += call_chars
                    if is_history:
                        history_chars += call_chars
                except (TypeError, ValueError):
                    load_captured = False
            if is_history:
                history_chars += message_chars
                history_count += 1
        metadata['history_message_count'] = history_count
    else:
        metadata['history_message_count'] = None
    metadata['history_dependency'] = {
        'value': signals['history_reference'] if messages is not None else None,
        'status': 'ESTIMATED' if messages is not None else 'UNKNOWN',
        'method': 'REFERENTIAL_WORDING_WITH_CAPTURED_HISTORY_V1' if messages is not None else 'HISTORY_NOT_CAPTURED',
    }
    if signals['history_reference']:
        if messages is None:
            features['D'] = FeatureValue(provenance='REFERENTIAL_QUESTION_HISTORY_NOT_CAPTURED')
        elif metadata['history_message_count'] and features['D'].status != 'UNKNOWN':
            features['D'] = estimated(max(0.5, features['D'].value), 'SELECTED_DEPENDENCY_AND_ACTUAL_HISTORY_V2')
    # Only caller-supplied evidence observations can affect this component.
    # Raw BM25/vector/rerank scores have no universal calibrated scale.
    if features['D'].status != 'UNKNOWN' and (completeness is not None or conflict is not None):
        features['D'] = estimated(max(features['D'].value,
            1 - completeness if completeness is not None else 0,
            conflict if conflict is not None else 0), 'DEPENDENCY_WITH_EXPLICIT_EVIDENCE_OBSERVATIONS_V2')
    if modality_captured:
        features['M'] = estimated(min(1.0, image_count * 0.5 + structured_count * 0.25 + lexical_tables * 0.15),
                                  'ACTUAL_MODALITY_STRUCTURE_AND_TEXT_DELIMITERS_V1')
    metadata['multimodal_presence'] = {'value': bool(image_count) if modality_captured or image_count else None,
                                      'status': 'OBSERVED' if modality_captured or image_count else 'UNKNOWN',
                                      'method': 'ACTUAL_MESSAGE_BLOCKS_AND_CITATION_CONTENT_TYPE_V1'}
    metadata['structured_evidence_count'] = structured_count
    metadata['structured_text_estimate_count'] = lexical_tables
    metadata['history_chars'] = history_chars
    features['L'] = estimated(min(1.0, load_chars / 12000), load_method) if load_captured else FeatureValue()
    metadata['input_text_chars'] = load_chars
    metadata['input_load'] = {'status': 'OBSERVED' if load_captured else 'UNKNOWN', 'method': load_method}
    metadata['text_only_token_estimate'] = math.ceil(load_chars / 4)
    metadata['token_estimate'] = math.ceil(load_chars / 4) if load_captured else None
    metadata['token_count'] = {'status': 'ESTIMATED' if load_captured else 'UNKNOWN',
                               'method': 'CHARACTERS_DIVIDED_BY_FOUR_V1' if load_captured else 'UNMODELED_NON_TEXT_INPUT'}
    return features, metadata


def score_interval(features, weights):
    total = sum(weights.values())
    lower = sum(weights[k] * v.value for k, v in features.items() if v.status != 'UNKNOWN') / total
    upper = lower + sum(weights[k] for k, v in features.items() if v.status == 'UNKNOWN') / total
    return min(1.0, lower), min(1.0, upper)


def measure_target_field_collision(question, citations):
    """Detect competing table headings for an explicitly named source.

    This matches query text to supplied filenames/identifier prefixes and
    repeated headings, never to a fixed filename, Gold value or answer. A hit
    does not establish that the requested value is present or correct.
    """
    unknown = {'status': 'UNKNOWN', 'value': None, 'method': 'TARGET_FIELD_HEADING_COLLISION_V1'}
    if not isinstance(question, str) or not question.strip() or not citations:
        return unknown
    tokens = re.findall(r'[\w]+', question.casefold())
    normalize = lambda text: ' '.join(re.findall(r'[\w]+', text.casefold()))
    query_phrases = {' '.join(tokens[i:i + n]) for n in range(4, 9)
                     for i in range(len(tokens) - n + 1)}
    rows, targets = [], set()
    observations_complete = True
    for citation in citations:
        source = citation.get('source_file') or citation.get('fileName') or citation.get('filename') or citation.get('file_name')
        content = citation.get('content')
        if not isinstance(source, str) or not source or not isinstance(content, str):
            observations_complete = False
            continue
        # The name is an observed label, NOT a verified file/chunk identity.
        name = source.replace('\\', '/').rsplit('/', 1)[-1].casefold()
        stem = name.rsplit('.', 1)[0]
        source_match = name in question.casefold() or (len(stem) >= 8 and stem in question.casefold())
        source_match |= any(len(t) >= 8 and re.fullmatch(r'[0-9a-f]+', t) and stem.startswith(t) for t in tokens)
        if source_match:
            targets.add(name)
        headings = [normalize(cell) for line in content.splitlines() if '|' in line or '\t' in line
                    for cell in re.split(r'[|\t]', line)]
        matched = {phrase for phrase in query_phrases if any(' ' + phrase + ' ' in ' ' + h + ' ' for h in headings)}
        rows.append((name, matched))
    if len(targets) != 1:
        return {**unknown, 'target_label_count': len(targets)}
    target = next(iter(targets))
    target_phrases = set().union(*(p for name, p in rows if name == target))
    competing = {name for name, phrases in rows if name != target and phrases & target_phrases}
    if not competing and not observations_complete:
        return {**unknown, 'target_label_count': 1, 'reason': 'INCOMPLETE_SOURCE_OBSERVATIONS'}
    return {'status': 'ESTIMATED', 'value': bool(target_phrases and competing),
            'method': 'TARGET_FIELD_HEADING_COLLISION_V1',
            'target_label_count': 1, 'competing_source_label_count': len(competing),
            'target_heading_matched': bool(target_phrases),
            'evidence_completeness': 'UNKNOWN'}


def task_interactions(question, features, metadata):
    """Pre-generation task cues, not observed model competence or Gold coverage.

    Each rule describes a general operation rather than a dataset, topic, name,
    answer, qid or historical verdict. No negative preference for GLM is learned.
    A table/long input alone is deliberately insufficient. Visual structure is
    a risk cue, never a claim that the required image evidence was delivered.
    """
    if not isinstance(question, str) or not question.strip():
        return []
    signals = _question_signals(question)
    matches = []
    collision = metadata.get('target_field_collision', {})
    if collision.get('status') == 'ESTIMATED' and collision.get('value') is True:
        matches.append('TARGET_FIELD_DISTRACTORS')
    if signals['comparison']:
        matches.append('RELATIONAL_COMPARISON')
    if signals['field_alignment']:
        matches.append('ORDERED_FIELD_ALIGNMENT')
    table_query = bool(re.search(r'\b(?:table|tabulated|row|column)\b|表格|表中|行列', question, re.I))
    if signals['numeric_conditions'] and table_query:
        matches.append('CONDITIONED_TABLE_LOOKUP')
    if signals['ordering'] and signals['output_constraint']:
        matches.append('CONSTRAINED_ORDERED_LIST')
    # Two explicit relations: an entity performs a role in a work, then the
    # question asks for a related creator. A plain "who is X" does not match.
    role_link = re.search(r'\b(?:played|portrayed|starred|acted|performed)\b|扮演|饰演|主演', question, re.I)
    creator_query = re.search(r'\b(?:which|what|whose)\b.*\b(?:director|author|writer|composer|creator)\b|哪.*(?:导演|作者|作曲)|谁.*(?:执导|创作)', question, re.I)
    if role_link and creator_query:
        matches.append('ENTITY_RELATION_JOIN')
    visual_anchor = re.search(r'\b(?:PPT|slides?|figure|diagram)\b|图中|图示|课件', question, re.I)
    identity_or_count = re.search(r'\b(?:how many|which|what)\b|多少|几个|哪|什么', question, re.I)
    modality = features.get('M')
    if visual_anchor and identity_or_count and modality and modality.status != 'UNKNOWN' and modality.value > 0:
        matches.append('VISUAL_REFERENCE_WITH_STRUCTURE')
    return matches


def quality_first_candidate(question, citations=None, messages=None):
    """Explicit model preference from general wording families, never competence.

    The optional evidence/message arguments support offline replay parity; no
    source IDs, answers, Gold labels, or historical verdicts enter this rule.
    Evidence eligibility and fixed/OFF modes remain the caller's responsibility.
    These cues estimate benefit and are not calibrated success probabilities.
    """
    if not isinstance(question, str) or not question.strip():
        return {'role': None, 'risk': [], 'defer': [], 'reason': 'QUESTION_UNKNOWN',
                'status': 'UNKNOWN'}
    q = question.casefold()
    risk = []
    if re.search(r'计算|相减|差额|剩余|换算|比率|相隔|多多少|少多少|少多|多多|高多少|低多少|百分[点比]|\b(?:calculate|calculation|subtract|ratio|remaining|remained|exceed|sum|twice|percentage points|more than|less than|how many years|how much .*than)\b', q):
        risk.append('DERIVED_NUMERIC_OPERATION')
    if re.search(r'刚才|同一|上述|它|其|该项目|这张|前.*(?:题|问)|\b(?:its|that report|that project|that year|same|previous|question (?:two|three|\d+)|questions? ago)\b', q):
        risk.append('CONVERSATIONAL_ENTITY_ALIGNMENT')
    if re.search(r'分别|口径|对应|各自|比较|对比|区别|\b(?:respectively|compare|versus|difference)\b', q):
        risk.append('RELATION_OR_FIELD_ALIGNMENT')
    if re.search(r'万元|多少.*(?:元|人|个)|哪位|什么|哪.*(?:位|年)|\b(?:how many|how much|who|where|what time|what date|when|which)\b', q):
        risk.append('SCOPED_FACT_LOOKUP')
    if re.search(r'\band\b|以及|并曾|同时|和.*(?:什么|多少|哪)|如何|为什么|\b(?:how does|why)\b', q):
        risk.append('MULTI_FACT_OR_MECHANISM')
    defer = []
    if re.search(r'\b(?:sequence|mitigations?|preventative follow-ups?|escalation path|rollback|disable path)\b|操作步骤|处置流程|回滚路径|升级路径', q):
        defer.append('PROCEDURAL_COVERAGE')
    if re.search(r'\b(?:env var|environment variable|branch name|email.*(?:channel|slack)|(?:channel|slack).*email)\b|环境变量|分支名', q):
        defer.append('VERBATIM_IDENTIFIER_LOOKUP')
    priority = any(x in risk for x in ('DERIVED_NUMERIC_OPERATION', 'CONVERSATIONAL_ENTITY_ALIGNMENT', 'RELATION_OR_FIELD_ALIGNMENT'))
    role = 'EXPENSIVE' if priority or not defer else 'CHEAP'
    return {'role': role, 'risk': risk, 'defer': defer,
            'reason': 'BENEFIT_RISK_PRIORITY' if priority else 'LOW_OBSERVED_BENEFIT_FAMILY' if defer else 'QUALITY_FIRST_DEFAULT',
            'status': 'ESTIMATED'}


def qwen_risk_v4(question, citations=None, messages=None):
    """Pure pre-generation risk points, never a model failure probability.

    Frozen general wording and context interactions match the offline V4 rule.
    Source labels only measure diversity/format; no benchmark outcome is read.
    Absent citations/history are empty observations, never invented evidence.
    """
    if not isinstance(question, str) or not question.strip():
        return {'score_points': 0, 'signals': {}, 'status': 'UNKNOWN',
                'reason': 'QUESTION_UNKNOWN', 'matched_rules': [],
                'interpretation': 'ESTIMATED_RISK_NOT_PROBABILITY'}
    citations = list(citations or ())
    messages = list(messages or ())
    def _signals(question,citations,messages):
        q=question.casefold();match=lambda p:bool(re.search(p,q,re.I))
        hist=any(m.get('role')=='assistant' and m.get('content') for m in messages)
        sources={c.get('fileId') or c.get('file_id') or c.get('fileName') for c in citations};sources.discard(None)
        structured=any(str(c.get('fileName','')).lower().endswith(('.xlsx','.xls','.csv','.docx')) or c.get('content_type')=='table' for c in citations)
        arithmetic=match(r'计算|相减|差额|剩余|换算|比率|相隔|多多少|少多少|高多少|低多少|百分点|\b(?:calculate|calculation|subtract|ratio|remaining|remained|exceed|sum|twice|percentage points|more than|less than|how many years|how much .*than)\b')
        reference=hist and match(r'刚才|同一|上述|其|该项目|这张|前.*(?:题|问)|\b(?:its|that report|that project|that year|same|previous|question (?:two|three|\d+)|questions? ago)\b')
        financial=match(r'结转|结余|支出|收入|负债|预算|成本|收益率|\b(?:budget|cost|liabilit\w*|equity|revenues?|expenditures?|yield factor|retail price|income)\b')
        multi=question.count('?')+question.count('？')>=2 or match(r'\b(?:and (?:(?:by|at|from|to) )?(?:what|which|who|where|when|how)|what .* and (?:percentage|who)|date and time|sampling approach and percentage)\b|并曾|以及.*(?:谁|多少|什么)|分别|各自')
        mechanism=match(r'\b(?:how does|how .*prevent|why|explain)\b|为什么|如何.*(?:避免|防止)')
        numeric_scope=match(r'\d{4}|万元|千元|million|thousands|\b(?:fiscal|FY\d|table|percentage|dollars|percentages|USD)\b')
        procedure=match(r'\b(?:sequence|mitigations?|preventative follow-ups?|escalation path|rollback|disable path)\b|操作步骤|处置流程|回滚路径|升级路径')
        identifier=match(r'\b(?:env var|environment variable|branch name|metric name|run id|image tag|email.*(?:channel|slack))\b|环境变量|分支名|镜像标签|指标名称|运行标识')
        return dict(arithmetic=arithmetic,history_reference=reference,financial_field=financial,multiple_facts=multi,
                    mechanism=mechanism,numeric_scope=numeric_scope,structured=structured,competing_sources=len(sources)>=3,
                    procedure=procedure,identifier=identifier,has_history=hist,has_evidence=bool(citations))

    def _score(s):
        base=max(45*s['arithmetic'],40*s['history_reference'],35*s['financial_field'],35*s['multiple_facts'],30*s['mechanism'])
        # Context alone cannot upgrade; interactions amplify an identified task.
        if not base:return 0
        value=base+10*s['numeric_scope']+10*(s['structured'] and (s['financial_field'] or s['arithmetic']))+10*s['competing_sources']
        if not (s['arithmetic'] or s['history_reference']):value-=20*(s['procedure'] or s['identifier'])
        # A bare arithmetic cue in ordinary history is not enough to pay for an upgrade.
        if not s['has_evidence'] and not (s['numeric_scope'] or s['financial_field']):value-=10
        return max(0,min(100,value))
    observed = _signals(question, citations, messages)
    points = _score(observed)
    # Primary families use max, not a sum of correlated wording cues.
    primary = {name: weight for name, weight in (
        ('arithmetic', 45), ('history_reference', 40), ('financial_field', 35),
        ('multiple_facts', 35), ('mechanism', 30)) if observed[name]}
    base = max(primary.values(), default=0)
    amplifiers = {
        'numeric_scope': 10 if base and observed['numeric_scope'] else 0,
        'structured_numeric': 10 if base and observed['structured'] and (observed['financial_field'] or observed['arithmetic']) else 0,
        'competing_sources': 10 if base and observed['competing_sources'] else 0,
    }
    discounts = {
        'procedure_or_identifier': 20 if base and not (observed['arithmetic'] or observed['history_reference']) and (observed['procedure'] or observed['identifier']) else 0,
        'bare_history_arithmetic': 10 if base and not observed['has_evidence'] and not (observed['numeric_scope'] or observed['financial_field']) else 0,
    }
    return {'score_points': points, 'signals': observed, 'status': 'ESTIMATED',
            'primary_contributions': primary, 'primary_max_points': base,
            'amplifiers': amplifiers, 'discounts': discounts,
            'matched_rules': [name for name, matched in observed.items() if matched],
            'interpretation': 'ESTIMATED_RISK_NOT_PROBABILITY'}


def _risk_v5_context_length(question, messages):
    """Exact request characters and text observations, never provider tokens.

    These observations have no scoring weight. Missing messages are UNKNOWN,
    including their request/system/history lengths, rather than an empty input.
    """
    result = {'status': 'UNKNOWN' if messages is None else 'OBSERVED',
              'request_chars': None, 'system_text_chars': None,
              'prior_history_chars': None,
              'question_chars': len(question) if isinstance(question, str) else None,
              'interpretation': 'OBSERVE_ONLY_NO_VALIDATED_LENGTH_WEIGHT'}
    if messages is None:
        return result
    from app.services.grounded_request_budget import request_chars
    def text_chars(message):
        content = message.get('content')
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(part['text']) for part in content
                       if isinstance(part, dict) and isinstance(part.get('text'), str))
        return 0
    current = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=len(messages))
    result.update(request_chars=request_chars(messages),
                  system_text_chars=sum(text_chars(m) for m in messages if m.get('role') in ('system', 'developer')),
                  prior_history_chars=sum(text_chars(m) for m in messages[:current] if m.get('role') not in ('system', 'developer')))
    return result


def qwen_risk_v5(question, citations=None, messages=None):
    """Opt-in frozen variant D; context length is observation only.

    V4 signal extraction stays immutable. Only the independent fact-pair weight,
    two task interactions and literal identifier discount differ in this rule.
    """
    messages = None if messages is None else list(messages)
    risk = qwen_risk_v4(question, citations, messages)
    risk['context_length'] = _risk_v5_context_length(question, messages)
    if risk['status'] != 'ESTIMATED':
        risk['extra_signals'] = {}
        return risk
    q = question.casefold()
    match = lambda pattern: bool(re.search(pattern, q, re.I))
    extra = {
        'time_or_measurement': match(r'时间|日期|何时|几点|截止|比例|百分|采样|速率|\b(?:time|date|when|scheduled|deadline|confirm|rate|percentage|percent|sampling|qps|slots?|multipliers?|numeric|compute)\b'),
        'causal_relation': match(r'为此|因此|因而|导致|\b(?:because|therefore|as a result|in response|prevent)\b'),
        'conditional_mechanism': match(r'\b(?:if|when|unless|resume|retry)\b|如果|当.*时|除非|重试|恢复'),
        'literal_identifier_query': match(r'\b(?:command|flag|config key|endpoint|header|content.type|model name|model version|issue id|tracking id)\b|命令|参数名|配置键|接口路径|请求头|模型名称|版本号|工单编号'),
    }
    observed = risk['signals']
    primary = dict(risk['primary_contributions'])
    if observed['multiple_facts']:
        primary['multiple_facts'] = 35 if extra['time_or_measurement'] or extra['causal_relation'] else 25
    base = max(primary.values(), default=0)
    amplifiers = dict(risk['amplifiers'])
    amplifiers.update(history_financial=5 if base and observed['history_reference'] and observed['financial_field'] else 0,
                      conditional_mechanism=10 if base and observed['mechanism'] and extra['conditional_mechanism'] else 0)
    discounts = dict(risk['discounts'])
    discounts['literal_identifier_query'] = 10 if base and extra['literal_identifier_query'] and not (observed['arithmetic'] or observed['history_reference']) else 0
    points = max(0, min(100, base + sum(amplifiers.values()) - sum(discounts.values()))) if base else 0
    risk.update(score_points=points, primary_contributions=primary, primary_max_points=base,
                amplifiers=amplifiers, discounts=discounts, extra_signals=extra,
                matched_rules=risk['matched_rules'] + ['extra.' + name for name, value in extra.items() if value])
    return risk


def qwen_risk_v6(question, citations=None, messages=None):
    """Opt-in semantic variant C, with raw risk independent of route eligibility.

    Frozen V5 observations are refined only for reference/name precision,
    coupled multi-fact tasks and exclusion arithmetic. Length remains observe-only.
    """
    risk = qwen_risk_v5(question, citations, messages)
    if risk['status'] != 'ESTIMATED':
        risk['semantic_signals'] = {}
        return risk
    match = lambda pattern: bool(re.search(pattern, question, re.I))
    observed = dict(risk['signals'])
    reference_pattern = (
        r'刚才|同一|上述|其(?!中|他|余|实|次|后|前)|该项目|这张|前.*(?:题|问)|'
        r'\b(?:its|that report|that project|that year|same|previous|question (?:two|three|\d+)|'
        r'questions? ago)\b')
    reference_question = re.sub(r'\b(?:repeat\w*|reissu\w*|retry\w*|resen\w*|duplicat\w*)\s+(?:\w+\s+){0,2}same\b', '', question, flags=re.I)
    observed['history_reference'] = observed['has_history'] and bool(re.search(reference_pattern, reference_question, re.I))
    mechanism_question = re.sub(r'(?:更名|改名|命名)\s*为\s*什么', '', question)
    observed['mechanism'] = bool(re.search(r'\b(?:how does|how .*prevent|why|explain)\b|为什么|如何.*(?:避免|防止)', mechanism_question, re.I))
    semantic = {
        'measurement': match(r'比例|百分|采样|速率|计算|换算|倍率|乘数|数量|名额|槽位|\b(?:rate|percentage|percent|sampling|qps|slots?|multipliers?|numeric|compute|calculate)\b'),
        'temporal_dependency': match(r'截止|之前|之后|早于|晚于|以便|才能|需要.*确认|(?:日期|时间).{0,30}(?:相同|不同|一致|差异)|\b(?:before|after|until|deadline|by what time|by when|needs? to|must|so that|so the|in order to|same (?:date|time)|earlier|later|difference|duration)\b'),
        'exclusion_arithmetic': match(r'排除|剔除|扣除|去掉|不计|不含|\b(?:exclude|excluding|except|after removing)\b') and match(r'多少|几个|几项|几家|几名|几位|数量|\b(?:how many|how much|number|count)\b'),
    }
    observed['arithmetic'] = observed['arithmetic'] or semantic['exclusion_arithmetic']
    extra = risk['extra_signals']
    semantic['coupled_multiple_facts'] = extra['causal_relation'] or (extra['time_or_measurement'] and (semantic['measurement'] or semantic['temporal_dependency']))
    primary = {name: weight for name, weight in (
        ('arithmetic', 45), ('history_reference', 40), ('financial_field', 35),
        ('multiple_facts', 35 if semantic['coupled_multiple_facts'] else 25),
        ('mechanism', 30)) if observed[name]}
    base = max(primary.values(), default=0)
    amplifiers = {
        'numeric_scope': 10 if base and observed['numeric_scope'] else 0,
        'structured_numeric': 10 if base and observed['structured'] and (observed['financial_field'] or observed['arithmetic']) else 0,
        'competing_sources': 10 if base and observed['competing_sources'] else 0,
        'history_financial': 5 if base and observed['history_reference'] and observed['financial_field'] else 0,
        'conditional_mechanism': 10 if base and observed['mechanism'] and extra['conditional_mechanism'] else 0,
    }
    discounts = {
        'procedure_or_identifier': 20 if base and not (observed['arithmetic'] or observed['history_reference']) and (observed['procedure'] or observed['identifier']) else 0,
        'bare_history_arithmetic': 10 if base and not observed['has_evidence'] and not (observed['numeric_scope'] or observed['financial_field']) else 0,
        'literal_identifier_query': 10 if base and extra['literal_identifier_query'] and not (observed['arithmetic'] or observed['history_reference']) else 0,
    }
    points = max(0, min(100, base + sum(amplifiers.values()) - sum(discounts.values()))) if base else 0
    risk.update(score_points=points, signals=observed, primary_contributions=primary,
                primary_max_points=base, amplifiers=amplifiers, discounts=discounts,
                semantic_signals=semantic,
                matched_rules=[name for name, value in observed.items() if value]
                + ['extra.' + name for name, value in extra.items() if value]
                + ['semantic.' + name for name, value in semantic.items() if value])
    return risk


def qwen_risk_e(question, citations=None, messages=None):
    """Frozen E_THREE_SLOTS: retain V6 and raise evidence-backed multi-slot risk."""
    risk = qwen_risk_v6(question, citations, messages)
    if risk['status'] != 'ESTIMATED' or not any(
            isinstance(c.get('content'), str) and c['content'].strip() for c in citations or ()):
        return risk
    text = question.strip()
    pattern = r'什么|多少|哪(?:些|个|一|两|位|种|家|里)|何(?:时|处)|谁|为何|为什么|如何|怎样|\b(?:what|which|who|where|when|why|how)\b'
    slots = []
    for found in re.finditer(pattern, text, re.I):
        if re.fullmatch(r'[a-z]+', found.group(), re.I):
            prefix = text[:found.start()]
            coordinated = bool(re.search(r'(?:\b(?:and|also|plus)\s+(?:(?:on|at|by|from|for|in|to|then)\s+)*|[,;?]\s*)$', prefix, re.I))
            initial = not slots and (found.group().casefold() in ('what', 'who', 'why', 'how') or not prefix.strip())
            if not (initial or coordinated):
                continue
        if slots and found.start() <= slots[-1][1] + 1:
            slots[-1] = (slots[-1][0], found.end())
        else:
            slots.append((found.start(), found.end()))
    dependency = re.search(r'为什么|原因|导致|以便|才能|之前|之后|如果|除非|相隔|\b(?:cause\w*|why|because|before|after|until|if|unless|when|so that|in order to|prerequisites?)\b', text, re.I)
    facets = re.search(r'(?:what|which)[^?？]{0,240},[^?？]{0,160},?\s+and\s+[^?？]{0,100}(?:checks?|tests?|requirements?|artifacts?|defaults?|steps?|actions?)\b', text, re.I)
    added = []
    if dependency and (len(slots) >= 2 or facets):
        added.append('dependent_answer_slots')
    if len(slots) >= 3:
        added.append('three_independent_requested_slots')
    return {**risk, 'v6_score_points': risk['score_points'], 'explicit_interrogative_slots': len(slots),
            'score_points': max(risk['score_points'], 45 if added else 0),
            'e_rules': added, 'matched_rules': risk['matched_rules'] + added}


def apply_selection_policy(question, features, metadata, config):
    """Shared by live selection and offline replay of captured input features.

    The max avoids adding several correlated wording cues. The floor is an
    explicitly uncalibrated preference, not a failure probability. UNKNOWN
    components retain their interval; no source identity is manufactured.
    """
    lower, upper = score_interval(features, config.weights)
    details = {'policy': config.selection_policy, 'base_score_lower': lower,
               'base_score_upper': upper, 'matched_rules': [], 'status': 'NOT_APPLICABLE'}
    if config.selection_policy in RISK_VERSIONS:
        risk = metadata.get(config.selection_policy.lower().replace('risk_', 'qwen_risk_')) or {}
        known = risk.get('status') == 'ESTIMATED' and isinstance(risk.get('score_points'), (int, float))
        lower = upper = risk['score_points'] / 100 if known else 0.0
        details.update(status='ESTIMATED' if known else 'UNKNOWN',
                       matched_rules=risk.get('matched_rules', []) if known else [],
                       method=RISK_VERSIONS[config.selection_policy], interpretation='ESTIMATED_RISK_NOT_PROBABILITY',
                       calibration='FROZEN_HEURISTIC_NOT_PROBABILITY',
                       reason='RISK_THRESHOLD' if known else 'RISK_OBSERVATIONS_UNKNOWN')
    elif config.selection_policy == 'AFFINITY_V2':
        matches = task_interactions(question, features, metadata)
        details.update(matched_rules=matches, status='ESTIMATED',
                       task_risk_floor=config.task_risk_floor, calibration='UNCALIBRATED')
        if matches:
            lower, upper = max(lower, config.task_risk_floor), max(upper, config.task_risk_floor)
    elif config.selection_policy == 'QUALITY_FIRST_V3':
        candidate = quality_first_candidate(question)
        details.update(status=candidate['status'], role_override=candidate['role'],
                       matched_rules=candidate['risk'] + candidate['defer'],
                       risk_rules=candidate['risk'], defer_rules=candidate['defer'],
                       reason=candidate['reason'],
                       cheap_deferral_reason=candidate['reason'] if candidate['role'] == 'CHEAP' else None,
                       method='GENERAL_WORDING_BENEFIT_PREFERENCE_V1',
                       interpretation='ESTIMATED_BENEFIT_NOT_PROBABILITY',
                       calibration='UNCALIBRATED')
    return lower, upper, details


def decide_route(question, citations, messages=None, retrieval_status='SUCCESS', *, config=None,
                 baseline_model=None, feature_overrides=None, completeness=None, conflict=None,
                 top1_score_kind=None):
    config = config or RouterConfig.from_env()
    common = dict(enabled=config.mode != 'OFF', mode=config.mode, threshold=config.threshold)
    if config.mode == 'OFF':
        return RouteDecision(**common, selected_role='BASELINE', selected_model=baseline_model, reason='ROUTER_OFF')
    # Retrieval errors and insufficiency are separate terminal paths, never an
    # opportunity to hide missing evidence by buying a different answer model.
    if retrieval_status != 'SUCCESS':
        reason = ('RETRIEVAL_FAILURE' if retrieval_status in ('RETRIEVAL_ERROR', 'RETRIEVAL_FAILURE') else
                  'INSUFFICIENT_EVIDENCE' if retrieval_status in ('NO_RESULTS', 'LOW_RELEVANCE', 'INSUFFICIENT_EVIDENCE') else 'RETRIEVAL_STATUS_UNKNOWN')
        return RouteDecision(**common, selected_role='NONE', selected_model=None, reason=reason)
    if completeness is not None:
        _unit(completeness, 'completeness')
    if not citations or completeness == 0:
        return RouteDecision(**common, selected_role='NONE', selected_model=None, reason='INSUFFICIENT_EVIDENCE')
    features, metadata = extract_features(question, citations, messages, completeness=completeness,
                                          conflict=conflict, top1_score_kind=top1_score_kind)
    if config.selection_policy == 'RISK_V4':
        metadata['qwen_risk_v4'] = qwen_risk_v4(question, citations, messages)
    elif config.selection_policy == 'RISK_V5':
        metadata['qwen_risk_v5'] = qwen_risk_v5(question, citations, messages)
    elif config.selection_policy == 'RISK_V6':
        metadata['qwen_risk_v6'] = qwen_risk_v6(question, citations, messages)
    elif config.selection_policy == 'RISK_E':
        metadata['qwen_risk_e'] = qwen_risk_e(question, citations, messages)
    metadata['weights'] = dict(config.weights)
    metadata['unknown_policy'] = {'mode': config.unknown_policy}
    metadata['model_roles'] = {'cheap': {'model': config.cheap_model},
                               'expensive': {'model': config.expensive_model}}
    for key, value in (feature_overrides or {}).items():
        if key not in FEATURE_NAMES or not isinstance(value, FeatureValue):
            raise ValueError('feature overrides require named FeatureValue observations')
        features[key] = value
    if config.selection_policy == 'AFFINITY_V2':
        metadata['target_field_collision'] = measure_target_field_collision(question, citations)
    lower, upper, policy = apply_selection_policy(question, features, metadata, config)
    metadata['selection_policy'] = policy
    metadata['rule_version'] = {'AFFINITY_V2': AFFINITY_VERSION,
                                'QUALITY_FIRST_V3': QUALITY_FIRST_VERSION}.get(config.selection_policy, RULE_VERSION)
    if config.selection_policy == 'RISK_V4':
        metadata['rule_version'] = RISK_V4_VERSION
    elif config.selection_policy == 'RISK_V5':
        metadata['rule_version'] = RISK_V5_VERSION
    elif config.selection_policy == 'RISK_V6':
        metadata['rule_version'] = RISK_V6_VERSION
    elif config.selection_policy == 'RISK_E':
        metadata['rule_version'] = RISK_E_VERSION
    if config.mode in ('CHEAP_ONLY', 'EXPENSIVE_ONLY'):
        role = 'CHEAP' if config.mode == 'CHEAP_ONLY' else 'EXPENSIVE'
        reason = 'FIXED_MODE'
    elif config.selection_policy in RISK_VERSIONS:
        role = 'EXPENSIVE' if policy['status'] == 'ESTIMATED' and lower >= config.threshold else 'CHEAP'
        reason = ('RISK_AT_OR_ABOVE_THRESHOLD' if role == 'EXPENSIVE' else
                  'RISK_OBSERVATIONS_UNKNOWN_CHEAP' if policy['status'] == 'UNKNOWN' else 'RISK_BELOW_THRESHOLD')
    elif config.selection_policy == 'QUALITY_FIRST_V3':
        role = policy['role_override'] or config.unknown_policy
        reason = policy['reason'] if policy['role_override'] else 'QUESTION_UNKNOWN_' + config.unknown_policy
    elif upper < config.threshold:
        role, reason = 'CHEAP', 'INTERVAL_BELOW_THRESHOLD'
    elif lower >= config.threshold:
        role = 'EXPENSIVE'
        reason = ('TASK_INTERACTION_RISK_FLOOR' if policy['matched_rules'] and
                  policy['base_score_lower'] < config.threshold else 'INTERVAL_AT_OR_ABOVE_THRESHOLD')
    else:
        role, reason = config.unknown_policy, 'UNCERTAIN_INTERVAL_' + config.unknown_policy
    target = {'BASELINE': baseline_model, 'CHEAP': config.cheap_model, 'EXPENSIVE': config.expensive_model}[role]
    return RouteDecision(**common, selected_role=role, selected_model=target, reason=reason,
                         score_lower=lower, score_upper=upper, features=features, metadata=metadata)


@dataclass(frozen=True)
class EscalationDecision:
    should_retry: bool
    selected_model: Optional[str]
    reason: str

    def to_payload(self):
        return {'should_retry': self.should_retry, 'model': self.selected_model, 'reason': self.reason}


def decide_escalation(decision, *, completion_status, quality_status='UNKNOWN', retries_used=0,
                      provider_fallback=False, unsupported=False, config=None):
    """At most one extra call; caller supplies a deterministic quality outcome.

    This function neither inspects answers nor runs a validator. UNKNOWN and
    NOT_RUN quality never imply failure and never cause a retry.
    """
    config = config or RouterConfig.from_env()
    deny = lambda reason: EscalationDecision(False, None, reason)
    if config.mode != 'DYNAMIC' or decision.mode != 'DYNAMIC' or decision.selected_role != 'CHEAP':
        return deny('NOT_DYNAMIC_CHEAP')
    if retries_used != 0:
        return deny('RETRY_LIMIT')
    if completion_status == 'UNKNOWN' or quality_status in ('UNKNOWN', 'NOT_RUN'):
        return deny('OUTCOME_UNKNOWN')
    if provider_fallback and not config.escalate_on_provider_fallback:
        return deny('PROVIDER_FALLBACK_DISABLED')
    if unsupported and not config.escalate_on_unsupported:
        return deny('UNSUPPORTED_DISABLED')
    if completion_status == 'TRUNCATED':
        if config.escalate_on_truncated:
            return EscalationDecision(True, config.expensive_model, 'TRUNCATED_OPT_IN')
        return deny('TRUNCATED_DISABLED')
    if completion_status != 'COMPLETED':
        return deny('NOT_COMPLETED')
    if quality_status == 'FAIL' and config.escalate_on_quality_fail:
        return EscalationDecision(True, config.expensive_model, 'DETERMINISTIC_QUALITY_FAIL')
    return deny('NO_DETERMINISTIC_FAIL')
