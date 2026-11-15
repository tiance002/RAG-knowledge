import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { FolderOutlined, DownOutlined, SendOutlined, CloseOutlined, StopOutlined } from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import { useChatHistory } from '../chatHistory';
import type { Conversation, Message } from '../chatHistory';
import { createConversationId } from '../conversationId';
import { conversationApi, normalizeCitations } from '../conversationApi';
import type { ServerSession, ServerTurn } from '../conversationApi';
import { createSSEParser } from '../sseParser';
import './Chat.css';

type Model = { id: string; name: string; type: string };
type KB = { id: string; name: string };

export default function Chat() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { sessions, put, change, loadSession, saveServer } = useChatHistory();
  const session = sessions.find(item => item.id === id);
  const [models, setModels] = useState<Model[]>([]);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [preferredModel, setPreferredModel] = useState('');
  const [model, setModel] = useState('');
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [mode, setMode] = useState<'normal' | 'agent'>('normal');
  const [text, setText] = useState('');
  const [, setActivityVersion] = useState(0);
  const [error, setError] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [kbSearch, setKbSearch] = useState('');
  const controllers = useRef(new Map<string, { abort: AbortController; turnId?: string }>());
  const submitting = useRef(new Set<string>());
  const activityChanged = () => setActivityVersion(v => v + 1);
  const activityKey = id || '__draft__';
  const busy = submitting.current.has(activityKey) || controllers.current.has(activityKey);
  const historyLoader = useRef(loadSession); historyLoader.current = loadSession;
  const initialized = useRef('');
  const routeId = useRef(id); routeId.current = id;
  const routeKey = useRef(location.key); routeKey.current = location.key;
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
        if (!routeId.current) setModel(saved);
      } catch (e) { setError(e instanceof Error ? e.message : '加载失败'); }
    };
    void load();
    // Streams remain bound to their captured session when navigation changes.
  }, []);

  useEffect(() => {
    setText(''); setError(''); setPickerOpen(false); initialized.current = '';
    const loadRoute = location.key;
    if (id && !id.startsWith('legacy:') && !controllers.current.has(id)) void historyLoader.current(id).catch(e => { if (routeKey.current === loadRoute) setError(e instanceof Error ? e.message : '加载历史失败'); });
    if (!id) { setModel(preferredModel); setKbIds([]); setMode('normal'); }
  }, [id, location.key]);
  useEffect(() => {
    if (session && (session.loaded || session.legacy) && initialized.current !== session.id) {
      initialized.current = session.id; setModel(session.model || preferredModel); setKbIds(session.kbIds); setMode(session.mode);
    }
  }, [session, preferredModel]);
  useEffect(() => {
    if (!session?.activeTurnId || controllers.current.has(id || '__draft__') || session.legacy) return;
    let disposed = false;
    const recover = async () => { try { await conversationApi<ServerTurn>('/turns/' + session.activeTurnId); if (!disposed) await historyLoader.current(session.id); } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '恢复状态失败'); } };
    const timer = window.setInterval(() => void recover(), 3000); void recover();
    return () => { disposed = true; window.clearInterval(timer); };
  }, [id, session?.activeTurnId, busy]);
  useEffect(() => { if (session?.messages.length) bottom.current?.scrollIntoView({ block: 'end' }); }, [session?.messages]);
  const messages = session?.messages || [];
  const toggleKB = (kbId: string) => setKbIds(kbIds.includes(kbId) ? kbIds.filter(value => value !== kbId) : [...kbIds, kbId]);
  const cancel = async () => {
    const selectedId = id;
    const selectedRoute = routeKey.current;
    const owned = controllers.current.get(selectedId || '__draft__');
    const active = owned?.turnId || session?.activeTurnId;
    if (!active) { setError('正在确认服务端请求状态，请稍候再停止'); return; }
    try { await conversationApi('/turns/' + active + '/cancel', { method: 'POST' }); owned?.abort.abort(); if (selectedId) await historyLoader.current(selectedId); }
    catch (e) { if (routeKey.current === selectedRoute) setError(e instanceof Error ? e.message : '停止失败'); }
  };

  const send = async () => {
    if (!text.trim() || busy || session?.activeTurnId || (id && (!session || (!session.loaded && !session.legacy)))) return;
    const sourceKey = id || '__draft__';
    if (submitting.current.has(sourceKey) || controllers.current.has(sourceKey)) return;
    const sourceRoute = routeKey.current;
    if (!model || !models.some(item => item.id === model)) { setError('请先选择对话模型；如果列表为空，请在模型配置中添加。'); return; }
    submitting.current.add(sourceKey); activityChanged();
    setError(''); setPickerOpen(false);
    const question = text.trim();
    let target: Conversation;
    let clientRequestId: string;
    const config = { ...(session?.legacy ? {} : session?.config), model_id: model, mode: mode === 'normal' ? 'chat' : 'agent' };
    const sameScope = !!session && !session.legacy && [...session.kbIds].sort().join('\0') === [...kbIds].sort().join('\0');
    try {
      clientRequestId = createConversationId();
      const saved = await conversationApi<ServerSession>(sameScope ? '/sessions/' + encodeURIComponent(session!.id) : '/sessions', { method: sameScope ? 'PATCH' : 'POST', body: JSON.stringify(sameScope ? { expected_revision: session!.revision, config_overrides: config } : { title: question.slice(0, 48), scope_mode: kbIds.length ? 'SELECTED' : 'ALL_ENABLED', kb_ids: kbIds, config_overrides: config }) });
      target = { ...saveServer(saved), messages: sameScope ? messages : [], loaded: true };
    } catch (e) { submitting.current.delete(sourceKey); activityChanged(); if (routeKey.current === sourceRoute) setError(e instanceof Error ? e.message : '会话保存失败'); return; }
    const conversationId = target.id;
    const next: Message[] = [...target.messages, { role: 'user', content: question }];
    put({ ...target, messages: [...next, { role: 'assistant', content: '', steps: [], streaming: true, turnId: clientRequestId }] });
    if (routeKey.current === sourceRoute) setText('');
    const abort = new AbortController(); const owned = { abort, turnId: undefined as string | undefined };
    controllers.current.set(conversationId, owned); submitting.current.delete(sourceKey); activityChanged();
    if (id !== conversationId && routeKey.current === sourceRoute) navigate(`/chat/${conversationId}`, { replace: true });
    let terminal = false;
    let assistantTurnId = clientRequestId;
    const update = (fn: (message: Message) => Message) => change(conversationId, item => ({ ...item, messages: item.messages.map(message => message.role === 'assistant' && message.turnId === assistantTurnId ? fn(message) : message) }));
    try {
      const response = await fetch('/api/v1/chat/sessions/' + encodeURIComponent(conversationId) + '/turns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: abort.signal,
        body: JSON.stringify({ question, client_request_id: clientRequestId, expected_revision: target.revision }) });
      if (!response.ok) { let detail = ''; try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || ''); } catch { /* preserve HTTP status */ } throw new Error(`请求失败（HTTP ${response.status}）${detail ? '：' + detail : ''}`); }
      if (!response.body) throw new Error('服务端未返回事件流');
      const reader = response.body.getReader(); const decoder = new TextDecoder();
      const parser = createSSEParser((type, data) => {
          if (terminal || !data) return;
          const payload = JSON.parse(data);
          if (type === 'turn_started') { update(m => ({ ...m, turnId: payload.turn_id })); assistantTurnId = payload.turn_id; if (controllers.current.get(conversationId) === owned) owned.turnId = payload.turn_id; change(conversationId, item => ({ ...item, revision: Math.max(item.revision || 0, payload.revision || 0), activeTurnId: payload.turn_id })); }
          if (type === 'answer_chunk') update(m => ({ ...m, content: m.content + (payload.content || '') }));
          if (type === 'agent_thought') update(m => ({ ...m, steps: [...(m.steps || []), payload.content || payload.step || ''] }));
          if (type === 'rag_result') update(m => ({ ...m, citations: normalizeCitations(payload.citations || []) }));
          if (type === 'error') throw new Error(payload.message || payload.error || '生成失败');
          if (type === 'done') { terminal = true; change(conversationId, item => ({ ...item, revision: Math.max(item.revision || 0, payload.session_revision || 0), activeTurnId: null })); if (payload.replayed) update(m => ({ ...m, content: payload.assistant_content || m.content, citations: normalizeCitations(payload.citations || []) })); const state = payload.generation_status; update(m => ({ ...m, streaming: false, status: state && !['COMPLETED', 'NOT_RUN'].includes(state) ? `生成状态：${state}` : undefined })); }
      });
      while (true) {
        const part = await reader.read();
        parser.push(decoder.decode(part.value || new Uint8Array(), { stream: !part.done }), part.done);
        if (part.done || terminal) break;
      }
      if (!terminal) throw new Error('连接中断，未收到完成状态');
    } catch (e) { const reason = abort.signal.aborted ? '已请求停止，正在核对服务端状态' : e instanceof Error ? e.message : '请求失败'; update(m => ({ ...m, streaming: false, status: reason })); if (routeId.current === conversationId) setError(reason + '；不会自动重发问题。'); }
    finally { try { await historyLoader.current(conversationId); } catch (e) { if (routeId.current === conversationId) setError(e instanceof Error ? e.message : '无法恢复会话，请刷新查看；不要重复发送'); }
      if (controllers.current.get(conversationId) === owned) { controllers.current.delete(conversationId); activityChanged(); } }
  };

  const composer = <div className="chat-compose-wrap">
    {error && <div className="chat-error" role="alert">{error} {!models.length && <Link to="/settings">配置模型</Link>}</div>}
    <div className={`chat-composer ${pickerOpen ? 'picker-open' : ''}`}>
      {!!kbIds.length && <div className="chat-selected">{kbIds.map(kbId => <span className="chat-kb-chip" key={kbId}><FolderOutlined /> {kbs.find(kb => kb.id === kbId)?.name || '已失效的知识库'}<button aria-label={`移除 ${kbs.find(kb => kb.id === kbId)?.name || '知识库'}`} onClick={() => toggleKB(kbId)}><CloseOutlined /></button></span>)}</div>}
      <textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} placeholder="输入问题或描述任务…" rows={3} aria-label="输入问题或描述任务" />
      <div className="chat-compose-actions"><div className="compose-left">
        <label className="chat-select-wrap"><span className="sr-only">对话模式</span><select value={mode} onChange={e => { const value = e.target.value as 'normal' | 'agent'; setMode(value);  }}><option value="normal">快速问答</option><option value="agent">Agent</option></select></label>
        <div className="kb-picker-container"><button type="button" className={`picker-trigger ${kbIds.length ? 'has-selection' : ''}`} aria-label="选择知识库" aria-expanded={pickerOpen} aria-controls="chat-kb-picker" onClick={() => setPickerOpen(!pickerOpen)}><FolderOutlined /> <span>知识库{kbIds.length ? ` (${kbIds.length})` : ''}</span> <DownOutlined /></button>
          {pickerOpen && <div id="chat-kb-picker" className="chat-kb-popover"><div className="picker-heading"><strong>选择知识库</strong><button aria-label="关闭知识库选择" onClick={() => setPickerOpen(false)}><CloseOutlined /></button></div><input value={kbSearch} onChange={e => setKbSearch(e.target.value)} placeholder="搜索知识库" aria-label="搜索知识库" /><div className="picker-list">{kbs.filter(kb => kb.name.toLowerCase().includes(kbSearch.toLowerCase())).map(kb => <label key={kb.id}><input type="checkbox" checked={kbIds.includes(kb.id)} onChange={() => toggleKB(kb.id)} /><FolderOutlined /><span>{kb.name}</span></label>)}{kbs.length === 0 && <p>暂无知识库，先去创建一个</p>}</div><div className="picker-footer">已选 {kbIds.length} 个 <button onClick={() => setPickerOpen(false)}>完成</button></div></div>}
        </div>
      </div><div className="compose-right"><label className="chat-select-wrap"><span className="sr-only">对话模型</span><select value={model} onChange={e => { setModel(e.target.value);  }}><option value="">请选择模型</option>{models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>{(busy || session?.activeTurnId) ? <button className="send-button stop" aria-label="停止生成" onClick={() => void cancel()}><StopOutlined /></button> : <button className="send-button" aria-label="发送" onClick={() => void send()} disabled={!text.trim() || (!!id && !session?.legacy && !session?.loaded)}><SendOutlined /></button>}</div></div>
    </div>
    <p className="compose-hint">内容由模型生成，请结合来源核对</p>
  </div>;

  return <div className={`page chat-page ${messages.length ? 'has-messages' : 'is-empty'}`}>
    {messages.length ? <><header className="chat-conversation-title">{session?.title || '对话'}{session?.legacy && <small> · 浏览器只读备份，发送将创建服务端新会话</small>}</header><div className="chat-thread" aria-live="polite">{session?.hasOlder && <button disabled={busy || !!session.activeTurnId} onClick={() => { if (id) void loadSession(id, true).catch(e => setError(e instanceof Error ? e.message : '加载失败')); }}>加载更早的消息</button>}{messages.map((m, i) => <div className={`chat-message ${m.role}`} key={i}><div className="chat-bubble">{m.role === 'assistant' && <div className="chat-assistant-label"><span>知</span> 知识助手</div>}{m.steps && m.steps.length > 0 && <details><summary>查看处理过程</summary>{m.steps.map((step, j) => <p key={j}>{step}</p>)}</details>}<div className="chat-answer"><ReactMarkdown>{m.content || (m.streaming ? '正在处理…' : '')}</ReactMarkdown></div>{m.status && <p className="chat-error">{m.status}</p>}{!!m.citations?.length && <details><summary>参考来源（{m.citations.length}）</summary>{m.citations.map((c, j) => <div className="chat-citation" key={c.chunkId || j}><strong>{c.fileName || c.kbName || '来源'}</strong><small>{c.location}</small><p>{c.content}</p></div>)}</details>}</div></div>)}<div ref={bottom} /></div>{composer}</> : <div className="chat-welcome"><h1>Hi，我是你的知识助手</h1><p>让你的知识触手可及</p>{composer}</div>}
  </div>;
}

