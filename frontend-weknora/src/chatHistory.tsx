import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { conversationApi, normalizeCitations } from './conversationApi';
import type { ServerSession, ServerTurn } from './conversationApi';

export type Citation = { chunkId?: string; fileName?: string; kbName?: string; content?: string; location?: string };
export type Message = { role: 'user' | 'assistant'; content: string; citations?: Citation[]; status?: string; steps?: string[]; streaming?: boolean; turnId?: string };
export type Conversation = { id: string; title: string; updatedAt: number; model: string; kbIds: string[]; mode: 'normal' | 'agent'; messages: Message[]; revision?: number; activeTurnId?: string | null; legacy?: boolean; legacyIndex?: number; loaded?: boolean; hasOlder?: boolean; oldestSequence?: number; config?: Record<string, unknown> };
type ChatHistory = { sessions: Conversation[]; put: (session: Conversation) => void; change: (id: string, change: (item: Conversation) => Conversation) => void; remove: (id: string) => Promise<void>; storageError: boolean; error: string; loading: boolean; reload: () => Promise<void>; loadSession: (id: string, older?: boolean) => Promise<void>; saveServer: (item: ServerSession) => Conversation; importLegacy: () => Promise<void>; exportLegacy: () => void; importing: boolean; importStatus: string; legacyCount: number };
const KEY = 'rag-workspace.chat-history.v1';
const Context = createContext<ChatHistory | null>(null);
function readArchive() {
  let raw: string | null = null;
  try { raw = localStorage.getItem(KEY); const parsed: unknown = JSON.parse(raw || '[]'); return { raw, originals: Array.isArray(parsed) ? parsed : [], error: !Array.isArray(parsed) }; }
  catch { return { raw, originals: [] as unknown[], error: true }; }
}
function serverConversation(item: ServerSession): Conversation { return { id: item.id, title: item.title, updatedAt: Date.parse(item.updated_at || '') || Date.now(), model: String(item.config_overrides?.model_id || ''), mode: item.config_overrides?.mode === 'agent' ? 'agent' : 'normal', kbIds: item.kb_ids || [], revision: item.revision, activeTurnId: item.active_turn_id, config: item.config_overrides || {}, messages: [] }; }
function turnMessages(turns: ServerTurn[]): Message[] { return turns.flatMap(t => [{ role: 'user' as const, content: t.user_content, turnId: t.id }, { role: 'assistant' as const, content: t.assistant_content || '', citations: normalizeCitations(t.citations), status: !['COMPLETED', 'SUCCEEDED', 'SUCCESS'].includes(t.status) ? `状态：${t.status}${t.generation_status ? ' / ' + t.generation_status : ''}` : undefined, streaming: false, turnId: t.id }]); }
// The cache can receive list/detail responses after newer streamed mutations.
export function mergeServerSession(cached: Conversation | undefined, item: ServerSession): Conversation {
  const mapped = serverConversation(item);
  if (cached && (cached.revision || 0) > item.revision) return cached;
  return { ...cached, ...mapped, messages: cached?.messages || [], loaded: cached?.loaded,
    hasOlder: cached?.hasOlder, oldestSequence: cached?.oldestSequence,
    activeTurnId: cached?.messages.some(m => m.streaming) && cached.revision === item.revision ? cached.activeTurnId : mapped.activeTurnId };
}
export function inspectArchive(originals: unknown[]) {
  let invalidRows = 0; let invalidMessages = 0;
  const legacy = originals.flatMap((raw, legacyIndex): Conversation[] => {
    if (!raw || typeof raw !== 'object' || !('id' in raw) || typeof raw.id !== 'string' || !('messages' in raw) || !Array.isArray(raw.messages)) { invalidRows++; return []; }
    const v = raw as Conversation;
    const messages: Message[] = [];
    for (const value of v.messages) {
      if (!value || typeof value !== 'object' || !['user', 'assistant'].includes(value.role) || typeof value.content !== 'string') { invalidMessages++; continue; }
      messages.push({ role: value.role, content: value.content, streaming: false,
        citations: Array.isArray(value.citations) ? value.citations.filter(c => c && typeof c === 'object').map(c => ({ chunkId: typeof c.chunkId === 'string' ? c.chunkId : undefined, fileName: typeof c.fileName === 'string' ? c.fileName : undefined, kbName: typeof c.kbName === 'string' ? c.kbName : undefined, content: typeof c.content === 'string' ? c.content : undefined, location: typeof c.location === 'string' ? c.location : undefined })) : undefined,
        steps: Array.isArray(value.steps) ? value.steps.filter(step => typeof step === 'string') : undefined,
        status: typeof value.status === 'string' ? value.status : undefined });
    }
    return [{ id: 'legacy:' + legacyIndex + ':' + v.id, title: typeof v.title === 'string' ? v.title : '浏览器旧会话', model: typeof v.model === 'string' ? v.model : '', kbIds: Array.isArray(v.kbIds) ? v.kbIds.filter(k => typeof k === 'string') : [], updatedAt: Number(v.updatedAt) || 0, mode: v.mode === 'agent' ? 'agent' : 'normal', legacy: true, legacyIndex, loaded: true, messages }];
  });
  return { legacy, warning: invalidRows || invalidMessages ? `浏览器旧历史含 ${invalidRows} 个无效会话、${invalidMessages} 条无效消息；仅显示可读取内容，原始备份保留，可导出核对` : '' };
}
type ImportPreview = { sessions: { index: number; already_imported: boolean; session_id?: string }[]; errors?: unknown[]; counts?: Record<string, number>; importable_count?: number; existing_count?: number };
function importErrors(value: { errors?: unknown[] }) { return value.errors?.length ? '；预检/导入问题：' + JSON.stringify(value.errors) : ''; }
export function ChatHistoryProvider({ children }: { children: ReactNode }) {
  const [archive] = useState(readArchive);
  const { legacy, warning: archiveWarning } = useMemo(() => inspectArchive(archive.originals), [archive]);
  const [server, setServer] = useState<Conversation[]>([]);
  const [origins, setOrigins] = useState<number[]>([]);
  const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [importing, setImporting] = useState(false); const [importStatus, setImportStatus] = useState('');
  const loadingSessions = useRef(new Map<string, { promise: Promise<void>; version: number }>());
  const mutationVersions = useRef(new Map<string, number>());
  const requestSequences = useRef(new Map<string, number>());
  const markMutation = (id: string) => mutationVersions.current.set(id, (mutationVersions.current.get(id) || 0) + 1);
  const put = useCallback((item: Conversation) => { markMutation(item.id); setServer(old => [item, ...old.filter(s => s.id !== item.id)]); }, []);
  const change = useCallback((id: string, fn: (item: Conversation) => Conversation) => { markMutation(id); setServer(old => old.map(item => item.id === id ? fn(item) : item)); }, []);
  const saveServer = useCallback((item: ServerSession) => { markMutation(item.id); const mapped = serverConversation(item); setServer(old => [mergeServerSession(old.find(s => s.id === item.id), item), ...old.filter(s => s.id !== item.id)]); return mapped; }, []);
  const reload = useCallback(async () => { setLoading(true); const versions = new Map(mutationVersions.current); try { let offset = 0; const all: ServerSession[] = []; while (true) { const page = await conversationApi<{ items: ServerSession[]; has_more?: boolean }>('/sessions?limit=100&offset=' + offset); all.push(...page.items); if (!page.has_more && page.items.length < 100) break; if (!page.items.length) break; offset += page.items.length; } setServer(old => [...old.filter(cached => !all.some(s => s.id === cached.id) && versions.get(cached.id) !== mutationVersions.current.get(cached.id)), ...all.map(s => { const cached = old.find(v => v.id === s.id); return cached && versions.get(s.id) !== mutationVersions.current.get(s.id) ? cached : mergeServerSession(cached, s); })]); setError(''); if (archive.originals.length) { try { const preview = await conversationApi<ImportPreview>('/history-import/preview', { method: 'POST', body: JSON.stringify({ sessions: archive.originals }) }); setOrigins(preview.sessions.flatMap(v => v.already_imported && v.session_id ? [v.index] : [])); if (preview.errors?.length) setError(importErrors(preview)); } catch (e) { setOrigins([]); setError(e instanceof Error ? e.message : '旧历史导入状态读取失败'); } } } catch (e) { setError(e instanceof Error ? e.message : '历史加载失败'); } finally { setLoading(false); } }, []);
  useEffect(() => { void reload(); }, [reload]);
  const loadSession = useCallback((id: string, older = false): Promise<void> => {
    if (id.startsWith('legacy:')) return Promise.resolve();
    const key = id + ':' + older; const running = loadingSessions.current.get(key);
    const version = mutationVersions.current.get(id) || 0;
    if (running?.version === version) return running.promise;
    const sequence = (requestSequences.current.get(key) || 0) + 1;
    requestSequences.current.set(key, sequence);
    const before = server.find(s => s.id === id)?.oldestSequence || 0;
    const job = (async () => { try {
      const item = await conversationApi<ServerSession>('/sessions/' + encodeURIComponent(id));
      const page = await conversationApi<{ items: ServerTurn[]; has_more?: boolean }>('/sessions/' + encodeURIComponent(id) + '/turns?limit=50' + (older ? '&before_seq=' + before : ''));
      const ordered = page.items.slice().sort((a,b) => a.sequence_no - b.sequence_no);
      setServer(old => {
        const previous = old.find(s => s.id === id);
        const changed = version !== (mutationVersions.current.get(id) || 0);
        const stale = changed || sequence !== requestSequences.current.get(key) || !!previous?.messages.some(m => m.streaming) || (previous?.revision || 0) > item.revision;
        // Older pages only add absent turns and may safely complete during a stream.
        if (!older && stale) return old;
        const incoming = turnMessages(ordered);
        const base = stale && previous ? previous : mergeServerSession(previous, item);
        const messages = older ? [...incoming.filter(m => !previous?.messages.some(n => n.turnId === m.turnId)), ...(previous?.messages || [])] : incoming;
        const oldestSequence = older ? Math.min(previous?.oldestSequence ?? Infinity, ordered[0]?.sequence_no ?? Infinity) : ordered[0]?.sequence_no ?? previous?.oldestSequence;
        const merged = { ...base, loaded: true, messages, oldestSequence: Number.isFinite(oldestSequence) ? oldestSequence : undefined,
          hasOlder: page.has_more ?? (ordered.length === 50 && (ordered[0]?.sequence_no || 0) > 1) };
        return [merged, ...old.filter(s => s.id !== id)];
      });
    } finally { if (requestSequences.current.get(key) === sequence) loadingSessions.current.delete(key); } })(); loadingSessions.current.set(key, { promise: job, version }); return job;
  }, [server]);
  const remove = useCallback(async (id: string) => { if (id.startsWith('legacy:')) return; try { await conversationApi('/sessions/' + encodeURIComponent(id), { method: 'DELETE' }); setServer(old => old.filter(item => item.id !== id)); } catch (e) { setError(e instanceof Error ? e.message : '归档失败'); throw e; } }, []);
  const importLegacy = useCallback(async () => { setImporting(true); setImportStatus('正在预检浏览器历史…'); try { const body = JSON.stringify({ sessions: archive.originals }); const preview = await conversationApi<ImportPreview>('/history-import/preview', { method: 'POST', body }); setImportStatus('预检完成，正在导入… ' + JSON.stringify(preview.counts || { importable: preview.importable_count, existing: preview.existing_count }) + importErrors(preview)); const result = await conversationApi<{ sessions: ServerSession[]; errors?: unknown[]; counts?: Record<string, number>; imported_count?: number; existing_count?: number }>('/history-import', { method: 'POST', body }); result.sessions.forEach(saveServer); setImportStatus('导入结果：' + JSON.stringify(result.counts || { imported: result.imported_count, existing: result.existing_count }) + importErrors(preview) + importErrors(result) + '；浏览器原始备份保留'); await reload(); } catch (e) { setImportStatus(e instanceof Error ? e.message : '导入失败，浏览器原始备份保留'); } finally { setImporting(false); } }, [archive, reload, saveServer]);
  const exportLegacy = useCallback(() => { try { const blob = new Blob([archive.raw || '[]'], { type: 'application/json' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'rag-browser-history-original.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); setImportStatus('已导出浏览器原始 JSON 备份'); } catch (e) { setImportStatus(e instanceof Error ? e.message : '导出失败'); } }, [archive]);
  const sessions = useMemo(() => [...server, ...legacy.filter(s => !origins.includes(s.legacyIndex!))], [server, legacy, origins]);
  return <Context.Provider value={{ sessions, put, change, remove, storageError: archive.error, error: [archiveWarning, error].filter(Boolean).join('；'), loading, reload, loadSession, saveServer, importLegacy, exportLegacy, importing, importStatus, legacyCount: archive.originals.length }}>{children}</Context.Provider>;
}
export function useChatHistory() { const value = useContext(Context); if (!value) throw new Error('ChatHistoryProvider is missing'); return value; }
