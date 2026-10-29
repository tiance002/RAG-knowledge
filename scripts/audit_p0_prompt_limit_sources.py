import pathlib,re
b=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027/runtime200-r4/contexts')
for i in (137,146,149,153):
 c=(b/f'{i}-before.txt').read_text();print('\nCASE',i)
 chunks=re.split(r'(?=\n\[\d+\] 来源:)',c)
 for chunk in chunks:
  h=chunk[:140].replace('\n',' ');terms=['14,009.54','13,898.15','GS5002','GS5101','GS6001','GS6883A','has-exch','housing','M250']
  hits=[x for x in terms if x.lower() in chunk.lower()];print(h,'hits',','.join(hits))
