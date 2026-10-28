"""Assemble credential-free document profiles and image assets entirely on ECS."""
import hashlib,json,pathlib,sys

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 root=pathlib.Path(sys.argv[1]);r=root/'runtime200-r1'
 prep=json.loads((r/'preparation.json').read_bytes());assert prep['status']=='PARSED'
 assert json.loads((r/'text-preparation.json').read_bytes())['status']=='COMPLETE'
 if (r/'profiles.jsonl').exists():raise ValueError('No profile overwrite')
 sources=json.loads((r/'sources.json').read_bytes());assets={};count=0;pages=0
 old=json.loads(pathlib.Path('/opt/amrag/experiments/pdf-table-ablation-20261027-r1/input.json').read_bytes())
 captions={(c['source_sha256'],c['page_number']):c['content'] for c in old['arms']['before'] if c['content_type']=='image'}
 with (r/'profiles.jsonl').open('w') as f:
  for sha,source in sources.items():
   a=json.loads((r/'parsed'/sha/'before.json').read_bytes());b=json.loads((r/'parsed'/sha/'after.json').read_bytes())
   assert len(a['pages'])==len(b['pages']);pages+=len(a['pages'])
   for x,y in zip(a['pages'],b['pages']):assert x['page_text_sha256']==y['page_text_sha256']
   image_keys={}
   for arm,payload in [('before',a),('after',b)]:
    chunks=payload['chunks']
    for index,c in enumerate(chunks):
     c.update(source_id=sha,original_index=index)
     locator=(c.get('metadata') or {}).get('source_locator') or {}
     c['source_pages']=locator.get('pages') or [c['page_number']]
     if c['content_type']=='image':
      path=pathlib.Path(c['image_path'])
      if not path.is_absolute():path=r/'parsed'/sha/arm/'images'/path
      assert path.is_file(),path
      asset=digest(path);c['image_asset_sha256']=asset
      assets.setdefault(asset,{'path':str(path),'sha256':asset,'bytes':path.stat().st_size})
      if source['profile']=='pdf-regression':c['content']=captions[(sha,c['page_number'])];c['caption_frozen']=True
    image_keys[arm]=[(c['page_number'],c['image_asset_sha256']) for c in chunks if c['content_type']=='image']
    record={'profile':source['profile'],'arm':arm,'source_id':sha,'name':source['name'], 'chunks':chunks}
    f.write(json.dumps(record,ensure_ascii=False)+'\n');count+=len(chunks)
   assert image_keys['before']==image_keys['after']
  for line in (r/'text-profiles.jsonl').open():
   record=json.loads(line);record['arm']='shared'
   for i,c in enumerate(record['chunks']):c.update(source_id=record['source_id'],original_index=i,source_pages=[c['page_number']])
   f.write(json.dumps(record,ensure_ascii=False)+'\n');count+=len(record['chunks'])
 needed=set()
 for line in (r/'profiles.jsonl').open():
  for c in json.loads(line)['chunks']:
   if c['content_type']=='image' and not c.get('caption_frozen'):needed.add(c['image_asset_sha256'])
 (r/'image-assets.json').write_text(json.dumps({k:assets[k] for k in sorted(needed)},ensure_ascii=False,indent=2))
 summary={'status':'ASSEMBLED','PDFs':len(sources),'PDF_pages':pages,'base_page_OCR_equal':True,
          'profile_chunks':count,'new_unique_images':len(needed),'old_captions_reused':len(captions),
          'profiles_sha256':digest(r/'profiles.jsonl'),'image_manifest_sha256':digest(r/'image-assets.json'),
          'freeze_sha256':digest(root/'dataset200/freeze.json'),'control_sql_sha256':digest(r/'control-sql.jsonl')}
 (r/'assembled.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))

if __name__=='__main__':main()
