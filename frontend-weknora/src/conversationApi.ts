export type ServerSession = { id: string; title: string; scope_mode: string; kb_ids: string[]; config_overrides: Record<string, unknown>; revision: number; active_turn_id?: string | null; updated_at?: string; legacy_origin?: unknown };
export type ServerTurn = { id: string; sequence_no: number; user_content: string; assistant_content: string; status: string; generation_status?: string; citations?: Record<string, unknown>[] };
export async function conversationApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1/chat' + path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!response.ok) { let detail = ''; try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body); } catch { /* HTTP code remains available */ } throw new Error(`会话请求失败（HTTP ${response.status}）${detail ? '：' + detail : ''}`); }
  return response.status === 204 ? undefined as T : response.json();
}
export function normalizeCitations(items: Record<string, unknown>[] = []) {
  return items.map(c => ({ ...c, chunkId: String(c.chunkId || c.chunk_id || c.id || ''), fileName: String(c.fileName || c.file_name || c.source_name || ''), kbName: String(c.kbName || c.kb_name || ''), content: String(c.content || c.text || ''), location: String(c.location || c.page_label || '') }));
}
