"""Persist the owner's project-wide VLM replacement; never print or export secrets."""
import argparse,json,os,pathlib,re,shutil,sqlite3,time

parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
base=pathlib.Path(args.root).resolve();db=pathlib.Path('/opt/amrag/backend/local.db')
target='zai-org/GLM-5.3-Flash';old='Qwen/Qwen2.5-VL-72B-Instruct'
ids=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes());vlmid=ids['models']['vlm']
connection=sqlite3.connect(db);connection.row_factory=sqlite3.Row
row=dict(connection.execute('select * from custom_models where id=?',(vlmid,)).fetchone())
assert row['model_type']=='VLM' and row['model_name']==old
snapshot=base/'model-migration-backup'/str(time.time_ns());snapshot.mkdir(mode=0o700,parents=True)
backup_path=snapshot/'local-before-vlm.db'
descriptor=os.open(backup_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(descriptor)
with sqlite3.connect(backup_path) as backup:connection.backup(backup)
other_before=[tuple(r) for r in connection.execute('select * from custom_models where id<>? order by id',(vlmid,))]
counts_before=[connection.execute('select count(*) from '+table).fetchone()[0] for table in ('knowledge_bases','file_documents','document_chunks')]
with connection:
 result=connection.execute('update custom_models set name=?,model_name=? where id=? and model_name=?',
    ('GLM-5.3-Flash',target,vlmid,old));assert result.rowcount==1
assert other_before==[tuple(r) for r in connection.execute('select * from custom_models where id<>? order by id',(vlmid,))]
assert counts_before==[connection.execute('select count(*) from '+table).fetchone()[0] for table in ('knowledge_bases','file_documents','document_chunks')]
assert connection.execute('select count(*) from custom_models where model_name=?',(old,)).fetchone()[0]==0
refs=connection.execute('select count(*) from knowledge_bases where vlm_model=?',(vlmid,)).fetchone()[0]
connection.close()
# Settings fallback is durable too. Endpoint/key remain solely on ECS, reused from the same VLM record.
env=pathlib.Path('/opt/amrag/backend/.env')
if env.exists():
 shutil.copyfile(env,snapshot/'backend.env.before');os.chmod(snapshot/'backend.env.before',0o600)
 text=env.read_text()
else:text=''
for key,value in {'VLM_MODEL':target,'VLM_BASE_URL':row['base_url'],'VLM_API_KEY':row['api_key']}.items():
 assert '\n' not in value and '\r' not in value
 line=key+'='+json.dumps(value)
 text=re.sub(r'^'+key+r'=.*$',lambda _:line,text,flags=re.M) if re.search(r'^'+key+'=',text,re.M) else text.rstrip()+'\n'+line+'\n'
temp=env.with_suffix('.env.vlm-tmp');temp.write_text(text);os.chmod(temp,0o600);temp.replace(env)
policy={'schema':'project-vlm-policy/v1','vlm_model':target,'vlm_model_id':vlmid,'runtime_name':'runtime200-r2',
 'scope':'all future project image-description calls; shared before/after captions',
 'owner_instruction':'Replace Qwen VLM persistently, not for one invocation',
 'historical_outputs':'preserve original model attribution; do not overwrite',
 'other_models':'unchanged','automatic_fallback':False}
(base/'model-policy.json').write_text(json.dumps(policy,indent=2))
report={'status':'PERSISTED','old_model':old,'new_model':target,'existing_kb_references':refs,
 'counts_unchanged':counts_before,'other_models_unchanged':True,'secrets_exported':False,'at':time.time(),
 'service_restart':False,'parser_service_switch':False}
(base/'model-migration.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
# Fresh request journal, reusing only completed no-inference preparation artifacts.
prior=base/'runtime200-r1';runtime=base/policy['runtime_name'];runtime.mkdir(mode=0o700)
for name in ('assembled.json','profiles.jsonl','image-assets.json','control-sql.jsonl','sources.json','text-sources'):
 assert (prior/name).exists();(runtime/name).symlink_to(prior/name,target_is_directory=(prior/name).is_dir())
shutil.copytree(prior/'chroma',runtime/'chroma')
print(json.dumps({'fresh_runtime':str(runtime),'prior_journal':'preserved','provider_calls':0}))
