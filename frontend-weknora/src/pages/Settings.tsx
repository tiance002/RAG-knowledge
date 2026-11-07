import { FormEvent, useCallback, useEffect, useState } from 'react';
import { MessageOutlined, DatabaseOutlined, PictureOutlined, FilterOutlined } from '@ant-design/icons';
import './Settings.css';

type ModelKind = 'llm' | 'embedding' | 'vlm' | 'rerank';
type Model = { id: string; name: string; model_type: ModelKind; model_name: string; base_url: string; context_length: number };
type ModelOption = { id: string; name: string; type: string };
type Preferences = { defaultLlmModel: string; defaultEmbeddingModel: string; defaultVlmModel: string; maxConcurrency: number; chunkSize: number; chunkOverlap: number; maxChatHistoryRounds: number };
type ModelForm = { name: string; model_type: ModelKind; model_name: string; base_url: string; api_key: string; context_length: number };

const categories: { id: ModelKind | 'all'; label: string }[] = [
  { id: 'all', label: '全部' }, { id: 'llm', label: '对话' },
  { id: 'embedding', label: 'Embedding' }, { id: 'rerank', label: 'ReRank' },
  { id: 'vlm', label: '视觉' },
];
const kindLabel: Record<ModelKind, string> = { llm: '语言模型', embedding: '向量模型', vlm: '视觉模型', rerank: '重排序模型' };
const emptyModel = (kind: ModelKind = 'llm'): ModelForm => ({ name: '', model_type: kind, model_name: '', base_url: '', api_key: '', context_length: 4096 });

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!response.ok) throw new Error(`请求失败（${response.status}）`);
  const result = await response.json();
  return result.data as T;
}
function preferencesPayload(p: Preferences) {
  return { default_llm_model: p.defaultLlmModel || '', default_embedding_model: p.defaultEmbeddingModel || '', default_vlm_model: p.defaultVlmModel || '', max_concurrency: p.maxConcurrency, chunk_size: p.chunkSize, chunk_overlap: p.chunkOverlap, max_chat_history_rounds: p.maxChatHistoryRounds };
}
function safeUrl(value: string) {
  try { const url = new URL(value); return `${url.origin}${url.pathname}`; } catch { return '模型服务地址'; }
}

