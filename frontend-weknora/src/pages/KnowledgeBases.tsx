import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AppstoreOutlined, ArrowLeftOutlined, FileTextOutlined, PlusOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import TaskMonitor from '../../../frontend/src/file-tasks/TaskMonitor';
import { submitOnce } from '../../../frontend/src/file-tasks/requestKey';
import './KnowledgeBases.css';

type KnowledgeBase = { id: string; name: string; description: string | null; embedding_model: string; vlm_model: string; chunk_count: number; updated_at: string };
type Model = { id: string; name: string; type: string; provider?: string };
type Document = { id: string; name: string; display_name?: string; original_name?: string; size: number; status: string; progress: number; chunk_count: number; error_message?: string | null };
type Chunk = { id: string; content: string; page_number?: number | null; content_type?: string; chunk_type?: string; parent_id?: string | null };
type UploadResult = { file_id: string; status: string; outcome?: string; duplicate?: boolean; display_name?: string | null };
type UploadConflict = { code: 'FILE_NAME_CONFLICT'; original_name: string; same_sha256: boolean; duplicate_file_id?: string | null; existing_files: { file_id: string; display_name?: string | null; status: string }[]; actions: string[] };
type PendingUpload = { file: File; kbId: string; conflict: UploadConflict; targetId: string };
type ParseOptions = { xlsx_first_row_as_header: boolean; parent_child: boolean; parent_chunk_size: number; parent_chunk_overlap: number; child_chunk_size: number; child_chunk_overlap: number };
type Recall = {
  results: {
    chunkId?: string; context_group_id?: string; seed_child_ids?: string[];
    representative_child_id?: string; score_child_id?: string;
    citation_sources?: { id: string }[]; score: number; content: string; fileName: string; location: string;
  }[];
  query_time: number; retrieval_status?: string; expansion_delivery_status?: string;
  evaluation?: { mapping_status?: string; matched_seed_count: number; context_group_count: number };
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || body?.message || `请求失败 (${response.status})`), { detail, status: response.status });
  }
  return body?.data as T;
}

const statusLabels: Record<string, string> = { pending: '待解析', parsing: '解析中', pending_confirm: '待确认', parsed: '待入库', embedding: '入库中', ready: '已入库', failed: '处理失败' };
const displayDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN'); };
const defaultParseOptions: ParseOptions = { xlsx_first_row_as_header: false, parent_child: true, parent_chunk_size: 4096, parent_chunk_overlap: 0, child_chunk_size: 384, child_chunk_overlap: 48 };

