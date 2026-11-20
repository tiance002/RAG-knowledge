// Reuse one key for concurrent submissions and ambiguous network failures.
const pending = new Map<string, { key: string; request?: Promise<unknown> }>();

export function submitOnce<T>(scope: string, input: unknown, send: (key: string) => Promise<T>): Promise<T> {
  const id = JSON.stringify([scope, input]);
  const active = pending.get(id);
  if (active?.request) return active.request as Promise<T>;
  const key = active?.key ?? Array.from(crypto.getRandomValues(new Uint8Array(16)),
    n => n.toString(16).padStart(2, '0')).join('');
  const request = send(key).then(result => {
    pending.delete(id);
    return result;
  }, error => {
    const status = error?.response?.status ?? error?.status;
    if (status >= 400 && status < 500) pending.delete(id);
    else pending.set(id, { key });
    throw error;
  });
  pending.set(id, { key, request });
  return request;
}
