import { useEffect, useState } from 'react';
import './TaskMonitor.css';

type Receipt = { task_id: string; file_id: string; file_name: string; status: string; stage: string;
  attempt_no: number; heartbeat_at: number | null; next_run_at: number; error_type?: string; error_message?: string;
  active_build_id?: string; target_generation?: number | null; resolution_available?: boolean };
type Queue = { waiting: number; running: number; failed: number; oldest_wait_seconds: number | null };
type View = { queues: Record<string, Queue>; tasks: Receipt[]; has_more: boolean };
type Detail = { task_id: string;
  attempts: { attempt_no: number; stage: string; outcome: string; error: string | null }[];
  effects: { effect_key: string; stage: string; state: string; provider_request_id: string | null; stored_result: boolean }[];
  decisions: { created_at: number; action: string; reason: string }[] };
const states: Record<string, string> = { QUEUED: '排队中', RUNNING: '执行中', RETRY_WAIT: '等待重试',
  COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消' };
const stages: Record<string, string> = { parse: '解析', embed: '生成向量', index: '发布索引' };
const time = (ms: number | null) => ms ? new Date(ms).toLocaleString('zh-CN') : '—';

export default function TaskMonitor({ kbId }: { kbId: string }) {
  const [view, setView] = useState<View>();
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Detail>();
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const res = await fetch(`/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/tasks?offset=${offset}`, { signal: controller.signal });
        if (!res.ok) throw new Error(`任务列表加载失败 (${res.status})`);
        const body = await res.json();
        if (!controller.signal.aborted) setView(body.data);
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(refresh, 5000);
      }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [kbId, offset, revision]);
  const showDetail = async (r: Receipt) => {
    try {
      const res = await fetch(`/api/v1/files/${encodeURIComponent(r.file_id)}/tasks/${encodeURIComponent(r.task_id)}`);
      if (!res.ok) throw new Error('任务记录加载失败');
      setDetail((await res.json()).data);
    } catch (e) { setError(String(e)); }
  };
  const resolve = async (row: Receipt, action: 'retry' | 'terminate') => {
    if (busy) return;
    if (action === 'retry' && !window.confirm('上一次模型请求可能已执行。重新处理可能产生额外的模型费用，是否继续？')) return;
    const reason = action === 'retry' ? '用户确认重新处理，接受可能的额外模型费用' : '用户取消未完成的处理';
    setBusy(true); setError('');
    try {
      const res = await fetch(`/api/v1/files/${encodeURIComponent(row.file_id)}/tasks/${encodeURIComponent(row.task_id)}/resolve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, reason }) });
      if (res.status === 409) {
        setRevision(r => r + 1);
        throw new Error('任务状态已变化，请刷新后再试。');
      }
      if (!res.ok) throw new Error('任务处理失败，请稍后再试。');
      setDetail(undefined); setRevision(r => r + 1);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <section className="file-task-monitor" aria-label="文件处理任务">
    {error && <p role="alert">{error}</p>}
    {view && Object.entries(view.queues).map(([name, q]) => <p key={name}>
      <strong>{name === 'document.index' ? '索引发布' : '解析与向量计算'}</strong>
      {'　'}等待 {q.waiting} · 执行 {q.running} · 失败 {q.failed}
      {q.oldest_wait_seconds != null && ` · 最久等待 ${q.oldest_wait_seconds} 秒`}
    </p>)}
    <div className="task-table-scroll"><table><thead><tr>
      {['文件与任务', '状态与阶段', '执行次数', '最近心跳', '下次重试', '生效构建'].map(s => <th key={s}>{s}</th>)}
    </tr></thead><tbody>{view?.tasks.map(r => <tr key={r.task_id}>
      <td>{r.file_name}<small>{r.task_id}</small><button onClick={() => void showDetail(r)}>执行记录</button></td><td>{states[r.status] || r.status} · {stages[r.stage] || r.stage}
        {(r.error_message || r.error_type) && <small>{r.error_message || r.error_type}</small>}
        {r.resolution_available && <div>
          <button disabled={busy} onClick={() => void resolve(r, 'terminate')}>取消处理</button>
          {r.target_generation != null && <button disabled={busy} onClick={() => void resolve(r, 'retry')}>重新处理</button>}
        </div>}
      </td><td>{r.attempt_no}</td><td>{time(r.heartbeat_at)}</td><td>{r.status === 'RETRY_WAIT' ? time(r.next_run_at) : '—'}</td>
      <td>{r.active_build_id || '—'}</td></tr>)}</tbody></table></div>
    {!view ? <p>正在读取任务…</p> : !view.tasks.length && <p>暂无任务</p>}
    {detail && <section aria-label="执行记录"><h3>任务 {detail.task_id}</h3>
      {detail.attempts.map(a => <p key={a.attempt_no}>第 {a.attempt_no} 次 · {stages[a.stage] || a.stage} · {a.outcome} {a.error}</p>)}
      {detail.effects.map(e => <p key={e.effect_key}>{stages[e.stage] || e.stage} · {e.state} ·
        {e.stored_result ? ' 已保存结果' : ' 尚无已保存结果'}{e.provider_request_id && ` · 供应商请求 ${e.provider_request_id}`}</p>)}
      {detail.decisions.map(d => <p key={d.created_at}>{time(d.created_at)} · {d.action} · {d.reason}</p>)}
      <button onClick={() => setDetail(undefined)}>收起记录</button></section>}
    {(offset > 0 || view?.has_more) && <nav><button disabled={offset === 0} onClick={() => setOffset(offset - 50)}>上一页</button>
      <button disabled={!view?.has_more} onClick={() => setOffset(offset + 50)}>下一页</button></nav>}
  </section>;
}
