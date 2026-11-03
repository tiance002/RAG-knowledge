/**
 * 对话相关 API
 */

import request from './request';
import type {
  RewriteRequest,
  RewriteResponse,
  ChatRequest,
  AgentThoughtData,
  RagResultData,
  AnswerChunkData,
  DoneData,
  ApiResponse,
} from '@/types';

/** 问题改写 */
export const rewriteQuery = (data: RewriteRequest): Promise<ApiResponse<RewriteResponse>> => {
  return request.post('/chat/rewrite', data);
};

/** SSE 事件处理器类型 */
export interface SSEEventHandlers {
  onAgentThought?: (data: AgentThoughtData) => void;
  onRagResult?: (data: RagResultData) => void;
  onAnswerChunk?: (data: AnswerChunkData) => void;
  onDone?: (data: DoneData) => void;
  onError?: (error: Error) => void;
}

type StreamFailure = 'FAILED' | 'TRUNCATED' | 'UNKNOWN' | 'INTERRUPTED' | 'CANCELLED';

export class ChatStreamError extends Error {
  constructor(public readonly status: StreamFailure, message: string) {
    super(message);
  }
}

/** Each server response has exactly one terminal outcome, including unexpected EOF. */
export const createChatStream = (
  data: ChatRequest, handlers: SSEEventHandlers
): { abort: () => void } => {
  const controller = new AbortController();
  const started = performance.now();
  const requestId = globalThis.crypto?.randomUUID?.() ?? `chat-${Date.now()}-${++requestSequence}`;
  let terminal = false;
  const finish = (event: 'error' | 'done', status: string) => {
    terminal = true;
    console.debug('chat.stream', {requestId, event, status, elapsedMs: Math.round(performance.now() - started)});
  };
  const fail = (error: ChatStreamError) => {
    if (terminal) return;
    finish('error', error.status);
    handlers.onError?.(error);
  };

  async function receive() {
    const response = await fetch('/api/v1/chat/completions', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Request-ID': requestId},
      body: JSON.stringify(data), signal: controller.signal,
    });
    if (!response.ok) throw new ChatStreamError('FAILED', `请求失败（HTTP ${response.status}）`);
    const reader = response.body?.getReader();
    if (!reader) throw new ChatStreamError('INTERRUPTED', '响应没有可读取的数据流。');
    const decoder = new TextDecoder();
    let buffer = '';
    let eventType = '';
    try {
      while (!terminal) {
        const {done, value} = await reader.read();
        if (done) throw new ChatStreamError('INTERRUPTED', '连接中断，未收到服务端结束状态。');
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const raw of lines) {
          const line = raw.trim();
          if (line.startsWith('event:')) {
            eventType = line.slice(6).trim();
            continue;
          }
          if (!line.startsWith('data:')) continue;
          const payload = JSON.parse(line.slice(5));
          if (!payload || typeof payload !== 'object') throw new Error('invalid event');
          const type = eventType || ('error' in payload ? 'error' : 'step' in payload ? 'agent_thought' :
            'citations' in payload ? 'rag_result' : 'content' in payload ? 'answer_chunk' : 'usage' in payload ? 'done' : '');
          eventType = '';
          if (type === 'error' || type === 'done') {
            const state = payload.status ?? payload.generation_status;
            if (state === 'TRUNCATED') throw new ChatStreamError('TRUNCATED', '模型输出达到上限，回答未完成。');
            if (state === 'UNKNOWN') throw new ChatStreamError('UNKNOWN', '请求结果未知。');
            if (state === 'CANCELLED') throw new ChatStreamError('CANCELLED', '请求已取消。');
            if (type === 'error' || state === 'FAILED') throw new ChatStreamError('FAILED', payload.error || '请求失败。');
            finish('done', 'COMPLETED');
            handlers.onDone?.(payload);
            return;
          }
          if (type === 'agent_thought' || type === 'thought') handlers.onAgentThought?.(payload);
          if (type === 'rag_result' || type === 'rag') handlers.onRagResult?.(payload);
          if (type === 'answer_chunk' || type === 'answer') handlers.onAnswerChunk?.(payload);
        }
      }
    } finally {
      void reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  void receive().catch(error => fail(error instanceof ChatStreamError ? error :
    new ChatStreamError('INTERRUPTED', '响应读取中断，未确认完成。')));
  return {
    abort: () => {
      if (terminal) return;
      controller.abort();
      fail(new ChatStreamError('CANCELLED', '请求已取消。'));
    },
  };
};

let requestSequence = 0;
