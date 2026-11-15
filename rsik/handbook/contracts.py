"""Finite, versioned handbook predicates and action vocabulary."""
from ..contracts import ContractError, object_fields, string_list, text


DOMAINS = {'format': {'PDF', 'DOCX', 'XLSX', 'OTHER'}, 'budget_class': {'SMALL', 'NORMAL', 'LARGE'},
           **{name: {True, False} for name in ('same_source', 'same_table', 'same_numeric_value',
                       'has_independent_hit', 'has_header', 'has_unit')}}
ACTION_TARGETS = {'PRESERVE_BOTH': 'MEMBER_PAIR', 'DROP_DUPLICATE': 'MEMBER_PAIR',
                  'PRIORITIZE_FIRST': 'MEMBER_PAIR', 'PRIORITIZE_LAST': 'MEMBER_PAIR',
                  'LIMIT_LONG_MEMBERS': 'NONSEED_MEMBERS', 'PREFER_INDEPENDENT_HITS': 'SELECTED_MEMBERS',
                  'FILTER_SHORT_NONSEED_TAILS':'NONSEED_MEMBERS'}
OPPOSITES = {frozenset(('PRESERVE_BOTH', 'DROP_DUPLICATE')),
             frozenset(('PRIORITIZE_FIRST', 'PRIORITIZE_LAST'))}


def condition_domains(conditions):
    if not isinstance(conditions, list) or len(conditions) > 16:
        raise ContractError('INVALID_HANDBOOK_CONDITION')
    domains = {key: set(values) for key, values in DOMAINS.items()}
    for clause in conditions:
        object_fields(clause, ('field', 'op', 'value'))
        field, operation, value = clause['field'], clause['op'], clause['value']
        if field not in DOMAINS:
            raise ContractError('INVALID_HANDBOOK_CONDITION')
        if field not in {'format', 'budget_class'}:
            if operation != 'eq' or type(value) is not bool:
                raise ContractError('INVALID_HANDBOOK_CONDITION')
            allowed = {value}
        else:
            if operation not in {'eq', 'in'}:
                raise ContractError('INVALID_HANDBOOK_CONDITION')
            values = [value] if operation == 'eq' else value
            if not isinstance(values, list) or not values or any(type(v) is not str or v not in DOMAINS[field] for v in values):
                raise ContractError('INVALID_HANDBOOK_CONDITION')
            allowed = set(values)
        domains[field] &= allowed
        if not domains[field]:
            raise ContractError('UNSATISFIABLE_HANDBOOK_CONDITION')
    return domains


def validate_entry(entry):
    object_fields(entry, ('section', 'applicable_format', 'conditions', 'action', 'explanation',
                          'falsification', 'support_refs', 'compatibility_fingerprint', 'supersedes'))
    if entry['section'] not in {'dedup', 'budget', 'order', 'format'}:
        raise ContractError()
    string_list(entry['applicable_format'], limit=4)
    if not entry['applicable_format'] or set(entry['applicable_format']) - DOMAINS['format']:
        raise ContractError()
    domains = condition_domains(entry['conditions'])
    if not domains['format'] & set(entry['applicable_format']):
        raise ContractError('UNSATISFIABLE_HANDBOOK_CONDITION')
    action = entry['action']
    object_fields(action, ('name', 'target', 'params'))
    if action['name'] not in ACTION_TARGETS or action['target'] != ACTION_TARGETS[action['name']]:
        raise ContractError('INVALID_HANDBOOK_ACTION')
    if action['name'] == 'LIMIT_LONG_MEMBERS':
        object_fields(action['params'], ('max_chars',))
        if type(action['params']['max_chars']) is not int or not 1 <= action['params']['max_chars'] <= 4000:
            raise ContractError('INVALID_HANDBOOK_ACTION')
    elif action['name']=='FILTER_SHORT_NONSEED_TAILS':
        object_fields(action['params'],('reference','numerator','denominator'))
        p=action['params']
        if (p['reference']!='LONGEST_SEED_CONTENT_LENGTH' or type(p['numerator']) is not int
                or type(p['denominator']) is not int or not 0<p['numerator']<=p['denominator']<=8):
            raise ContractError('INVALID_HANDBOOK_ACTION')
    elif action['params'] != {}:
        raise ContractError('INVALID_HANDBOOK_ACTION')
    for field in ('explanation', 'falsification', 'compatibility_fingerprint'):
        text(entry[field])
    string_list(entry['support_refs'])
    string_list(entry['supersedes'])
    return entry


def conditions_match(entry, features):
    for clause in entry['conditions']:
        field = clause['field']
        if field not in features or features[field] == 'UNKNOWN':
            return False
        actual = features[field]
        if type(actual) is not (str if field in {'format', 'budget_class'} else bool):
            return False
        if actual not in condition_domains([clause])[field]:
            return False
    return features.get('format') in entry['applicable_format']


def conflicting(left, right):
    if (left['compatibility_fingerprint'] != right['compatibility_fingerprint']
            or not set(left['applicable_format']) & set(right['applicable_format'])
            or left['action']['target'] != right['action']['target']
            or frozenset((left['action']['name'], right['action']['name'])) not in OPPOSITES):
        return False
    a, b = condition_domains(left['conditions']), condition_domains(right['conditions'])
    a['format'] &= set(left['applicable_format'])
    b['format'] &= set(right['applicable_format'])
    return all(a[field] & b[field] for field in DOMAINS)
