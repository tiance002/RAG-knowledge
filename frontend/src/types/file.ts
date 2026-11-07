/**
 * 文件相关类型定义
 */

/** 文件处理状态 */
export type FileStatus =
  | 'pending'          // 待解析
  | 'parsing'          // 解析中
  | 'pending_confirm'  // 待确认
  | 'parsed'           // 解析完成
  | 'embedding'        // 向量化中
  | 'ready'            // 已入库
  | 'failed';          // 失败

/** 文件信息 */
export interface FileInfo {
  id: string;
  name: string;
  original_name?: string;
  display_name?: string;
  sha256?: string | null;
  supersedes_file_id?: string | null;
  size: number;
  type: string;
  status: FileStatus;
  progress: number;
  progressMessage?: string;
  chunkCount?: number;
  chunk_count?: number;
  createdAt: string;
  created_at?: string;
  updatedAt: string;
  errorMessage?: string;
  error_message?: string | null;
}

/** 文件上传响应 */
export interface FileUploadResponse {
  file_id: string;
  status: string;
  outcome: 'created' | 'duplicate' | 'replacement_staged';
  duplicate: boolean;
  display_name?: string;
  name_conflict: boolean;
}

/** Chunk 内容类型 */
export type ChunkContentType = 'text' | 'image' | 'table' | 'mixed';

/** Chunk 信息 */
export interface ChunkInfo {
  id: string;
  fileId: string;
  fileName: string;
  pageNumber?: number;
  rowNumber?: number;
  contentType: ChunkContentType;
  content: string;
  imageUrl?: string;
  vlmDescription?: string;
  tokenCount?: number;
  token_count?: number;
  parentId?: string | null;
  headingLevel?: number | null;
  headingText?: string | null;
  chunkType?: string;
  metadata?: Record<string, unknown>;
}

/** Chunk 更新请求 */
export interface UpdateChunkRequest {
  content: string;
}

/** Chunk 列表响应 */
export interface ChunkListResponse {
  items: ChunkInfo[];
  total: number;
}
