import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AppstoreOutlined, ArrowLeftOutlined, FileTextOutlined, PlusOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import './KnowledgeBases.css';

type KnowledgeBase = { id: string; name: string; description: string | null; embedding_model: string; vlm_model: string; chunk_count: number; updated_at: string };
type Model = { id: string; name: string; type: string; provider?: string };
type Document = { id: string; name: string; display_name?: string; original_name?: string; size: number; status: string; progress: number; chunk_count: number; error_message?: string | null };
type Chunk = { id: string; content: string; page_number?: number | null; content_type?: string };
type Recall = { results: { chunkId?: string; score: number; content: string; fileName: string; location: string }[]; query_time: number; retrieval_status?: string; expansion_delivery_status?: string };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || body?.message || `请求失败 (${response.status})`), { detail });
  }
  return body?.data as T;
}

const statusLabels: Record<string, string> = { pending: '待解析', parsing: '解析中', pending_confirm: '待确认', parsed: '待入库', embedding: '入库中', ready: '已入库', failed: '处理失败' };
const displayDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN'); };

export default function KnowledgeBases() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [selected, setSelected] = useState<KnowledgeBase | null>(null);
  const [files, setFiles] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uploadConflict, setUploadConflict] = useState<{ file: File; detail: { same_sha256: boolean; duplicate_file_id?: string; existing_files: { file_id: string; display_name: string; status: string }[] } } | null>(null);
  const [replaceTarget, setReplaceTarget] = useState('');
  const [uploadNotice, setUploadNotice] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [embedding, setEmbedding] = useState('');
  const [vlm, setVlm] = useState('');
  const [preferences, setPreferences] = useState({ defaultEmbeddingModel: '', defaultVlmModel: '' });
  const [recycle, setRecycle] = useState(false);
  const [deleted, setDeleted] = useState<KnowledgeBase[]>([]);
  const [preview, setPreview] = useState<Document | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [recallOpen, setRecallOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [searchMode, setSearchMode] = useState('hybrid');
  const [filter, setFilter] = useState('');
  const [sortBy, setSortBy] = useState('recent');
  const [recall, setRecall] = useState<Recall | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const [bases, available, saved] = await Promise.all([api<KnowledgeBase[]>('/knowledge-bases'), api<Model[]>('/models'), api<{ defaultEmbeddingModel: string; defaultVlmModel: string }>('/settings')]);
      setItems(bases || []); setModels(available || []); setPreferences(saved);
    } catch (err) { setError(err instanceof Error ? err.message : '加载失败'); }
    finally { setLoading(false); }
  }, []);
  const loadFiles = useCallback(async (id: string) => {
    try { setFiles(await api<Document[]>(`/knowledge-bases/${encodeURIComponent(id)}/files`) || []); }
    catch (err) { setError(err instanceof Error ? err.message : '加载文件失败'); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (!selected) return; void loadFiles(selected.id); const timer = window.setInterval(() => void loadFiles(selected.id), 5000); return () => window.clearInterval(timer); }, [selected, loadFiles]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault(); if (!name.trim() || !embedding) return;
    setBusy(true); setError('');
    try {
      await api('/knowledge-bases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), description: description.trim() || null, embedding_model: embedding, vlm_model: vlm }) });
      setCreating(false); setName(''); setDescription(''); setEmbedding(''); setVlm(''); await load();
    } catch (err) { setError(err instanceof Error ? err.message : '创建失败'); }
    finally { setBusy(false); }
  };
  const sendUpload = async (file: File, action = 'check', target?: string) => {
    if (!selected) return;
    const form = new FormData(); form.append('file', file); setBusy(true); setError(''); setUploadNotice('');
    try {
      const params = new URLSearchParams({ action }); if (target) params.set('target_file_id', target);
      const result = await api<{ duplicate: boolean; outcome: string; status: string }>(`/knowledge-bases/${encodeURIComponent(selected.id)}/upload?${params}`, { method: 'POST', body: form });
      setUploadConflict(null); setUploadNotice(result.duplicate ? `已复用已有文件，当前状态：${result.status}` : result.outcome === 'replacement_staged' ? '已创建替换版本；新版本成功入库后用于检索，历史数据保留。' : '上传成功，请解析文件。');
      await loadFiles(selected.id);
    } catch (err) {
      const detail = (err as Error & { detail?: { code: string; same_sha256: boolean; duplicate_file_id?: string; existing_files: { file_id: string; display_name: string; status: string }[] } }).detail;
      if (detail?.code === 'FILE_NAME_CONFLICT') { setUploadConflict({ file, detail }); setReplaceTarget(detail.existing_files[0]?.file_id || ''); }
      else setError(err instanceof Error ? err.message : '上传失败');
    } finally { setBusy(false); }
  };
  const upload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''; if (file) void sendUpload(file);
  };
  const action = async (file: Document, kind: 'parse' | 'vectorize') => {
    setBusy(true); setError('');
    try { await api(`/files/${encodeURIComponent(file.id)}/${kind}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: kind === 'parse' ? JSON.stringify({ auto_vectorize: false }) : undefined }); if (selected) await loadFiles(selected.id); }
    catch (err) { setError(err instanceof Error ? err.message : '操作失败'); }
    finally { setBusy(false); }
  };
  const modelName = (id: string) => models.find(m => m.id === id)?.name || id || '未配置';
  const visibleItems = items.filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(filter.trim().toLowerCase())).slice().sort((a, b) => sortBy === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const openCreate = () => { setError(''); setEmbedding(models.some(m => m.type === 'embedding' && m.id === preferences.defaultEmbeddingModel) ? preferences.defaultEmbeddingModel : ''); setVlm(models.some(m => m.type === 'vlm' && m.id === preferences.defaultVlmModel) ? preferences.defaultVlmModel : ''); setCreating(true); };
  const openRecycle = async () => { setBusy(true); setError(''); try { setDeleted(await api<KnowledgeBase[]>('/knowledge-bases/recycle-bin') || []); setRecycle(true); } catch (err) { setError(err instanceof Error ? err.message : '加载回收站失败'); } finally { setBusy(false); } };
  const removeBase = async (base: KnowledgeBase) => { if (!window.confirm(`将“${base.name}”移入回收站？`)) return; setBusy(true); setError(''); try { await api(`/knowledge-bases/${encodeURIComponent(base.id)}`, { method: 'DELETE' }); setSelected(null); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作失败'); } finally { setBusy(false); } };
  const recycleAction = async (base: KnowledgeBase, permanent: boolean) => { if (permanent && !window.confirm(`永久删除“${base.name}”及其文档？此操作无法撤销。`)) return; setBusy(true); setError(''); try { await api(`/knowledge-bases/${encodeURIComponent(base.id)}/${permanent ? 'permanent' : 'restore'}`, { method: permanent ? 'DELETE' : 'POST' }); setDeleted(await api<KnowledgeBase[]>('/knowledge-bases/recycle-bin') || []); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作失败'); } finally { setBusy(false); } };
  const openPreview = async (file: Document) => { setBusy(true); setError(''); try { setChunks(await api<Chunk[]>(`/files/${encodeURIComponent(file.id)}/chunks`) || []); setPreview(file); } catch (err) { setError(err instanceof Error ? err.message : '加载分块失败'); } finally { setBusy(false); } };
  const submitChunks = async () => { if (!preview || !selected) return; setBusy(true); setError(''); try { await api(`/files/${encodeURIComponent(preview.id)}/chunks/submit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chunks: chunks.map(({ id, content }) => ({ id, content })) }) }); setPreview(null); await loadFiles(selected.id); } catch (err) { setError(err instanceof Error ? err.message : '提交分块失败'); } finally { setBusy(false); } };
  const runRecall = async (event: React.FormEvent) => { event.preventDefault(); if (!selected || !query.trim()) return; setBusy(true); setError(''); setRecall(null); try { setRecall(await api<Recall>(`/knowledge-bases/${encodeURIComponent(selected.id)}/recall`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: query.trim(), search_mode: searchMode, top_k: 20, context_window: 0 }) })); } catch (err) { setError(err instanceof Error ? err.message : '召回失败'); } finally { setBusy(false); } };

  if (selected) return <section className="kb-page">
    <button className="kb-back" onClick={() => { setSelected(null); setError(''); void load(); }}><ArrowLeftOutlined /> 返回知识库</button>
    <div className="kb-detail-header"><div className="kb-tile-icon"><AppstoreOutlined /></div><div><h1>{selected.name}</h1><p>{selected.description || '暂无描述'}</p></div></div>
    <div className="kb-info"><span>Embedding：{modelName(selected.embedding_model)}</span><span>视觉模型：{modelName(selected.vlm_model)}</span><span>片段：{selected.chunk_count}</span></div>
    <div className="kb-section-head"><div><h2>文档</h2><p>上传资料后，依次解析并确认入库。</p></div><div className="kb-head-actions"><button className="kb-secondary" onClick={() => { setRecallOpen(!recallOpen); setRecall(null); }}>召回测试</button><button className="kb-secondary" onClick={() => void removeBase(selected)}>移入回收站</button><label className="kb-primary kb-upload"><UploadOutlined /> 上传文档<input type="file" disabled={busy} onChange={upload} accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.jpg,.jpeg,.png,.bmp,.webp" /></label></div></div>
    {error && <div className="kb-error" role="alert">{error}</div>}
    {uploadNotice && <p role="status">{uploadNotice}</p>}
    {uploadConflict && <div className="kb-modal-backdrop"><div className="kb-modal" role="dialog" aria-modal="true" aria-labelledby="upload-conflict-title"><h2 id="upload-conflict-title">已有同名文件</h2><p>{uploadConflict.detail.same_sha256 ? '内容相同，可复用已有文件，也可另建条目或创建替换版本。' : '内容不同，请另建条目或选择旧文件创建替换版本。'}新版本成功入库后用于检索，历史文件与索引保留。</p><label>替换目标<select value={replaceTarget} onChange={event => setReplaceTarget(event.target.value)}>{uploadConflict.detail.existing_files.map(row => <option key={row.file_id} value={row.file_id}>{row.display_name} · {row.status}</option>)}</select></label><div className="kb-modal-actions"><button className="kb-secondary" disabled={busy} onClick={() => setUploadConflict(null)}>取消</button>{uploadConflict.detail.same_sha256 && <button className="kb-secondary" disabled={busy} onClick={() => void sendUpload(uploadConflict.file, 'reuse', uploadConflict.detail.duplicate_file_id)}>复用已有文件</button>}<button className="kb-secondary" disabled={busy} onClick={() => void sendUpload(uploadConflict.file, 'new')}>另建条目</button><button className="kb-primary" disabled={busy || !replaceTarget} onClick={() => void sendUpload(uploadConflict.file, 'replace', replaceTarget)}>创建替换版本</button></div></div></div>}
    {recallOpen && <div className="kb-recall"><form onSubmit={runRecall}><input value={query} onChange={e => setQuery(e.target.value)} placeholder="输入问题或关键词，查看召回内容" required /><select value={searchMode} onChange={e => setSearchMode(e.target.value)}><option value="hybrid">混合检索</option><option value="vector">向量检索</option><option value="fulltext">全文检索</option></select><button className="kb-primary" disabled={busy}>开始测试</button></form>{recall && <div className="kb-recall-results"><p>状态：{recall.retrieval_status || '未提供'} · {recall.results.length} 条结果 · {recall.query_time.toFixed(0)} 毫秒</p>{recall.results.map((result, index) => <article key={result.chunkId || index}><div><strong>{result.fileName}</strong><span>{result.location} · 分数 {result.score.toFixed(3)}</span></div><p>{result.content}</p></article>)}</div>}</div>}
    <div className="kb-file-list">{files.length === 0 ? <div className="kb-empty"><FileTextOutlined /><strong>还没有文档</strong><span>上传文档，开始构建知识库</span></div> : files.map(file => <div className="kb-file-row" key={file.id}><div className="kb-file-icon"><FileTextOutlined /></div><div className="kb-file-name"><strong title={file.original_name || file.name}>{file.display_name || file.name}</strong><span>{(file.size / 1024).toFixed(1)} KB · {file.chunk_count} 个片段{file.error_message ? ` · ${file.error_message}` : ''}</span></div><span className={`kb-status ${file.status}`}>{statusLabels[file.status] || file.status}{['parsing', 'embedding'].includes(file.status) ? ` ${file.progress}%` : ''}</span>{file.chunk_count > 0 && <button className="kb-text-button" disabled={busy} onClick={() => void openPreview(file)}>查看分块</button>}{['pending', 'failed'].includes(file.status) && <button className="kb-text-button" disabled={busy} onClick={() => void action(file, 'parse')}>解析</button>}{['parsed', 'pending_confirm'].includes(file.status) && <button className="kb-text-button" disabled={busy} onClick={() => void action(file, 'vectorize')}>确认入库</button>}</div>)}</div>
    {preview && <div className="kb-modal-backdrop"><div className="kb-modal kb-preview" role="dialog" aria-modal="true" aria-labelledby="kb-preview-title"><div className="kb-modal-head"><div><h2 id="kb-preview-title">文档分块 · {preview.name}</h2><p>{preview.status === 'pending_confirm' ? '可调整内容，然后提交入库。' : '已处理文档的分块以只读方式展示。'}</p></div><button aria-label="关闭" onClick={() => setPreview(null)}>×</button></div>{error && <div className="kb-error" role="alert">{error}</div>}<div className="kb-chunks">{chunks.length === 0 ? <p>暂无分块</p> : chunks.map((chunk, index) => <label key={chunk.id}><span>片段 {index + 1}{chunk.page_number != null ? ` · 第 ${chunk.page_number} 页` : ''}</span><textarea readOnly={preview.status !== 'pending_confirm'} value={chunk.content} onChange={e => setChunks(old => old.map(item => item.id === chunk.id ? { ...item, content: e.target.value } : item))} rows={4} /></label>)}</div><div className="kb-modal-actions"><button className="kb-secondary" onClick={() => setPreview(null)}>关闭</button>{preview.status === 'pending_confirm' && <button className="kb-primary" disabled={busy || chunks.length === 0 || chunks.some(c => !c.content.trim())} onClick={() => void submitChunks()}>提交并入库</button>}</div></div></div>}
  </section>;

  return <section className="kb-page">
    <div className="kb-page-head"><div><h1>知识库</h1><p>集中管理你的资料，让知识随时可用。</p></div><div className="kb-head-actions"><select className="kb-sort" aria-label="排序方式" value={sortBy} onChange={e => setSortBy(e.target.value)}><option value="recent">最近更新</option><option value="name">名称排序</option></select><button className="kb-secondary" onClick={() => void openRecycle()}>回收站</button><button className="kb-secondary" onClick={() => void load()} aria-label="刷新知识库"><ReloadOutlined /></button><button className="kb-primary" onClick={openCreate}><PlusOutlined /> 新建知识库</button></div></div>
    {error && <div className="kb-error" role="alert">{error}</div>}
    <div className="kb-toolbar"><strong>全部知识库 <span>{items.length}</span></strong><input aria-label="搜索知识库" placeholder="搜索知识库" value={filter} onChange={e => setFilter(e.target.value)} /></div>
    {loading ? <div className="kb-empty">加载中…</div> : items.length === 0 ? <div className="kb-empty kb-empty-panel"><div className="kb-empty-icon"><AppstoreOutlined /></div><strong>创建你的第一个知识库</strong><span>上传文档，让知识有序沉淀。</span><button className="kb-primary" onClick={openCreate}><PlusOutlined /> 新建知识库</button></div> : <div className="kb-grid">{visibleItems.map(kb => <button className="kb-card" key={kb.id} onClick={() => { setError(''); setFiles([]); setSelected(kb); }}><div className="kb-card-top"><div className="kb-tile-icon"><AppstoreOutlined /></div><span className="kb-card-arrow">↗</span></div><h2>{kb.name}</h2><p>{kb.description || '暂无描述'}</p><div className="kb-card-foot"><span>{kb.chunk_count} 个片段</span><span>更新于 {displayDate(kb.updated_at)}</span></div></button>)}</div>}
    {!loading && items.length > 0 && visibleItems.length === 0 && <div className="kb-empty">没有找到匹配的知识库</div>}
    {recycle && <div className="kb-modal-backdrop"><div className="kb-modal" role="dialog" aria-modal="true" aria-labelledby="kb-recycle-title"><div className="kb-modal-head"><div><h2 id="kb-recycle-title">回收站</h2><p>从回收站恢复知识库。</p></div><button aria-label="关闭" onClick={() => setRecycle(false)}>×</button></div>{error && <div className="kb-error" role="alert">{error}</div>}{deleted.length === 0 ? <p className="kb-model-hint">回收站为空</p> : deleted.map(base => <div className="kb-recycle-row" key={base.id}><strong>{base.name}</strong><button className="kb-text-button" disabled={busy} onClick={() => void recycleAction(base, false)}>恢复</button></div>)}</div></div>}
    {creating && <div className="kb-modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget && !busy) setCreating(false); }}><div className="kb-modal" role="dialog" aria-modal="true" aria-labelledby="kb-modal-title"><div className="kb-modal-head"><div><h2 id="kb-modal-title">新建知识库</h2><p>选择已配置的模型，为文档建立检索空间。</p></div><button aria-label="关闭" onClick={() => setCreating(false)}>×</button></div><form onSubmit={create}><label>知识库名称 <em>*</em><input required maxLength={100} value={name} onChange={e => setName(e.target.value)} placeholder="例如：产品文档" /></label><label>简介<textarea value={description} onChange={e => setDescription(e.target.value)} placeholder="描述知识库的用途（选填）" rows={3} /></label><label>Embedding 模型 <em>*</em><select required value={embedding} onChange={e => setEmbedding(e.target.value)}><option value="">请选择模型</option>{models.filter(m => m.type.toLowerCase() === 'embedding').map(m => <option value={m.id} key={m.id}>{m.name}</option>)}</select></label><label>视觉模型 <small>（可选）</small><select value={vlm} onChange={e => setVlm(e.target.value)}><option value="">暂不配置</option>{models.filter(m => m.type.toLowerCase() === 'vlm').map(m => <option value={m.id} key={m.id}>{m.name}</option>)}</select></label><p className="kb-model-hint">没有可用模型？前往 <Link to="/settings" onClick={() => setCreating(false)}>模型与设置</Link> 添加。</p>{error && <div className="kb-error" role="alert">{error}</div>}<div className="kb-modal-actions"><button type="button" className="kb-secondary" onClick={() => setCreating(false)}>取消</button><button className="kb-primary" type="submit" disabled={busy || !embedding || !name.trim()}>{busy ? '创建中…' : '创建知识库'}</button></div></form></div></div>}
  </section>;
}

