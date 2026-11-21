"""Recover only untouched work from an isolated, drained panel pause."""

from hashlib import sha256
import json
import os
from tempfile import NamedTemporaryFile


def _read_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Missing or indirect panel artifact: {path.name}')
    return path.read_bytes()


def publish_carried_receipt(directory, name, raw):
    """Publish the exact bytes under a new attempt without modifying the source."""
    with NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = stream.name
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.link(temporary, directory / name)
        finally:
            stream.close()
            os.unlink(temporary)


def frozen_pause(source, completion_sha256, binding, cases, attempt_id):
    """Return the original receipt bytes and the strictly unsent cases."""
    raw = _read_file(source / 'completion.json')
    if sha256(raw).hexdigest() != completion_sha256:
        raise ValueError('Prior completion hash mismatch')
    completion = json.loads(raw)
    if (completion.get('state') != 'FAILED' or completion.get('capacity_paused') is not True
            or completion.get('cases_all_attempted') is not False
            or completion.get('case_count') != len(cases)
            or any(completion.get(key) != value for key, value in binding.items())
            or not completion.get('attempt_id') or completion['attempt_id'] == attempt_id):
        raise ValueError('Prior attempt is not a distinct capacity pause for this execution')

    index_bytes = _read_file(source / 'case-files.json')
    if sha256(index_bytes).hexdigest() != completion.get('case_files_sha256'):
        raise ValueError('Prior receipt index hash mismatch')
    index = json.loads(index_bytes)
    names = index['files']
    ordinals = [case['ordinal'] for case in cases]
    pending = completion['pending_ordinals']
    if (not pending or any(type(value) is not int for value in pending)
            or pending != ordinals[-len(pending):]):
        raise ValueError('Pending work must be a nonempty untouched suffix')
    all_expected = {f"{case['ordinal']:02d}-case.json" for case in cases
                    if case['ordinal'] not in pending}
    if (len(all_expected) != index['cases'] or all_expected != set(names)
            or all_expected != {path.name for path in source.glob('*-case.json')}):
        raise ValueError('Prior pause does not account for every attempted case')

    receipts = {}
    valid_count = 0
    untouched = []
    for case in cases:
        ordinal = case['ordinal']
        stem = f'{ordinal:02d}'
        if ordinal in pending:
            marker = json.loads(_read_file(source / f'{stem}-state.json'))
            if (marker.get('state') != 'PENDING' or marker.get('case_id') != case['item_id']
                    or marker.get('ordinal') != ordinal
                    or any((source / 'attempts').glob(f'{stem}-try*.json'))):
                raise ValueError('Pending case has signs of an earlier attempt')
            untouched.append(case)
            continue
        name = f'{stem}-case.json'
        receipt_bytes = _read_file(source / name)
        if sha256(receipt_bytes).hexdigest() != names[name]:
            raise ValueError('Prior case receipt hash mismatch')
        receipt = json.loads(receipt_bytes)
        if (receipt.get('case_id') != case['item_id'] or receipt.get('ordinal') != ordinal
                or any(receipt.get(key) != value for key, value in binding.items())):
            raise ValueError('Prior case receipt identity mismatch')
        result = receipt.get('dev60_guard', {}).get('complete')
        if type(result) is not bool or (result and receipt.get('completion_unknown')):
            raise ValueError('Prior case cannot be classified')
        valid_count += int(result)
        receipts[name] = receipt_bytes
    if valid_count != completion.get('valid_case_count'):
        raise ValueError('Prior valid case count mismatch')
    return receipts, valid_count, untouched
