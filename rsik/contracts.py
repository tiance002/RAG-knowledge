"""Strict role contracts. Model confidence is metadata, never a decision input."""
from __future__ import annotations

import math
import hashlib
import re
import unicodedata


VERSION = 'rsi-v2.2'
ANGLES = ('P1_DEDUP', 'P2_BUDGET', 'P3_ORDER')


class ContractError(ValueError):
    def __init__(self, code='CONTRACT_ERROR'):
        self.code = code
        super().__init__(code)  # never echo rejected values or private evidence


def object_fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ContractError()


def text(value, *, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError()
    return value


def string_list(value, *, limit=32):
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(v, str) or not v for v in value):
        raise ContractError()
    if len(value) != len(set(value)):
        raise ContractError()
    return value


def confidence(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ContractError()


def validate_diagnosis(value, visible):
    required = ('schema_version', 'observation_refs', 'problem_layer', 'first_deviation', 'evidence_refs',
                'evidence_quote', 'hypothesis', 'fixable_within_seam', 'required_data', 'confidence')
    object_fields(value, required)
    if value['schema_version'] != VERSION or value['problem_layer'] not in {'SELECTION', 'WORKFLOW', 'PROMPT', 'DATA', 'UNKNOWN'}:
        raise ContractError()
    for field in ('first_deviation', 'hypothesis'):
        text(value[field])
    string_list(value['required_data'])
    string_list(value['observation_refs'])
    string_list(value['evidence_refs'])
    if not value['observation_refs'] or set(value['observation_refs']) - set(visible['observation_refs']):
        raise ContractError('OBSERVATION_INVALID')
    if set(value['evidence_refs']) - set(visible['evidence_quotes']):
        raise ContractError('HALLUCINATED_QUOTE')
    if value['evidence_quote'] is not None:
        if not isinstance(value['evidence_quote'], str) or value['evidence_quote'] not in [
                visible['evidence_quotes'][ref] for ref in value['evidence_refs']]:
            raise ContractError('HALLUCINATED_QUOTE')
    if type(value['fixable_within_seam']) is not bool:
        raise ContractError()
    confidence(value['confidence'])
    return value


def validate_proposal(value, visible, angle):
    fields = ('schema_version', 'proposal_id', 'round_id', 'angle', 'parent_id', 'observation_ref',
              'hypothesis', 'change', 'expected_impact', 'falsification', 'worker_patch', 'evidence_refs',
              'handbook_entry_ids', 'risk_assessment', 'confidence')
    object_fields(value, fields)
    if value['schema_version'] != VERSION or value['angle'] != angle or angle not in ANGLES:
        raise ContractError()
    for name in ('proposal_id', 'hypothesis', 'change', 'expected_impact', 'falsification', 'risk_assessment'):
        text(value[name])
    if value['round_id'] != visible['round_id'] or value['parent_id'] != visible['parent_alias']:
        raise ContractError('OBSERVATION_INVALID')
    if value['observation_ref'] not in visible['observation_refs']:
        raise ContractError('OBSERVATION_INVALID')
    normalized = ''.join(c for c in unicodedata.normalize('NFKC', value['falsification']).casefold() if not c.isspace())
    if normalized in {'none', 'n/a', 'na', 'null', 'unknown', '无', '暂无', '不适用', '未知'}:
        raise ContractError()
    object_fields(value['worker_patch'], ('old', 'new'))
    for name in ('old', 'new'):
        text(value['worker_patch'][name], maximum=16_384)
    if value['worker_patch']['old'] == value['worker_patch']['new']:
        raise ContractError('PATCH_INVALID')
    string_list(value['evidence_refs'])
    string_list(value['handbook_entry_ids'], limit=3)
    if set(value['evidence_refs']) - set(visible['evidence_quotes']):
        raise ContractError('HALLUCINATED_QUOTE')
    if set(value['handbook_entry_ids']) - set(visible.get('handbook_entry_ids', [])):
        raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
    confidence(value['confidence'])
    return value


def role_schema(role):
    """Provider JSON Schema plus local business/reference validation above."""
    string = {'type': 'string'}
    strings = {'type': 'array', 'items': string}
    number = {'type': 'number'}
    if role == 'diagnose':
        properties = {'schema_version': {'type': 'string', 'enum': [VERSION]}, 'observation_refs': strings,
                      'problem_layer': {'type': 'string', 'enum': ['SELECTION', 'WORKFLOW', 'PROMPT', 'DATA', 'UNKNOWN']},
                      'first_deviation': string, 'evidence_refs': strings, 'evidence_quote': {'type': ['string', 'null']},
                      'hypothesis': string, 'fixable_within_seam': {'type': 'boolean'},
                      'required_data': strings, 'confidence': number}
    elif role in ANGLES:
        properties = {name: string for name in ('schema_version', 'proposal_id', 'round_id', 'parent_id',
                                                'observation_ref', 'hypothesis', 'change', 'expected_impact',
                                                'falsification', 'risk_assessment')}
        properties.update(angle={'type': 'string', 'enum': [role]}, evidence_refs=strings,
                          handbook_entry_ids=strings, confidence=number,
                          worker_patch={'type': 'object', 'properties': {'old': string, 'new': string},
                                        'required': ['old', 'new'], 'additionalProperties': False})
        properties['schema_version'] = {'type': 'string', 'enum': [VERSION]}
    elif role == 'curator':
        clause = {'type': 'object', 'properties': {'field': {'type':'string', 'enum':[
            'format','budget_class','same_source','same_table','same_numeric_value','has_independent_hit','has_header','has_unit']},
            'op': {'type':'string','enum':['eq']}, 'value': {'anyOf':[{'type':'string'},{'type':'boolean'}]}},
            'required':['field','op','value'], 'additionalProperties':False}
        action = {'type':'object', 'properties': {'name':{'type':'string','enum':[
            'PRESERVE_BOTH','DROP_DUPLICATE','PRIORITIZE_FIRST','PRIORITIZE_LAST','LIMIT_LONG_MEMBERS','PREFER_INDEPENDENT_HITS','FILTER_SHORT_NONSEED_TAILS']},
            'target':{'type':'string','enum':['MEMBER_PAIR','NONSEED_MEMBERS','SELECTED_MEMBERS']},
            'params':{'anyOf':[{'type':'object','properties':{},'required':[],'additionalProperties':False},
                {'type':'object','properties':{'max_chars':{'type':'integer'}},'required':['max_chars'],'additionalProperties':False},
                {'type':'object','properties':{'reference':{'type':'string','enum':['LONGEST_SEED_CONTENT_LENGTH']},
                    'numerator':{'type':'integer'},'denominator':{'type':'integer'}},
                 'required':['reference','numerator','denominator'],'additionalProperties':False}]}},
            'required':['name','target','params'],'additionalProperties':False}
        entry_properties = {'section':{'type':'string','enum':['dedup','budget','order','format']},
            'applicable_format':strings, 'conditions':{'type':'array','items':clause}, 'action':action,
            **{k:string for k in ('explanation','falsification','compatibility_fingerprint')},
            'support_refs':strings, 'supersedes':strings}
        entry={'type':'object','properties':entry_properties,'required':list(entry_properties),'additionalProperties':False}
        properties={'schema_version':{'type':'string','enum':[VERSION]}, 'op':{'type':'string','enum':['ADD','REVISE','DEPRECATE']},
            'target_entry_id':{'type':['string','null']}, 'proposed_entry':{'anyOf':[entry,{'type':'null'}]},
            'evidence_refs':strings,'reason':string}
    elif role == 'meta':
        finding_properties={'category':{'type':'string','enum':['COVERAGE','FRESHNESS','FALSE_PASS_SUSPECTED','UNKNOWN_ATTRIBUTION']},
            'evidence_refs':strings, 'observation':string, 'certainty':{'type':'string','enum':['SUPPORTED','UNKNOWN']}}
        properties={'schema_version':{'type':'string','enum':[VERSION]},
            'findings':{'type':'array','items':{'type':'object','properties':finding_properties,
                'required':list(finding_properties),'additionalProperties':False}}, 'limitations':strings}
    else:
        raise ValueError('unsupported role schema')
    body = {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['OK', 'ABSTAIN']},
        'result': {'anyOf': [body, {'type': 'null'}]}, 'reason': {'type': ['string', 'null']}},
        'required': ['status', 'result', 'reason'], 'additionalProperties': False}


