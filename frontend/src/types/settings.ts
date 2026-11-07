/**
 * 系统设置相关类型定义
 */

/** 模型信息 */
export interface ModelInfo {
  id: string;
  name: string;
  type: 'embedding' | 'vlm' | 'llm' | 'rerank';
  provider: string;
  description?: string;
}

/** 系统设置 */
export interface SystemSettings {
  defaultEmbeddingModel: string | null;
  defaultVlmModel: string | null;
  defaultLlmModel: string | null;
  revision: number;
  degraded?: boolean;
  configError?: string | null;
  restartRequired?: string[];
  maxConcurrency: number;
  chunkSize: number;
  chunkOverlap: number;
  maxChatHistoryRounds: number;
}

/** 设置更新请求 */
export interface UpdateSettingsRequest {
  expected_revision: number;
  default_embedding_model?: string | null;
  default_vlm_model?: string | null;
  default_llm_model?: string | null;
  max_concurrency?: number;
  chunk_size?: number;
  chunk_overlap?: number;
  max_chat_history_rounds?: number;
}

/** 自定义模型 */
export interface CustomModel {
  id: string;
  name: string;
  model_type: 'llm' | 'embedding' | 'vlm' | 'rerank';
  base_url: string;
  api_key: string;
  model_name: string;
  context_length?: number;
  is_active: boolean;
  created_at: string;
}

/** 创建自定义模型请求 */
export interface CustomModelCreate {
  name: string;
  model_type: 'llm' | 'embedding' | 'vlm' | 'rerank';
  base_url: string;
  api_key: string;
  model_name: string;
  context_length?: number;
}

/** 更新自定义模型请求 */
export interface CustomModelUpdate {
  name?: string;
  model_type?: 'llm' | 'embedding' | 'vlm' | 'rerank';
  base_url?: string;
  api_key?: string;
  model_name?: string;
  context_length?: number;
}
