"""Create a fresh journal for GLM caption continuation; never mutate failed runs."""
import json,pathlib,shutil,sys
base=pathlib.Path(sys.argv[1]).resolve();policy_path=base/'model-policy.json';policy=json.loads(policy_path.read_bytes())
assert policy['vlm_model']=='zai-org/GLM-5.3-Flash' and policy['runtime_name']=='runtime200-r3'
prior=base/policy['runtime_name'];assert json.loads((prior/'completion.json').read_bytes())['exit_code']==1
runtime=base/'runtime200-r4';assert not runtime.exists();runtime.mkdir(mode=0o700)
for name in ('assembled.json','profiles.jsonl','image-assets.json','control-sql.jsonl','sources.json','text-sources'):
 p=prior/name;assert p.exists();(runtime/name).symlink_to(p,target_is_directory=p.is_dir())
shutil.copytree(prior/'chroma',runtime/'chroma')
captions=base/'runtime200-r2/caption-results.jsonl';assert captions.is_file()
(runtime/'prior-caption-results.jsonl').symlink_to(captions)
policy['runtime_name']='runtime200-r4'
policy['caption_policy']='reuse verified nonempty successes; retry failed/unknown/empty items until success with capped exponential backoff'
policy_path.write_text(json.dumps(policy,indent=2))
print(json.dumps({'runtime':str(runtime),'prior_caption_cache':str(captions),'prior_caption_cache_bytes':captions.stat().st_size,'provider_calls':0}))
