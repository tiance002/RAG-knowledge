import { useEffect, useState } from 'react';
import './TaskMonitor.css';

type Receipt = { task_id: string; file_id: string; file_name: string; status: string; stage: string;
  attempt_no: number; heartbeat_at: number | null; next_run_at: number; error_type?: string; error_message?: string;
  active_build_id?: string };
type Queue = { waiting: number; running: number; failed: number; unknown: number; oldest_wait_seconds: number | null };
type View = { queues: Record<string, Queue>; tasks: Receipt[]; has_more: boolean };
type Detail = { task_id: string;
  attempts: { attempt_no: number; stage: string; outcome: string; error: string | null }[];
  effects: { effect_key: string; stage: string; state: string; provider_request_id: string | null; stored_result: boolean }[];
  decisions: { created_at: number; action: string; reason: string }[] };
const states: Record<string, string> = { QUEUED: '排队中', RUNNING: '执行中', RETRY_WAIT: '等待重试',
  UNKNOWN: '待核对', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消', PUBLISH_UNKNOWN: '等待补投' };
const stages: Record<string, string> = { parse: '解析', embed: '生成向量', index: '发布索引' };
const time = (ms: number | null) => ms ? new Date(ms).toLocaleString('zh-CN') : '—';

export default function TaskMonitor({ kbId }: { kbId: string }) {
  const [view, setView] = useState<View>();
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState('');
  const [reason, setReason] = useState('');
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
  const resolve = async (row: Receipt, action: string) => {
    setBusy(true); setError('');
    try {
      const res = await fetch(`/api/v1/files/${encodeURIComponent(row.file_id)}/tasks/${encodeURIComponent(row.task_id)}/resolve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, reason }) });
      if (!res.ok) throw new Error((await res.json()).detail || '任务处理失败');
      setEditing(''); setReason(''); setDetail(undefined); setRevision(r => r + 1);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <section className="file-task-monitor" aria-label="文件处理任务">
    {error && <p role="alert">{error}</p>}
    {view && Object.entries(view.queues).map(([name, q]) => <p key={name}>
      <strong>{name === 'document.index' ? '索引发布' : '解析与向量计算'}</strong>
      {'　'}等待 {q.waiting} · 执行 {q.running} · 失败 {q.failed} · 待核对 {q.unknown}
      {q.oldest_wait_seconds != null && ` · 最久等待 ${q.oldest_wait_seconds} 秒`}
    </p>)}
    <div className="task-table-scroll"><table><thead><tr>
      {['文件与任务', '状态与阶段', '执行次数', '最近心跳', '下次重试', '生效构建'].map(s => <th key={s}>{s}</th>)}
    </tr></thead><tbody>{view?.tasks.map(r => <tr key={r.task_id}>
      <td>{r.file_name}<small>{r.task_id}</small><button onClick={() => void showDetail(r)}>执行记录</button></td><td>{states[r.status] || r.status} · {stages[r.stage] || r.stage}
        {(r.error_message || r.error_type) && <small>{r.error_message || r.error_type}</small>}
        {r.status === 'UNKNOWN' && <button onClick={() => { setEditing(r.task_id); setReason(''); }}>处理</button>}
        {editing === r.task_id && r.status === 'UNKNOWN' && <div><label>核对依据<input value={reason} maxLength={1000}
          onChange={e => setReason(e.target.value)} /></label>
          {[['resume', '使用已核实结果'], ['retry', '授权重试（可能重复计费）'], ['terminate', '终止并释放文件']].map(([a, title]) =>
            <button key={a} disabled={busy || !reason.trim()} onClick={() => void resolve(r, a)}>{title}</button>)}</div>}
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
