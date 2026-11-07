import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

export type Citation = { chunkId?: string; fileName?: string; kbName?: string; content?: string; location?: string };
export type Message = { role: 'user' | 'assistant'; content: string; citations?: Citation[]; status?: string; steps?: string[]; streaming?: boolean };
export type Conversation = { id: string; title: string; updatedAt: number; model: string; kbIds: string[]; mode: 'normal' | 'agent'; messages: Message[] };
type ChatHistory = { sessions: Conversation[]; put: (session: Conversation) => void; change: (id: string, change: (item: Conversation) => Conversation) => void; remove: (id: string) => void; storageError: boolean };
const KEY = 'rag-workspace.chat-history.v1';
const Context = createContext<ChatHistory | null>(null);

function load(): Conversation[] {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || '[]');
    return Array.isArray(value) ? value.filter(item => item && typeof item.id === 'string' && Array.isArray(item.messages)).map(item => ({ ...item, messages: item.messages.map((message: Message) => message.streaming ? { ...message, streaming: false, status: '生成中断，请重新提问' } : message) })) : [];
  } catch { return []; }
}
export function ChatHistoryProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<Conversation[]>(load);
  const [storageError, setStorageError] = useState(false);
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(sessions)); setStorageError(false); } catch { setStorageError(true); } }, [sessions]);
  useEffect(() => {
    const sync = (event: StorageEvent) => { if (event.key === KEY) setSessions(load()); };
    window.addEventListener('storage', sync); return () => window.removeEventListener('storage', sync);
  }, []);
  const put = useCallback((item: Conversation) => setSessions(old => [item, ...old.filter(s => s.id !== item.id)]), []);
  const change = useCallback((id: string, fn: (item: Conversation) => Conversation) => setSessions(old => old.map(item => item.id === id ? fn(item) : item)), []);
  const remove = useCallback((id: string) => setSessions(old => old.filter(item => item.id !== id)), []);
  const value = useMemo(() => ({ sessions, put, change, remove, storageError }), [sessions, put, change, remove, storageError]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useChatHistory() { const value = useContext(Context); if (!value) throw new Error('ChatHistoryProvider is missing'); return value; }
