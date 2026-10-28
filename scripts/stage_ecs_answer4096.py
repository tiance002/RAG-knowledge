"""Stage the 4096 answer limit against the real ECS source, without switching service."""
import hashlib,json,pathlib,shutil,sys
base=pathlib.Path(sys.argv[1]);prod=pathlib.Path('/opt/amrag/backend/app');stage=base/'answer-cap4096-v1'
if stage.exists():raise RuntimeError('No overwrite of existing stage')
expected=json.loads((base/'identity-before.json').read_bytes())['source_files']
for relative in ('backend/app/services/llm.py','backend/app/core/config.py'):
 path=pathlib.Path('/opt/amrag')/relative
 assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[relative]
shutil.copytree(prod,stage/'backend/app',ignore=shutil.ignore_patterns('__pycache__'))
(stage/'backend/app/__init__.py').write_text('');(stage/'backend/app/services/__init__.py').write_text('')
config=stage/'backend/app/core/config.py';text=config.read_text()
before='    LLM_MODEL: str = "qwen2.5:7b"\n';assert text.count(before)==1
config.write_text(text.replace(before,before+'    LLM_ANSWER_MAX_TOKENS: int = 4096\n'))
llm=stage/'backend/app/services/llm.py';text=llm.read_text()
changes=[('                stream=True,\n                temperature=temperature,\n',
          '                stream=True,\n                temperature=temperature,\n                max_tokens=settings.LLM_ANSWER_MAX_TOKENS,\n'),
         ('                max_tokens=2000,\n','                max_tokens=settings.LLM_ANSWER_MAX_TOKENS,\n'),
         ('            max_tokens=max_tokens\n','            max_tokens=settings.LLM_ANSWER_MAX_TOKENS if max_tokens is None else max_tokens\n')]
for old,new in changes:
 assert text.count(old)==1,old;text=text.replace(old,new,1)
llm.write_text(text)
manifest={'status':'STAGED','production_changed':False,'default_answer_max_tokens':4096,
 'baseline_llm_sha256':expected['backend/app/services/llm.py'],'baseline_config_sha256':expected['backend/app/core/config.py'],
 'stage_llm_sha256':hashlib.sha256(llm.read_bytes()).hexdigest(),'stage_config_sha256':hashlib.sha256(config.read_bytes()).hexdigest()}
(stage/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest))