def unwrap_response(value):
    object_fields(value, ('status', 'result', 'reason'))
    if value['status'] == 'ABSTAIN':
        if value['result'] is not None:
            raise ContractError()
        text(value['reason'])
        return None
    if value['status'] != 'OK' or not isinstance(value['result'], dict) or value['reason'] is not None:
        raise ContractError()
    return value['result']


def validate_readonly(value, visible, role):
    if role == 'meta':
        object_fields(value, ('schema_version','findings','limitations'))
        if value['schema_version'] != VERSION or not isinstance(value['findings'], list) or len(value['findings'])>16:
            raise ContractError()
        string_list(value['limitations'])
        for finding in value['findings']:
            object_fields(finding, ('category','evidence_refs','observation','certainty'))
            if finding['category'] not in {'COVERAGE','FRESHNESS','FALSE_PASS_SUSPECTED','UNKNOWN_ATTRIBUTION'} or finding['certainty'] not in {'SUPPORTED','UNKNOWN'}:
                raise ContractError()
            text(finding['observation']);string_list(finding['evidence_refs'])
            if set(finding['evidence_refs'])-set(visible['evidence_quotes']): raise ContractError('HALLUCINATED_QUOTE')
        return value
    if role != 'curator': raise ContractError()
    object_fields(value, ('schema_version','op','target_entry_id','proposed_entry','evidence_refs','reason'))
    if value['schema_version'] != VERSION or value['op'] not in {'ADD','REVISE','DEPRECATE'}: raise ContractError()
    text(value['reason']);string_list(value['evidence_refs'])
    if not value['evidence_refs'] or set(value['evidence_refs'])-set(visible['evidence_quotes']): raise ContractError('HALLUCINATED_QUOTE')
    if value['op']=='DEPRECATE':
        if value['target_entry_id'] not in visible['handbook_entry_ids'] or value['proposed_entry'] is not None: raise ContractError()
    else:
        from .handbook.contracts import validate_entry
        validate_entry(value['proposed_entry'])
        if value['op']=='ADD' and (value['target_entry_id'] is not None or value['proposed_entry']['supersedes']): raise ContractError()
        if value['op']=='REVISE' and (value['target_entry_id'] not in visible['handbook_entry_ids'] or value['proposed_entry']['supersedes'] != [value['target_entry_id']]): raise ContractError()
        if set(value['proposed_entry']['support_refs'])-set(value['evidence_refs']): raise ContractError()
    return value


