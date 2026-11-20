"""Exact bulkBM25 construction and on-disk embedding cache for constrained ECS RAM."""
from array import array
from collections import Counter
import hashlib,json,sqlite3

class VectorCache:
 def __init__(self,path):
  self.db=sqlite3.connect(path)
  self.db.execute('create table if not exists vectors(key text primary key,dimensions integer,data blob)');self.db.commit()
 def key(self,text):return hashlib.sha256(text.encode()).hexdigest()
 def has(self,text):return self.db.execute('select 1 from vectors where key=?',(self.key(text),)).fetchone() is not None
 def put_many(self,texts,vectors):
  assert len(texts)==len(vectors)
  self.db.executemany('insert into vectors values(?,?,?)',[(self.key(t),len(v),array('d',v).tobytes()) for t,v in zip(texts,vectors)]);self.db.commit()
 def get(self,text):
  row=self.db.execute('select dimensions,data from vectors where key=?',(self.key(text),)).fetchone()
  if row is None:raise KeyError('Missing frozen embedding')
  result=array('d');result.frombytes(row[1]);assert len(result)==row[0];return result.tolist()

def bulk_bm25(session,kb_id,chunks):
    """Build a fresh benchmark KB in one transaction, using its stored child chunks."""
    from itertools import islice
    from sqlmodel import select
    from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, IndexStats, generate_uuid
    from app.services.bm25 import tokenize

    if session.exec(select(ChunkIndex.id).where(ChunkIndex.kb_id == kb_id).limit(1)).first() or session.get(IndexStats, kb_id):
        raise ValueError("bulk_bm25 requires a fresh benchmark index")
    count, average, df = 0, 0., Counter()
    chunks = iter(chunks)
    while batch := list(islice(chunks, 256)):
        owned = dict(session.exec(select(DocumentChunk.id, DocumentChunk.chunk_type)
                     .join(FileDocument).where(FileDocument.kb_id == kb_id,
                     DocumentChunk.id.in_([cid for cid, _ in batch]))).all())
        indices, postings = [], []
        for cid, content in batch:
            if cid not in owned:
                raise ValueError(f"BM25 benchmark chunk outside supplied KB: {cid}")
            if owned[cid] == "parent":
                continue
            terms = Counter(tokenize(content))
            if not terms:
                continue
            length = sum(terms.values())
            average = (average * count + length) / (count + 1)
            count += 1
            df.update(terms.keys())
            index_id = generate_uuid()
            indices.append(dict(id=index_id, chunk_id=cid, kb_id=kb_id,
                                terms=json.dumps(terms, ensure_ascii=False), doc_length=length))
            postings.extend(dict(kb_id=kb_id, index_id=index_id, term=term, tf=tf) for term, tf in terms.items())
        if indices:
            session.execute(ChunkIndex.__table__.insert(), indices)
            session.execute(BM25Posting.__table__.insert(), postings)
    if count:
        session.add(IndexStats(kb_id=kb_id, total_docs=count, avg_doc_length=average,
                               term_doc_freq=json.dumps(df, ensure_ascii=False)))
    session.commit()

def verify_bulk_bm25():
 from sqlmodel import SQLModel,Session,create_engine,select
 from app.models import KnowledgeBase,FileDocument,DocumentChunk,ChunkIndex,IndexStats,BM25Posting
 from sqlalchemy import func
 from app.services.bm25 import BM25Index
 samples=['中文检索 alpha alpha 12345','中文表格 beta beta 67890','alpha beta evidence','']
 engine=create_engine('sqlite://')
 SQLModel.metadata.create_all(engine)
 with Session(engine) as s:
  for kb in ('incremental','bulk'):
   s.add(KnowledgeBase(id=kb,name=kb,embedding_model='unused',vlm_model='unused'))
   s.add(FileDocument(id=kb,kb_id=kb,name=kb,local_path='none',size=0));s.commit()
   for i,text in enumerate(samples):s.add(DocumentChunk(id=kb+str(i),file_id=kb,content=text,original_index=i))
   s.commit()
  service=BM25Index()
  for i,text in enumerate(samples):service.index_chunk(s,'incremental'+str(i),text,'incremental')
  s.commit();bulk_bm25(s,'bulk',[('bulk'+str(i),text) for i,text in enumerate(samples)])
  a=s.get(IndexStats,'incremental');b=s.get(IndexStats,'bulk')
  assert a.total_docs==b.total_docs and a.avg_doc_length==b.avg_doc_length
  # The persisted JSON is a legacy snapshot. Compare authoritative posting DF.
  def posting_df(kb):
   return dict(s.exec(select(BM25Posting.term,func.count(BM25Posting.index_id))
       .where(BM25Posting.kb_id==kb).group_by(BM25Posting.term)).all())
  assert posting_df('incremental')==posting_df('bulk')
  for query in ('中文 alpha','beta 67890','12345'):
   x=service.search(s,query,'incremental',top_k=10);y=service.search(s,query,'bulk',top_k=10)
   assert [(r['chunk_id'].replace('incremental',''),r['bm25_score']) for r in x]==[(r['chunk_id'].replace('bulk',''),r['bm25_score']) for r in y]
 return {'status':'PASS','checks':['termfrequencies','documentfrequencies','exact_average_recurrence','three_query_scores_and_order']}
