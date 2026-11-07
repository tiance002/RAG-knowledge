import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { FolderOutlined, DownOutlined, SendOutlined, CloseOutlined, StopOutlined } from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import { useChatHistory } from '../chatHistory';
import type { Conversation, Message } from '../chatHistory';
import './Chat.css';

type Model = { id: string; name: string; type: string };
type KB = { id: string; name: string };

export default function Chat() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { sessions, put, change } = useChatHistory();
  const session = sessions.find(item => item.id === id);
  const [models, setModels] = useState<Model[]>([]);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [preferredModel, setPreferredModel] = useState('');
  const [model, setModel] = useState('');
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [mode, setMode] = useState<'normal' | 'agent'>('normal');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [kbSearch, setKbSearch] = useState('');
  const controller = useRef<{ abort: AbortController; id: string } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const responses = await Promise.all(['/models', '/knowledge-bases', '/settings'].map(path => fetch('/api/v1' + path)));
        if (responses.some(r => !r.ok)) throw new Error('无法加载模型或知识库');
        const [mr, kr, sr] = await Promise.all(responses.map(r => r.json()));
        const available: Model[] = (Array.isArray(mr) ? mr : mr.data || []).filter((m: Model) => ['llm', 'vlm'].includes(m.type?.toLowerCase()));
        setModels(available); setKbs(Array.isArray(kr) ? kr : kr.data || []);
        const saved = available.some(m => m.id === sr.data?.defaultLlmModel) ? sr.data.defaultLlmModel : '';
        setPreferredModel(saved);
        if (!id) setModel(saved);
      } catch (e) { setError(e instanceof Error ? e.message : '加载失败'); }
    };
    void load();
    return () => controller.current?.abort.abort();
  }, []);

  useEffect(() => {
    if (controller.current && controller.current.id !== id) controller.current.abort.abort();
    const stored = sessions.find(item => item.id === id);
    setText(''); setError(''); setPickerOpen(false);
    setModel(stored?.model || preferredModel); setKbIds(stored?.kbIds || []); setMode(stored?.mode || 'normal');
    if (!controller.current || controller.current.id !== id) setBusy(false);
  }, [id, location.key]);

  useEffect(() => { if (session?.messages.length) bottom.current?.scrollIntoView({ block: 'end' }); }, [session?.messages]);
  const messages = session?.messages || [];
  const updateConfig = (values: Partial<Pick<Conversation, 'model' | 'kbIds' | 'mode'>>) => {
    if (id) change(id, item => ({ ...item, ...values }));
  };
  const toggleKB = (kbId: string) => {
    const next = kbIds.includes(kbId) ? kbIds.filter(value => value !== kbId) : [...kbIds, kbId];
    setKbIds(next); updateConfig({ kbIds: next });
  };

  const send = async () => {
    if (!text.trim() || busy) return;
    if (!model || !models.some(item => item.id === model)) { setError('请先选择对话模型；如果列表为空，请在模型配置中添加。'); return; }
    setError(''); setPickerOpen(false);
    const conversationId = id || crypto.randomUUID();
    const next: Message[] = [...messages, { role: 'user', content: text.trim() }];
    const startedAt = Date.now();
    put({ id: conversationId, title: session?.title || text.trim().slice(0, 48), updatedAt: startedAt, model, kbIds, mode,
      messages: [...next, { role: 'assistant', content: '', steps: [], streaming: true }] });
    setText(''); setBusy(true);
    const abort = new AbortController(); controller.current = { abort, id: conversationId };
    if (!id) navigate(`/chat/${conversationId}`, { replace: true });
    let terminal = false;
    const assistantIndex = next.length;
    const update = (fn: (message: Message) => Message) => change(conversationId, item => ({ ...item, messages: item.messages.map((message, index) => index === assistantIndex ? fn(message) : message) }));
    try {
      const response = await fetch('/api/v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: abort.signal,
        body: JSON.stringify({ messages: next.filter(m => m.content.trim()).map(({ role, content }) => ({ role, content })), kb_ids: kbIds, model_id: model, mode, stream: true, rerank_enabled: false }) });
      if (!response.ok || !response.body) throw new Error(`请求失败（HTTP ${response.status}）`);
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const part = await reader.read(); buffer += decoder.decode(part.value || new Uint8Array(), { stream: !part.done }).replace(/\r\n/g, '\n');
        const events = buffer.split('\n\n'); buffer = events.pop() || '';
        for (const block of events) {
          const type = block.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
          const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
          if (!data) continue;
          const payload = JSON.parse(data);
          if (type === 'answer_chunk') update(m => ({ ...m, content: m.content + (payload.content || '') }));
          if (type === 'agent_thought') update(m => ({ ...m, steps: [...(m.steps || []), payload.content || payload.step || ''] }));
          if (type === 'rag_result') update(m => ({ ...m, citations: payload.citations || [] }));
          if (type === 'error') throw new Error(payload.message || payload.error || '生成失败');
          if (type === 'done') { terminal = true; const state = payload.generation_status; update(m => ({ ...m, streaming: false, status: state && !['COMPLETED', 'NOT_RUN'].includes(state) ? `生成状态：${state}` : undefined })); }
        }
        if (part.done || terminal) break;
      }
      if (!terminal) throw new Error('连接中断，未收到完成状态');
    } catch (e) { const reason = abort.signal.aborted ? '生成已停止' : e instanceof Error ? e.message : '请求失败'; update(m => ({ ...m, streaming: false, status: reason })); }
    finally { if (controller.current?.id === conversationId) { setBusy(false); controller.current = null; } }
  };

  const composer = <div className="chat-compose-wrap">
    {error && <div className="chat-error" role="alert">{error} {!models.length && <Link to="/settings">配置模型</Link>}</div>}
    <div className={`chat-composer ${pickerOpen ? 'picker-open' : ''}`}>
      {!!kbIds.length && <div className="chat-selected">{kbIds.map(kbId => <span className="chat-kb-chip" key={kbId}><FolderOutlined /> {kbs.find(kb => kb.id === kbId)?.name || '已失效的知识库'}<button aria-label={`移除 ${kbs.find(kb => kb.id === kbId)?.name || '知识库'}`} onClick={() => toggleKB(kbId)}><CloseOutlined /></button></span>)}</div>}
      <textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} placeholder="输入问题或描述任务…" rows={3} aria-label="输入问题或描述任务" />
      <div className="chat-compose-actions"><div className="compose-left">
        <label className="chat-select-wrap"><span className="sr-only">对话模式</span><select value={mode} onChange={e => { const value = e.target.value as 'normal' | 'agent'; setMode(value); updateConfig({ mode: value }); }}><option value="normal">快速问答</option><option value="agent">Agent</option></select></label>
        <div className="kb-picker-container"><button type="button" className={`picker-trigger ${kbIds.length ? 'has-selection' : ''}`} aria-label="选择知识库" aria-expanded={pickerOpen} aria-controls="chat-kb-picker" onClick={() => setPickerOpen(!pickerOpen)}><FolderOutlined /> <span>知识库{kbIds.length ? ` (${kbIds.length})` : ''}</span> <DownOutlined /></button>
          {pickerOpen && <div id="chat-kb-picker" className="chat-kb-popover"><div className="picker-heading"><strong>选择知识库</strong><button aria-label="关闭知识库选择" onClick={() => setPickerOpen(false)}><CloseOutlined /></button></div><input value={kbSearch} onChange={e => setKbSearch(e.target.value)} placeholder="搜索知识库" aria-label="搜索知识库" /><div className="picker-list">{kbs.filter(kb => kb.name.toLowerCase().includes(kbSearch.toLowerCase())).map(kb => <label key={kb.id}><input type="checkbox" checked={kbIds.includes(kb.id)} onChange={() => toggleKB(kb.id)} /><FolderOutlined /><span>{kb.name}</span></label>)}{kbs.length === 0 && <p>暂无知识库，先去创建一个</p>}</div><div className="picker-footer">已选 {kbIds.length} 个 <button onClick={() => setPickerOpen(false)}>完成</button></div></div>}
        </div>
      </div><div className="compose-right"><label className="chat-select-wrap"><span className="sr-only">对话模型</span><select value={model} onChange={e => { setModel(e.target.value); updateConfig({ model: e.target.value }); }}><option value="">请选择模型</option>{models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>{busy ? <button className="send-button stop" aria-label="停止生成" onClick={() => controller.current?.abort.abort()}><StopOutlined /></button> : <button className="send-button" aria-label="发送" onClick={() => void send()} disabled={!text.trim()}><SendOutlined /></button>}</div></div>
    </div>
    <p className="compose-hint">内容由模型生成，请结合来源核对</p>
  </div>;

  return <div className={`page chat-page ${messages.length ? 'has-messages' : 'is-empty'}`}>
    {messages.length ? <><header className="chat-conversation-title">{session?.title || '对话'}</header><div className="chat-thread" aria-live="polite">{messages.map((m, i) => <div className={`chat-message ${m.role}`} key={i}><div className="chat-bubble">{m.role === 'assistant' && <div className="chat-assistant-label"><span>知</span> 知识助手</div>}{m.steps && m.steps.length > 0 && <details><summary>查看处理过程</summary>{m.steps.map((step, j) => <p key={j}>{step}</p>)}</details>}<div className="chat-answer"><ReactMarkdown>{m.content || (m.streaming ? '正在处理…' : '')}</ReactMarkdown></div>{m.status && <p className="chat-error">{m.status}</p>}{!!m.citations?.length && <details><summary>参考来源（{m.citations.length}）</summary>{m.citations.map((c, j) => <div className="chat-citation" key={c.chunkId || j}><strong>{c.fileName || c.kbName || '来源'}</strong><small>{c.location}</small><p>{c.content}</p></div>)}</details>}</div></div>)}<div ref={bottom} /></div>{composer}</> : <div className="chat-welcome"><h1>Hi，我是你的知识助手</h1><p>让你的知识触手可及</p>{composer}</div>}
  </div>;
}

