"""Local public engineering restoration; never calls an external model."""
import json, pathlib, shutil, sys, subprocess, os, hashlib
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import rsi_candidates as c
from rsi_feedback import build_feedback

def save(p, v):
    p.write_text(json.dumps(v, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def prepare():
    out = ROOT/'var/repair-round'
    out.mkdir(parents=True, exist_ok=False)
    old = pathlib.Path(r'D:\RAG-R0\var\r1')
    reg = out/'registry'
    shutil.copytree(old/'candidate-registry', reg)
    run = old/'engineering-failure/run'
    parent = '0f178f4d77c2532e322f991ef5bd733ee8a371bb61dbd9d55de439d26358117e'
    for p in (reg/'objects').iterdir():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == p.name
    feedback = c.read_json(run/'feedback.json')
    assert feedback == build_feedback(run, feedback['run_id'])
    obs = next(o for o in feedback['observations'] if o['category']=='required_fact_loss_vs_parent')
    save(out/'repair-patch.json', {'old':'return selected[-1:]', 'new':'return selected'})
    notes = dict(observation='Archived B keeps numeric row but loses header and unit.',
                 hypothesis='Restoring the already verified selection result restores distinct header/unit while omitting only a verified non-seed duplicate.',
                 change='Remove last-member slice: return selected[-1:] -> return selected.',
                 impact='Engineering restoration only; same source/order/facts, duplicate copy still omitted.',
                 falsification='Any required fact absent, duplicate delivered, cross-location content removed, or source/order mismatch.')
    child = c.derive(reg, parent, run/'implementation/B.json', None, notes, run, obs['observation_id'], out/'repair-patch.json')
    fixture = old/'engineering-failure/bundle'
    manifest = c.materialize(reg, parent, child, out/'bundle', fixture/'cases.json', fixture/'evaluation.json')
    c.verify_arm_sources(c.read_json(manifest), manifest.parent)
    save(out/'identity.json', dict(parent=parent, child=child, source_run=feedback['run_id'], observation_id=obs['observation_id'], manifest=str(manifest), registry_objects=len(list((reg/'objects').iterdir()))))
    print(json.dumps(c.read_json(out/'identity.json')))

if __name__=='__main__': prepare()