export default function KnowledgeBases() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [selected, setSelected] = useState<KnowledgeBase | null>(null);
  const [files, setFiles] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const uploadPending = useRef(false);
  const [uploadNotice, setUploadNotice] = useState('');
  const [pendingUpload, setPendingUpload] = useState<PendingUpload | null>(null);
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
  const [parseTarget, setParseTarget] = useState<Document | null>(null);
  const [parseOptions, setParseOptions] = useState<ParseOptions>(defaultParseOptions);
  const [tasksOpen, setTasksOpen] = useState(false);
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
  const sendUpload = async (file: File, choice?: 'reuse' | 'new' | 'replace', targetId?: string) => {
    if (!selected || uploadPending.current) return;
    const kbId = selected.id;
    uploadPending.current = true;
    const form = new FormData(); form.append('file', file); setBusy(true); setError(''); setUploadNotice('');
    const params = new URLSearchParams();
    if (choice) params.set('action', choice);
    if (targetId) params.set('target_file_id', targetId);
    try {
      const suffix = params.size ? `?${params}` : '';
      const result = await api<UploadResult>(`/knowledge-bases/${encodeURIComponent(kbId)}/upload${suffix}`, { method: 'POST', body: form });
      if (!result?.file_id) throw new Error('上传结果缺少文件 ID，请刷新文件列表确认。');
      setPendingUpload(null);
      if (result.duplicate || result.outcome === 'duplicate') {
        setUploadNotice(`已复用已有文档${result.display_name ? `“${result.display_name}”` : ''}（${statusLabels[result.status] || result.status}），未创建新文件。`);
      } else if (result.outcome === 'replacement_staged') {
        setUploadNotice('已创建待解析的新版本；原文档仍保留，后续请解析并确认入库。');
      } else {
        setUploadNotice('上传成功，请解析文件。');
      }
      await loadFiles(kbId);
    } catch (err) {
      const failure = err as { status?: number; detail?: Partial<UploadConflict> } | null;
      const detail = failure?.detail;
      if (failure?.status === 409 && detail?.code === 'FILE_NAME_CONFLICT' && Array.isArray(detail.existing_files) && Array.isArray(detail.actions)) {
        const conflict = detail as UploadConflict;
        setPendingUpload({ file, kbId, conflict, targetId: conflict.duplicate_file_id || conflict.existing_files[0]?.file_id || '' });
      } else if (failure?.status === undefined || failure.status >= 500) {
        setPendingUpload(null);
        setError('上传结果未确认，请刷新文件列表后再决定是否重新上传。');
      } else {
        setError(err instanceof Error ? err.message : '上传失败');
      }
    } finally { uploadPending.current = false; setBusy(false); }
  };
  const upload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''; if (file) void sendUpload(file);
  };
  const action = async (file: Document, kind: 'parse' | 'vectorize') => {
    if (kind === 'parse') { setParseOptions(defaultParseOptions); setParseTarget(file); setError(''); return; }
    setBusy(true); setError('');
    try { await submitOnce(`${kind}/${file.id}`, null, key =>
      api(`/files/${encodeURIComponent(file.id)}/${kind}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key } })); if (selected) await loadFiles(selected.id); }
    catch (err) { setError(err instanceof Error ? err.message : '操作失败'); }
    finally { setBusy(false); }
  };
  const confirmParse = async () => {
    if (!parseTarget) return;
    if (parseOptions.parent_child && (parseOptions.parent_chunk_size < 1 || parseOptions.parent_chunk_size > 65536 || parseOptions.parent_chunk_overlap < 0 || parseOptions.parent_chunk_overlap >= parseOptions.parent_chunk_size || parseOptions.child_chunk_size < 1 || parseOptions.child_chunk_size > 65536 || parseOptions.child_chunk_overlap < 0 || parseOptions.child_chunk_overlap >= parseOptions.child_chunk_size)) {
      setError('父子块大小和重叠值无效，请检查字符数范围。');
      return;
    }
    setBusy(true); setError('');
    const config = { auto_vectorize: false, ...parseOptions };
    try {
      await submitOnce(`parse/${parseTarget.id}`, config, key => api(`/files/${encodeURIComponent(parseTarget.id)}/parse`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(config) }));
      setParseTarget(null);
      if (selected) await loadFiles(selected.id);
    } catch (err) { setError(err instanceof Error ? err.message : '解析失败'); }
    finally { setBusy(false); }
  };
  const modelName = (id: string) => models.find(m => m.id === id)?.name || id || '未配置';
  const visibleItems = items.filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(filter.trim().toLowerCase())).slice().sort((a, b) => sortBy === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const openCreate = () => { setError(''); setEmbedding(models.some(m => m.type === 'embedding' && m.id === preferences.defaultEmbeddingModel) ? preferences.defaultEmbeddingModel : ''); setVlm(models.some(m => m.type === 'vlm' && m.id === preferences.defaultVlmModel) ? preferences.defaultVlmModel : ''); setCreating(true); };
  const openRecycle = async () => { setBusy(true); setError(''); try { setDeleted(await api<KnowledgeBase[]>('/knowledge-bases/recycle-bin') || []); setRecycle(true); } catch (err) { setError(err instanceof Error ? err.message : '加载回收站失败'); } finally { setBusy(false); } };
  const removeBase = async (base: KnowledgeBase) => { if (!window.confirm(`将“${base.name}”移入回收站？`)) return; setBusy(true); setError(''); try { await api(`/knowledge-bases/${encodeURIComponent(base.id)}`, { method: 'DELETE' }); setSelected(null); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作失败'); } finally { setBusy(false); } };
  const recycleAction = async (base: KnowledgeBase, permanent: boolean) => { if (permanent && !window.confirm(`永久删除“${base.name}”及其文档？此操作无法撤销。`)) return; setBusy(true); setError(''); try { await api(`/knowledge-bases/${encodeURIComponent(base.id)}/${permanent ? 'permanent' : 'restore'}`, { method: permanent ? 'DELETE' : 'POST' }); setDeleted(await api<KnowledgeBase[]>('/knowledge-bases/recycle-bin') || []); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作失败'); } finally { setBusy(false); } };
  const openPreview = async (file: Document) => { setBusy(true); setError(''); try { setChunks(await api<Chunk[]>(`/files/${encodeURIComponent(file.id)}/chunks`) || []); setPreview(file); } catch (err) { setError(err instanceof Error ? err.message : '加载分块失败'); } finally { setBusy(false); } };
  const submitChunks = async () => { if (!preview || !selected) return; setBusy(true); setError(''); try { const data = { chunks: chunks.map(({ id, content }) => ({ id, content })) };
    await submitOnce(`submit/${preview.id}`, data, key => api(`/files/${encodeURIComponent(preview.id)}/chunks/submit`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(data) })); setPreview(null); await loadFiles(selected.id); } catch (err) { setError(err instanceof Error ? err.message : '提交分块失败'); } finally { setBusy(false); } };
  const runRecall = async (event: React.FormEvent) => { event.preventDefault(); if (!selected || !query.trim()) return; setBusy(true); setError(''); setRecall(null); try { setRecall(await api<Recall>(`/knowledge-bases/${encodeURIComponent(selected.id)}/recall`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: query.trim(), search_mode: searchMode, top_k: 20, context_window: 0 }) })); } catch (err) { setError(err instanceof Error ? err.message : '召回失败'); } finally { setBusy(false); } };

  if (selected) return <section className="kb-page">
    <button className="kb-back" onClick={() => { setSelected(null); setPendingUpload(null); setError(''); void load(); }}><ArrowLeftOutlined /> 返回知识库</button>
    <div className="kb-detail-header"><div className="kb-tile-icon"><AppstoreOutlined /></div><div><h1>{selected.name}</h1><p>{selected.description || '暂无描述'}</p></div></div>
    <div className="kb-info"><span>Embedding：{modelName(selected.embedding_model)}</span><span>视觉模型：{modelName(selected.vlm_model)}</span><span>片段：{selected.chunk_count}</span></div>
    <div className="kb-section-head"><div><h2>文档</h2><p>上传资料后，依次解析并确认入库。</p></div><div className="kb-head-actions"><button className="kb-secondary" aria-expanded={tasksOpen} onClick={() => setTasksOpen(!tasksOpen)}>处理任务</button><button className="kb-secondary" onClick={() => { setRecallOpen(!recallOpen); setRecall(null); }}>召回测试</button><button className="kb-secondary" onClick={() => void removeBase(selected)}>移入回收站</button><label className="kb-primary kb-upload"><UploadOutlined /> 上传文档<input type="file" disabled={busy} onChange={upload} accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.jpg,.jpeg,.png,.bmp,.webp" /></label></div></div>
    {error && <div className="kb-error" role="alert">{error}</div>}
    {uploadNotice && <p role="status">{uploadNotice}</p>}
    {pendingUpload && pendingUpload.kbId === selected.id && <div className="kb-modal-backdrop"><div className="kb-modal" role="dialog" aria-modal="true" aria-labelledby="kb-upload-conflict-title"><div className="kb-modal-head"><div><h2 id="kb-upload-conflict-title">已有同名文档</h2><p>“{pendingUpload.conflict.original_name}”已存在。请选择如何处理本次上传。</p></div><button aria-label="关闭" disabled={busy} onClick={() => setPendingUpload(null)}>×</button></div>
      {error && <div className="kb-error" role="alert">{error}</div>}
      <p className="kb-model-hint">{pendingUpload.conflict.same_sha256 ? '检测到内容相同的文档。' : '内容与现有同名文档不同。新版本需要单独解析并确认入库，原文档会保留。'}</p>
      <label>选择已有文档<select aria-label="选择已有文档" value={pendingUpload.targetId} onChange={e => setPendingUpload(old => old ? { ...old, targetId: e.target.value } : old)}>{pendingUpload.conflict.existing_files.map(item => <option key={item.file_id} value={item.file_id}>{item.display_name || pendingUpload.conflict.original_name} · {statusLabels[item.status] || item.status}</option>)}</select></label>
      <div className="kb-modal-actions"><button className="kb-secondary" disabled={busy} onClick={() => setPendingUpload(null)}>取消</button>{pendingUpload.conflict.actions.includes('reuse') && <button className="kb-secondary" disabled={busy || !pendingUpload.conflict.duplicate_file_id} onClick={() => void sendUpload(pendingUpload.file, 'reuse', pendingUpload.conflict.duplicate_file_id || undefined)}>复用相同文档</button>}{pendingUpload.conflict.actions.includes('new') && <button className="kb-secondary" disabled={busy} onClick={() => void sendUpload(pendingUpload.file, 'new')}>另建文档</button>}{pendingUpload.conflict.actions.includes('replace') && <button className="kb-primary" disabled={busy || !pendingUpload.targetId} onClick={() => void sendUpload(pendingUpload.file, 'replace', pendingUpload.targetId)}>创建新版本</button>}</div></div></div>}
    {tasksOpen && <TaskMonitor key={selected.id} kbId={selected.id} />}
    {recallOpen && <div className="kb-recall"><form onSubmit={runRecall}><input value={query} onChange={e => setQuery(e.target.value)} placeholder="输入问题或关键词，查看召回内容" required /><select value={searchMode} onChange={e => setSearchMode(e.target.value)}><option value="hybrid">混合检索</option><option value="vector">向量检索</option><option value="fulltext">全文检索</option></select><button className="kb-primary" disabled={busy}>开始测试</button></form>{recall && <div className="kb-recall-results"><p>状态：{recall.retrieval_status || '未提供'} · {recall.evaluation?.mapping_status === 'VERIFIED' ? `${recall.evaluation.matched_seed_count} 个命中子块 · ${recall.evaluation.context_group_count} 个上下文组` : `${recall.results.length} 条结果 · 映射待核对`} · {recall.query_time.toFixed(0)} 毫秒</p>{recall.results.map((result, index) => <article key={result.context_group_id || result.chunkId || index}><div><strong>{result.fileName}</strong><span>{result.location} · 分数 {result.score.toFixed(3)}</span></div><p>{result.seed_child_ids?.length ? `${result.seed_child_ids.length} 个命中子块` : '命中子块待核对'} · {result.citation_sources ? `${result.citation_sources.length} 个引用来源` : '引用来源待核对'}</p><p>{result.content}</p></article>)}</div>}</div>}
    <div className="kb-file-list">{files.length === 0 ? <div className="kb-empty"><FileTextOutlined /><strong>还没有文档</strong><span>上传文档，开始构建知识库</span></div> : files.map(file => <div className="kb-file-row" key={file.id}><div className="kb-file-icon"><FileTextOutlined /></div><div className="kb-file-name"><strong title={file.original_name || file.name}>{file.display_name || file.name}</strong><span>{(file.size / 1024).toFixed(1)} KB · {file.chunk_count} 个片段{file.error_message ? ` · ${file.error_message}` : ''}</span></div><span className={`kb-status ${file.status}`}>{statusLabels[file.status] || file.status}{['parsing', 'embedding'].includes(file.status) ? ` ${file.progress}%` : ''}</span>{file.chunk_count > 0 && <button className="kb-text-button" disabled={busy} onClick={() => void openPreview(file)}>查看分块</button>}{['pending', 'failed'].includes(file.status) && <button className="kb-text-button" disabled={busy} onClick={() => void action(file, 'parse')}>解析</button>}{['parsed', 'pending_confirm'].includes(file.status) && <button className="kb-text-button" disabled={busy} onClick={() => void action(file, 'vectorize')}>确认入库</button>}</div>)}</div>
    {preview && <div className="kb-modal-backdrop"><div className="kb-modal kb-preview" role="dialog" aria-modal="true" aria-labelledby="kb-preview-title"><div className="kb-modal-head"><div><h2 id="kb-preview-title">文档分块 · {preview.name}</h2><p>{preview.status === 'pending_confirm' ? '可调整内容，然后提交入库。' : '已处理文档的分块以只读方式展示。父子结构显示数据库当前保存的关联。'}</p></div><button aria-label="关闭" onClick={() => setPreview(null)}>×</button></div>{error && <div className="kb-error" role="alert">{error}</div>}<div className="kb-chunks">{chunks.length === 0 ? <p>暂无分块</p> : chunks.map((chunk, index) => { const parent = chunk.parent_id ? chunks.find(item => item.id === chunk.parent_id) : undefined; const kind = chunk.chunk_type === 'parent' ? '父块（上下文）' : chunk.chunk_type === 'child' ? '子块（参与检索）' : '普通块'; return <label key={chunk.id}><span>{kind} · 片段 {index + 1}{chunk.page_number != null ? ` · 第 ${chunk.page_number} 页` : ''}{chunk.parent_id ? ` · 关联父块：${parent ? parent.content.slice(0, 48) : chunk.parent_id}` : ''}</span><textarea readOnly={preview.status !== 'pending_confirm'} value={chunk.content} onChange={e => setChunks(old => old.map(item => item.id === chunk.id ? { ...item, content: e.target.value } : item))} rows={4} /></label>; })}</div><div className="kb-modal-actions"><button className="kb-secondary" onClick={() => setPreview(null)}>关闭</button>{preview.status === 'pending_confirm' && <button className="kb-primary" disabled={busy || chunks.length === 0 || chunks.some(c => !c.content.trim())} onClick={() => void submitChunks()}>提交并入库</button>}</div></div></div>}
    {parseTarget && <div className="kb-modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget && !busy) setParseTarget(null); }}><div className="kb-modal" role="dialog" aria-modal="true" aria-labelledby="kb-parse-title"><div className="kb-modal-head"><div><h2 id="kb-parse-title">解析设置 · {parseTarget.display_name || parseTarget.name}</h2><p>父子分块默认启用，可在本次解析中关闭。</p></div><button aria-label="关闭" disabled={busy} onClick={() => setParseTarget(null)}>×</button></div><label className="kb-toggle-row"><input type="checkbox" checked={parseOptions.xlsx_first_row_as_header} onChange={e => setParseOptions(old => ({ ...old, xlsx_first_row_as_header: e.target.checked }))} /><span><strong>Excel 首行作为表头</strong><small>仅适用于第一行就是字段名的 Excel。关闭时用 A/B/C 列名并保留首行；报表标题、多层表头请关闭。</small></span></label><label className="kb-toggle-row"><input type="checkbox" checked={parseOptions.parent_child} onChange={e => setParseOptions(old => ({ ...old, parent_child: e.target.checked }))} /><span><strong>启用父子分块</strong><small>父块用于补充上下文，子块参与检索。参数按字符计算。</small></span></label>{parseOptions.parent_child && <><div className="kb-parent-child-note">参考值尚未用真实题集验证。已有索引不会因打开设置而改变；重新解析会创建本次文件的新切块结果。</div><div className="kb-parent-child-fields">{([['parent_chunk_size', '父块大小', 4096, 1], ['parent_chunk_overlap', '父块重叠', 0, 0], ['child_chunk_size', '子块大小', 384, 1], ['child_chunk_overlap', '子块重叠', 48, 0]] as const).map(([key, label, , min]) => <label key={key}>{label}<input type="number" min={min} max={65536} value={parseOptions[key]} onChange={e => setParseOptions(old => ({ ...old, [key]: Number(e.target.value) }))} /></label>)}</div></>}{error && <div className="kb-error" role="alert">{error}</div>}<div className="kb-modal-actions"><button type="button" className="kb-secondary" disabled={busy} onClick={() => setParseTarget(null)}>取消</button><button type="button" className="kb-primary" disabled={busy || (parseOptions.parent_child && (parseOptions.parent_chunk_size < 1 || parseOptions.parent_chunk_size > 65536 || parseOptions.parent_chunk_overlap < 0 || parseOptions.parent_chunk_overlap >= parseOptions.parent_chunk_size || parseOptions.child_chunk_size < 1 || parseOptions.child_chunk_size > 65536 || parseOptions.child_chunk_overlap < 0 || parseOptions.child_chunk_overlap >= parseOptions.child_chunk_size))} onClick={() => void confirmParse()}>{busy ? '提交中…' : '开始解析'}</button></div></div></div>}
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

