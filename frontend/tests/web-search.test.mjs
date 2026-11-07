import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL('../src/pages/Chat/WebCitation.tsx', import.meta.url))],
  bundle: true, write: false, format: 'esm', jsx: 'automatic',
});
const { default: WebCitation, safeWebUrl } = await import('data:text/javascript;base64,' + Buffer.from(outputFiles[0].text).toString('base64'));

test('web evidence accepts only absolute HTTP(S) URLs without credentials', () => {
  assert.equal(safeWebUrl('https://example.com/source?q=1'), 'https://example.com/source?q=1');
  assert.equal(safeWebUrl('http://example.com'), 'http://example.com/');
  for (const url of ['javascript:alert(1)', 'data:text/html,x', '/kb/a/preview/b', '//example.com', 'file:///tmp/a', 'https://user:pass@example.com', 'https://', undefined]) {
    assert.equal(safeWebUrl(url), undefined, String(url));
  }
});

test('web citation renders server title/URL and snippet without local provenance', () => {
  const html = renderToStaticMarkup(createElement(WebCitation, { citation: {
    source_type: 'web', title: 'Actual web title', url: 'https://example.com/source', content: '<script>unsafe</script>',
  } }));
  assert.match(html, /Actual web title/);
  assert.match(html, /href="https:\/\/example.com\/source"/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(html, /未知知识库|未知文件|未知位置|相似度|\/preview\/|<script>/);
});

test('unsafe web URL is inert and never becomes a file preview', () => {
  const html = renderToStaticMarkup(createElement(WebCitation, { citation: { source_type: 'web', title: 'Blocked', url: 'javascript:alert(1)' } }));
  assert.match(html, /链接无效/);
  assert.doesNotMatch(html, /href=|\/preview\//);
});

test('Chat opt-in is false per mode, forwarded to the request, with no KB requirement', async () => {
  const source = await readFile(new URL('../src/pages/Chat/index.tsx', import.meta.url), 'utf8');
  assert.equal((source.match(/webSearchEnabled: false/g) || []).length, 2);
  assert.match(source, /web_search_enabled: currentConfig\.webSearchEnabled/);
  assert.match(source, /kb_ids: currentConfig\.kbIds/);
  const sendGuard = source.slice(source.indexOf('const handleSend ='), source.indexOf('// 发送新消息时'));
  assert.doesNotMatch(sendGuard, /kbIds/);
  assert.match(source, /aria-label="联网搜索"/);
  assert.match(source, /外部搜索服务 Tavily/);
  assert.match(source, /不运行 Agent 工具循环/);
  assert.match(source, /citation\.source_type === 'web' \?/);
});

test('stream adapter sends explicit opt-in and empty KB scope unchanged', async t => {
  const { outputFiles } = await build({
    entryPoints: [fileURLToPath(new URL('../src/services/chat.ts', import.meta.url))],
    bundle: true, write: false, format: 'esm', plugins: [{
      name: 'rewrite-client-stub', setup(b) {
        b.onResolve({ filter: /^\.\/request$/ }, () => ({ path: 'request', namespace: 'stub' }));
        b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export default {}' }));
      },
    }],
  });
  const { createChatStream } = await import('data:text/javascript;base64,' + Buffer.from(outputFiles[0].text).toString('base64'));
  for (const enabled of [false, true]) {
    let sent;
    t.mock.method(globalThis, 'fetch', async (_url, options) => {
      sent = JSON.parse(options.body);
      return new Response('event: done\ndata: {}\n\n');
    });
    await new Promise((resolve, reject) => createChatStream({ messages: [{role: 'user', content: 'test'}], kb_ids: [], stream: true, web_search_enabled: enabled }, {onDone: resolve, onError: reject}));
    assert.equal(sent.web_search_enabled, enabled);
    assert.deepEqual(sent.kb_ids, []);
  }
});