export default function Settings() {
  const [models, setModels] = useState<Model[]>([]);
  const [options, setOptions] = useState<ModelOption[]>([]);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [category, setCategory] = useState<ModelKind | 'all'>('all');
  const [section, setSection] = useState<'models' | 'preferences'>('models');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [editing, setEditing] = useState<Model | null>(null);
  const [form, setForm] = useState<ModelForm | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextModels, nextOptions, nextPreferences] = await Promise.all([
        request<Model[]>('/settings/models'), request<ModelOption[]>('/models'), request<Preferences>('/settings'),
      ]);
      setModels(nextModels); setOptions(nextOptions); setPreferences(nextPreferences);
    } catch {
      setNotice({ text: '加载设置失败，请检查后端连接后重试。', error: true });
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const openCreate = () => { setNotice(null); setEditing(null); setForm(emptyModel(category === 'all' ? 'llm' : category)); };
  const openEdit = (model: Model) => { setNotice(null); setEditing(model); setForm({ name: model.name, model_type: model.model_type, model_name: model.model_name, base_url: model.base_url, api_key: '', context_length: model.context_length }); };
  const saveModel = async (event: FormEvent) => {
    event.preventDefault();
    if (!form) return;
    setBusy(true); setNotice(null);
    try {
      const payload: Partial<ModelForm> = { ...form };
      if (editing && !payload.api_key) delete payload.api_key;
      await request(editing ? `/settings/models/${encodeURIComponent(editing.id)}` : '/settings/models', { method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload) });
      setForm(null); setNotice({ text: editing ? '模型已更新。' : '模型已添加，请测试连接。', error: false });
      await load();
    } catch { setNotice({ text: '保存模型失败，请检查填写内容或服务状态。', error: true }); }
    finally { setBusy(false); }
  };
  const testModel = async (model: Model) => {
    setTesting(model.id); setNotice(null);
    try { await request(`/settings/models/${encodeURIComponent(model.id)}/test`, { method: 'POST' }); setNotice({ text: `“${model.name}”连接测试成功。`, error: false }); }
    catch { setNotice({ text: `“${model.name}”连接测试失败，请检查服务地址、凭据和模型名称。`, error: true }); }
    finally { setTesting(null); }
  };
  const deleteModel = async (model: Model) => {
    if (!window.confirm(`确定删除“${model.name}”吗？引用此模型的知识库可能需要重新配置。`)) return;
    setNotice(null);
    try {
      await request(`/settings/models/${encodeURIComponent(model.id)}`, { method: 'DELETE' });
      if (preferences && [preferences.defaultLlmModel, preferences.defaultEmbeddingModel, preferences.defaultVlmModel].includes(model.id)) {
        const updated = { ...preferences, defaultLlmModel: preferences.defaultLlmModel === model.id ? '' : preferences.defaultLlmModel, defaultEmbeddingModel: preferences.defaultEmbeddingModel === model.id ? '' : preferences.defaultEmbeddingModel, defaultVlmModel: preferences.defaultVlmModel === model.id ? '' : preferences.defaultVlmModel };
        try { await request('/settings', { method: 'PUT', body: JSON.stringify(preferencesPayload(updated)) }); }
        catch { await load(); setNotice({ text: '模型已删除，但偏好清理失败，请手动更新并保存偏好。', error: true }); return; }
      }
      setNotice({ text: '模型已删除。', error: false }); await load();
    } catch { setNotice({ text: '删除模型失败，请稍后重试。', error: true }); }
  };
  const savePreferences = async (event: FormEvent) => {
    event.preventDefault(); if (!preferences) return;
    if (preferences.chunkOverlap >= preferences.chunkSize) { setNotice({ text: '分块重叠必须小于分块大小。', error: true }); return; }
    setBusy(true); setNotice(null);
    try { const saved = await request<Preferences>('/settings', { method: 'PUT', body: JSON.stringify(preferencesPayload(preferences)) }); setPreferences(saved); setNotice({ text: '使用偏好已保存。', error: false }); }
    catch { setNotice({ text: '保存偏好失败，请检查输入并重试。', error: true }); }
    finally { setBusy(false); }
  };
  const updatePreference = <K extends keyof Preferences>(key: K, value: Preferences[K]) => setPreferences(old => old ? { ...old, [key]: value } : old);
  const visible = models.filter(model => category === 'all' || model.model_type.toLowerCase() === category);

  return <main className="wk-settings">
    <aside className="wk-settings-nav" aria-label="设置导航">
      <strong>设置</strong>
      <button type="button" className={section === 'models' ? 'active' : ''} aria-current={section === 'models' ? 'page' : undefined} onClick={() => { setSection('models'); setNotice(null); }}>模型配置</button>
      <button type="button" className={section === 'preferences' ? 'active' : ''} aria-current={section === 'preferences' ? 'page' : undefined} onClick={() => { setSection('preferences'); setNotice(null); }}>使用偏好</button>
    </aside>
    <div className="wk-settings-body">
    {notice && <div className={`wk-notice ${notice.error ? 'error' : ''}`} role="status">{notice.text}<button aria-label="关闭提示" onClick={() => setNotice(null)}>×</button></div>}
    {loading ? <p className="wk-muted">正在加载设置…</p> : section === 'models' ? <>
      <header className="wk-settings-header"><div><h1>模型配置</h1><p>管理不同类型的 AI 模型，支持本地模型和远程 API</p></div></header>
      <div className="wk-info-strip"><strong>模型配置</strong><span>添加自己的模型服务，并在需要时测试连接。</span></div>
      <div className="wk-model-tabs" role="tablist" aria-label="模型类型">{categories.map(item => <button key={item.id} role="tab" type="button" aria-selected={category === item.id} onClick={() => setCategory(item.id)} className={category === item.id ? 'active' : ''}>{item.label}({item.id === 'all' ? models.length : models.filter(model => model.model_type.toLowerCase() === item.id).length})</button>)}</div>
      <section className="wk-model-section" aria-label="模型列表">
        <div className="wk-model-grid">{visible.map(model => <article className="wk-model-card" key={model.id}>
          <div className="wk-model-card-head"><span className={`wk-model-symbol wk-model-symbol-${model.model_type}`} aria-hidden="true">{model.model_type === 'llm' ? <MessageOutlined /> : model.model_type === 'embedding' ? <DatabaseOutlined /> : model.model_type === 'vlm' ? <PictureOutlined /> : <FilterOutlined />}</span>
            <div className="wk-model-card-title"><h3 title={model.name}>{model.name}</h3><p title={safeUrl(model.base_url)}>自定义 · {model.model_name}{model.context_length ? ` · 上下文 ${model.context_length.toLocaleString()}` : ''}</p></div>
          </div>
          <div className="wk-model-actions"><button type="button" onClick={() => openEdit(model)} aria-label={`编辑 ${model.name}`}>编辑</button><button type="button" disabled={testing === model.id} onClick={() => void testModel(model)} aria-label={`测试连接 ${model.name}`}>{testing === model.id ? '测试中…' : '测试连接'}</button><button type="button" className="danger" onClick={() => void deleteModel(model)} aria-label={`删除 ${model.name}`}>删除</button></div>
        </article>)}
        <button type="button" className="wk-add-card" onClick={openCreate}><span>＋</span><strong>添加模型</strong></button></div>
      </section>
    </> : preferences && <>
      <header className="wk-settings-header"><div><h1>使用偏好</h1><p>选择模型并设置文档处理参数</p></div></header>
      <form className="wk-preferences" onSubmit={savePreferences}>
        <div className="wk-preference-heading"><div><h2>模型选择</h2><p>未选择时保持空白，也可在使用时选择。</p></div></div>
        <div className="wk-preference-rows">
          {([['defaultLlmModel', '对话模型', '用于智能问答', 'llm'], ['defaultEmbeddingModel', '向量模型', '用于知识库向量化', 'embedding'], ['defaultVlmModel', '视觉模型', '用于图片理解', 'vlm']] as const).map(([key, label, description, type]) => <label className="wk-preference-row" key={key}><span><strong>{label}</strong><small>{description}</small></span><select value={preferences[key] || ''} onChange={e => updatePreference(key, e.target.value)}><option value="">请选择模型</option>{options.filter(option => option.type.toLowerCase() === type).map(option => <option key={option.id} value={option.id}>{option.name}</option>)}</select></label>)}
        </div>
        <div className="wk-preference-heading wk-parameter-heading"><div><h2>文档与对话</h2><p>调整文档分块和对话处理参数。</p></div></div>
        <div className="wk-preference-rows">
          {([['maxChatHistoryRounds', '最大历史对话轮数', '发送给模型的历史对话轮数', 1, 100], ['maxConcurrency', '最大并发解析数', '同时处理的文档解析任务数', 1, 32], ['chunkSize', '分块大小', '文档片段的目标长度', 128, 2048], ['chunkOverlap', '分块重叠', '相邻文档片段的重叠长度', 0, 512]] as const).map(([key, label, description, min, max]) => <label className="wk-preference-row" key={key}><span><strong>{label}</strong><small>{description}</small></span><input required type="number" min={min} max={max} value={preferences[key]} onChange={e => updatePreference(key, e.target.valueAsNumber)} /></label>)}
        </div>
        <div className="wk-preference-save"><button className="wk-primary" disabled={busy} type="submit">{busy ? '保存中…' : '保存偏好'}</button></div>
      </form>
    </>}
    {form && <div className="wk-modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget && !busy) setForm(null); }}><section className="wk-modal" role="dialog" aria-modal="true" aria-labelledby="wk-model-modal-title"><div className="wk-modal-heading"><div><h2 id="wk-model-modal-title">{editing ? '编辑模型' : '添加模型'}</h2><p>填写模型服务的连接信息</p></div><button onClick={() => setForm(null)} aria-label="关闭">×</button></div>{notice?.error && <div className="wk-notice error" role="alert">{notice.text}</div>}<form onSubmit={saveModel} className="wk-model-form">
      <label>显示名称<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="例如：团队对话模型" /></label>
      <label>模型类型<select value={form.model_type} onChange={e => setForm({ ...form, model_type: e.target.value as ModelKind })}>{categories.filter(item => item.id !== 'all').map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
      <label>模型名称<input required value={form.model_name} onChange={e => setForm({ ...form, model_name: e.target.value })} placeholder="服务提供商的模型名称" /></label>
      <label>服务地址<input required type="text" value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} placeholder={form.model_type === 'rerank' ? '服务地址或本地模型路径' : 'https://example.com/v1'} /></label>
      <label>API Key<input type="password" autoComplete="new-password" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} placeholder={editing ? '留空以保留现有凭据' : '本地服务可留空'} /></label>
      <label>上下文长度<input required type="number" min="1" value={form.context_length} onChange={e => setForm({ ...form, context_length: e.target.valueAsNumber })} /></label>
      <div className="wk-modal-actions"><button type="button" className="wk-outline" onClick={() => setForm(null)}>取消</button><button type="submit" className="wk-primary" disabled={busy}>{busy ? '保存中…' : '保存模型'}</button></div>
    </form></section></div>}
  </div></main>;
}