def validate_completed_candidate(value,visible,role):
    """A workflow-attested binding, available only to post-experiment roles."""
    if role not in {'curator','meta'}:raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
    object_fields(value,('candidate_alias','code_sha256','quality_status','evidence_refs','implemented_action',
                         'chars_before','chars_after','robustness_passed','scope'))
    if (not isinstance(value['candidate_alias'],str) or not re.fullmatch(r'candidate-[0-9a-f]{32}',value['candidate_alias'])
            or value['code_sha256']!=hashlib.sha256(visible['parent_code'].encode()).hexdigest()
            or value['quality_status']!='VERIFIED' or value['robustness_passed'] is not True
            or value['scope']!='FROZEN_SOURCE_CLAUSE_PRESERVATION_ONLY'
            or any(type(value[k]) is not int or value[k]<0 for k in ('chars_before','chars_after'))):
        raise ContractError('UNBOUND_COMPLETED_CANDIDATE')
    string_list(value['evidence_refs'])
    if not value['evidence_refs'] or set(value['evidence_refs'])-set(visible['evidence_quotes']):
        raise ContractError('UNBOUND_COMPLETED_CANDIDATE')
    from .handbook.contracts import validate_entry
    validate_entry({'section':'budget','applicable_format':['OTHER'],'conditions':[],
        'action':value['implemented_action'],'explanation':'Workflow-attested implemented action.',
        'falsification':'Necessary fact loss rejects a future use.','support_refs':[],
        'compatibility_fingerprint':'workflow-schema-check','supersedes':[]})
    return value
