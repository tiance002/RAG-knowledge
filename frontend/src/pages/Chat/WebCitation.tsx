import type { Citation } from '@/types';

/** Only absolute HTTP(S) URLs may leave the app from web evidence. */
export function safeWebUrl(value?: string): string | undefined {
  if (!value || !/^https?:\/\//i.test(value.trim())) return undefined;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return undefined;
    return url.href;
  } catch {
    return undefined;
  }
}

export default function WebCitation({ citation }: { citation: Citation }) {
  const url = safeWebUrl(citation.url);
  const title = citation.title || citation.url || '网页来源';
  return (
    <div>
      <span>网页来源 · </span>
      {url ? <a href={url} target="_blank" rel="noopener noreferrer">{title}</a> : <span>{title}（链接无效）</span>}
      {url && <div style={{ overflowWrap: 'anywhere' }}>{url}</div>}
      {citation.content && <p style={{ whiteSpace: 'pre-wrap' }}>{citation.content}</p>}
    </div>
  );
}
