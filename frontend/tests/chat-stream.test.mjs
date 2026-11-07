import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

// Exercise the actual fetch/stream adapter. Only the unrelated rewrite client is stubbed.
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
const secret = 'private-marker-739';
const event = (type, payload) => `event: ${type}\r\ndata: ${JSON.stringify(payload)}\r\n\r\n`;
const prefix = event('answer_chunk', {content: secret});
for (const [title, body, expected] of [
  ['error then EOF', prefix + event('error', {error: secret}), ['partial', 'FAILED']],
  ['done then EOF', prefix + event('done', {}), ['partial', 'done']],
  ['abnormal EOF', prefix, ['partial', 'INTERRUPTED']],
  ['two terminals', event('done', {}) + event('error', {}), ['done']],
  ['truncated error', prefix + event('error', {status: 'TRUNCATED'}), ['partial', 'TRUNCATED']],
  ['truncated done', prefix + event('done', {generation_status: 'TRUNCATED'}), ['partial', 'TRUNCATED']],
  ['provider unknown', event('error', {status: 'UNKNOWN'}), ['UNKNOWN']],
  ['malformed event', 'event: done\ndata: {bad}\n\n', ['INTERRUPTED']],
]) {
  test(title, async t => {
    t.mock.method(globalThis, 'fetch', async () => new Response(body));
    const logs = [];
    for (const level of ['log', 'error', 'warn', 'debug']) t.mock.method(console, level, (...args) => logs.push(JSON.stringify(args)));
    const events = [];
    const finished = Promise.withResolvers();
    createChatStream({messages: [{role: 'user', content: secret}], kb_ids: [], stream: true}, {
      onAnswerChunk: data => { assert.equal(data.content, secret); events.push('partial'); },
      onError: err => { events.push(err.status || 'FAILED'); finished.resolve(); },
      onDone: () => { events.push('done'); finished.resolve(); },
    });
    await finished.promise;
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(events, expected);
    assert.ok(logs.every(line => !line.includes(secret)), 'private marker in diagnostics');
  });
}
