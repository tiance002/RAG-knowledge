"""Reviewed-known alias imports remain distinct from UNKNOWN relabeling."""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil

import pytest

from test_coverage_launch_preparation import import_control,read,review
from scripts import prepare_rsi_coverage_launch as builder
from rsik.events import canonical
from rsik.exposure import ExposureLedger
from rsik.launch import fixture_identity,bind_fixture_sources


def known_fixture(import_control):
    kwargs,historical,budget,mapping,aliases=import_control
    sid=next(iter(mapping));alias_id=mapping[sid]
    known=ExposureLedger(kwargs['output'].parent/'reviewed-known.sqlite')
    parent={'source_id':'canonical-parent','document_id':'canonical-doc','version':'pinned-CONTROL',
        'content_sha256':'0'*64,'source_family_id':'CANONICAL-CONTROL-FAMILY','origin':'INVENTED_CONTROL',
        'relation':'ORIGINAL','related_source_ids':[],'exposure_status':'EXPOSURE_UNKNOWN'}
    known.register(parent,review=review(parent))
    for source_id,source in historical.sources().items():
        record={k:v for k,v in source.items() if k!='review'}
        if source_id==sid:record.update(source_family_id=parent['source_family_id'],relation='DERIVED_FROM',related_source_ids=[parent['source_id']])
        known.register(record,review=review(record))
    for e in historical.log.read():
        if e['event_type']=='SOURCE_EXPOSED':
            p=e['payload'];known.expose(p['source_ids'],partition=p['partition'],experiment_id=p['experiment_id'],
                actor=p['actor'],evidence_ref='old:'+e['event_hash'],occurred_at=p['occurred_at'],event_key=e['event_hash'])
    specs=read(kwargs['fixture']/'relations.restricted.json')
    for spec in specs.values():
        if alias_id in spec['sources']:
            spec['sources'][alias_id].update(source_family_id=parent['source_family_id'],relation='DERIVED_FROM',related_source_ids=[parent['source_id']])
    (kwargs['fixture']/'relations.restricted.json').write_bytes(canonical(specs))
    manifest=read(kwargs['fixture']/'manifest.json');manifest['artifact_hashes']['relations.restricted.json']=hashlib.sha256((kwargs['fixture']/'relations.restricted.json').read_bytes()).hexdigest()
    (kwargs['fixture']/'manifest.json').write_bytes(canonical(manifest))
    return kwargs,historical,known,budget


def test_reviewed_known_with_parent_is_accepted_and_unknown_stays_unknown(import_control):
    kwargs,old,known,budget=known_fixture(import_control)
    original=Path(old.log.path).read_bytes();known_bytes=Path(known.log.path).read_bytes()
    kwargs['historical_ledger']=known.log.path
    extra=kwargs['output'].parent/'same-fixture';shutil.copytree(kwargs['fixture'],extra)
    kwargs['additional_fixtures']=[extra]
    builder.prepare(**kwargs)
    assert Path(old.log.path).read_bytes()==original
    assert Path(known.log.path).read_bytes()==known_bytes
    out=kwargs['output'];plan=read(out/'launch-plan.restricted.json');snapshot=read(out/'exposure-snapshot.restricted.json')
    assert plan['fixtures']=={str(p.resolve()):fixture_identity(p) for p in (kwargs['fixture'],extra)}
    bind_fixture_sources(plan,snapshot)
    assert all(s['exposure_status']=='EXPOSURE_UNKNOWN' for s in snapshot['sources'])
    ledger=ExposureLedger(out/'exposure-events.sqlite')
    assert ledger.sources()['canonical-parent']['relation']=='ORIGINAL'
    assert not snapshot['eligible']
    assert budget.snapshot()['attempts']==0


def test_existing_unknown_cannot_use_known_alias_even_with_parent_label(import_control):
    kwargs,old,known,budget=known_fixture(import_control)
    kwargs['historical_ledger']=old.log.path
    with pytest.raises(ValueError,match='UNKNOWN_HISTORY_MUST_NOT_BE_RELABELLED_KNOWN'):builder.prepare(**kwargs)


def test_multi_fixture_identity_conflict_is_rejected(import_control):
    kwargs,*_=import_control
    extra=kwargs['output'].parent/'different-fixture';shutil.copytree(kwargs['fixture'],extra)
    specs=read(extra/'relations.restricted.json')
    next(iter(next(iter(specs.values()))['sources'].values()))['source_family_id']='WRONG-FAMILY'
    (extra/'relations.restricted.json').write_bytes(canonical(specs))
    with pytest.raises(ValueError,match='MULTI_FIXTURE_SOURCE_IDENTITY_MISMATCH'):
        builder.prepare(**kwargs,additional_fixtures=[extra])
