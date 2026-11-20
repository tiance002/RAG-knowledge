"""Read-only, host-selected exact-input controls; never infer model quality or tokens."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from rsik.state import canonical, digest


ARMS = {'control': (False, False), 'search-memory': (True, False),
        'handbook': (False, True), 'combined': (True, True)}
HANDBOOK_INSTRUCTIONS = (
    ' Handbook CONSTRAINT_RISK means a relative change moved against a bound. '
    'CONFIRMED_FAIL requires a verified absolute value outside the bound. '
    'Risk and UNKNOWN do not establish that a constraint is satisfied. '
    'Read hard_constraint_statuses in the order of the objective constraints.'
)


def compare_experience_inputs(arms, *, permitted_directory):
    """Compare four independent receipts with host-pinned receipt/wire SHA256s.

    Each arm supplies ``receipt_path``, ``receipt_sha256`` and ``wire_sha256``.
    Only that receipt and its same-basename adjacent request are read. The
    returned report contains hashes/counts only, never request payload bodies.
    Provider usage is UNKNOWN: input receipts alone cannot establish billing.
    """
    if set(arms) != set(ARMS):
        raise ValueError('ABLATION_FOUR_ARMS_REQUIRED')
    root = Path(permitted_directory).resolve(strict=True)
    normalized, rows, experiences, paths = [], [], {}, set()
    try:
        for arm, (want_memory, want_handbook) in ARMS.items():
            spec = arms[arm]
            path = Path(spec['receipt_path']).resolve(strict=True)
            if not path.is_relative_to(root) or not path.name.endswith('.input-receipt.json'):
                raise ValueError('ABLATION_RECEIPT_PATH')
            if path in paths:
                raise ValueError('ABLATION_INDEPENDENT_ARMS_REQUIRED')
            paths.add(path)
            receipt_bytes = path.read_bytes()
            if sha256(receipt_bytes).hexdigest() != spec['receipt_sha256']:
                raise ValueError('ABLATION_RECEIPT_HASH')
            receipt = json.loads(receipt_bytes)
            key = path.name.removesuffix('.input-receipt.json')
            filename = key + '.request.json'
            if (receipt['schema'] != 'rsi-request-input-receipt/v1'
                    or receipt['request_key'] != key
                    or receipt['request_file'] != filename or receipt['request_wire_file'] != filename):
                raise ValueError('ABLATION_ADJACENT_REQUEST_REQUIRED')
            wire_path = (path.parent / filename).resolve(strict=True)
            if wire_path.parent != path.parent or not wire_path.is_relative_to(root):
                raise ValueError('ABLATION_WIRE_PATH')
            wire = wire_path.read_bytes()
            wire_hash = sha256(wire).hexdigest()
            if wire_hash != spec['wire_sha256'] or wire_hash != receipt['request_wire_sha256']:
                raise ValueError('ABLATION_WIRE_HASH')
            body = json.loads(wire.decode('utf-8'))
            if len(wire) != receipt['request_body_bytes'] or digest(body) != receipt['request_hash']:
                raise ValueError('ABLATION_WIRE_MEASUREMENT')
            content = body['input'][1]['content']
            if len(content.encode('utf-8')) != receipt['user_message_utf8_bytes']:
                raise ValueError('ABLATION_USER_MEASUREMENT')
            context = json.loads(content)
            public = context['public']
            if key != public['round_id'] + '-' + receipt['role'] + '-' + str(receipt['attempt']):
                raise ValueError('ABLATION_REQUEST_IDENTITY')
            memory = public.get('search_memory')
            handbook = public.get('handbook_entries')
            has_memory = bool(memory and memory.get('entries'))
            has_handbook = bool(handbook)
            if receipt.get('search_memory_entries_in_request') != (len(memory.get('entries', [])) if memory is not None else None):
                raise ValueError('ABLATION_MEMORY_MEASUREMENT')
            history = public.get('search_history')
            if receipt.get('search_history_items_in_request') != (len(history) if history is not None else None):
                raise ValueError('ABLATION_HISTORY_MEASUREMENT')
            if receipt.get('search_context_canonical_bytes') is not None and receipt['search_context_canonical_bytes'] != len(canonical({'search_history': history, 'search_memory': memory})):
                raise ValueError('ABLATION_SEARCH_MEASUREMENT')
            if (has_memory, has_handbook) != (want_memory, want_handbook):
                raise ValueError('ABLATION_EXPERIENCE_PRESENCE')
            if not want_memory and memory is not None:
                raise ValueError('ABLATION_EMPTY_MEMORY_IS_NOT_ABSENCE')
            ids = public['handbook_entry_ids']
            if ids != [entry['entry_id'] for entry in handbook or []]:
                raise ValueError('ABLATION_HANDBOOK_IDS')
            for name, value, included in [('memory', memory, has_memory), ('handbook', handbook, has_handbook)]:
                if included:
                    if name in experiences and experiences[name] != value:
                        raise ValueError('ABLATION_EXPERIENCE_CHANGED')
                    experiences[name] = deepcopy(value)
            if any('objective_applicability' in entry for entry in handbook or []):
                instructions = context.get('objective_instructions', '')
                if not instructions.endswith(HANDBOOK_INSTRUCTIONS):
                    raise ValueError('ABLATION_HANDBOOK_INSTRUCTIONS')
                context['objective_instructions'] = instructions[:-len(HANDBOOK_INSTRUCTIONS)]
            public.pop('search_memory', None)
            public.pop('handbook_entries', None)
            public['handbook_entry_ids'] = []
            body['input'][1]['content'] = canonical(context).decode('utf-8')
            # Preserve every other field, including parent, round, observations,
            # diagnosis, objective, model, actor prompts and cache settings.
            normalized.append((body, receipt['role'], receipt['attempt']))
            rows.append({'arm': arm, 'request_body_utf8_bytes': len(wire),
                         'user_message_utf8_bytes': len(content.encode('utf-8')),
                         'receipt_sha256': spec['receipt_sha256'], 'request_wire_sha256': wire_hash,
                         'provider_token_usage': 'UNKNOWN', 'answer_quality': 'NOT_RUN'})
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, OSError, AttributeError):
        raise ValueError('ABLATION_INVALID_EVIDENCE') from None
    if any(value != normalized[0] for value in normalized[1:]):
        raise ValueError('ABLATION_UNRELATED_INPUT_CHANGED')
    for row in rows:
        row['request_body_utf8_bytes_delta'] = row['request_body_utf8_bytes'] - rows[0]['request_body_utf8_bytes']
        row['user_message_utf8_bytes_delta'] = row['user_message_utf8_bytes'] - rows[0]['user_message_utf8_bytes']
    return {'schema': 'rsi-experience-input-ablation/v1', 'input_control': 'VERIFIED',
            'measurement_scope': 'EXACT_HTTP_REQUEST_BODY', 'rows': rows,
            'provider_token_usage': 'UNKNOWN', 'answer_quality': 'NOT_RUN',
            'model_effect': 'NOT_RUN'}
