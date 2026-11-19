from app.services.observability import event, observe
"""
文件解析服务
支持 PDF, Word, Excel, CSV, TXT 格式
"""

import os
import fitz  # PyMuPDF
from typing import List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
from docx import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.config import get_settings
from app.services.embedding import EmbeddingService
from app.services.task_effect_context import raise_if_effect_error
import asyncio

try:
    from langchain_experimental.text_splitter import SemanticChunker
except ImportError:
    SemanticChunker = None

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

settings = get_settings()

def run_async(coro):
    """在同步环境中运行异步函数"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        # 如果已经在循环中 (虽然 parser 通常在 celery 线程，不应在 loop 中)，
        # 但为了防止意外，这里做一个简单的 fallback 或者报错
        # 对于 Celery worker，通常是没有运行 loop 的
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    else:
        return loop.run_until_complete(coro)

class EmbeddingServiceAdapter:
    """LangChain Embedding 适配器 (使用 EmbeddingService)"""
    def __init__(self, base_url: str = None, api_key: str = None, model: str = None):
        self.service = EmbeddingService(base_url=base_url, api_key=api_key, model=model)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # 同步调用异步方法
        return run_async(self.service.embed_documents(texts, model_id=self.service.model))

    def embed_query(self, text: str) -> List[float]:
        # 同步调用异步方法
        return run_async(self.service.embed_query(text, model_id=self.service.model))


@dataclass
class ParsedChunk:
    """解析后的块"""
    content: str
    page_number: Optional[int]
    content_type: str  # text, table, image
    image_path: Optional[str] = None
    metadata: Optional[dict] = None
    token_count: int = 0


class TextSplitter:
    """
    文本切分器

    优化：语义完整性切分
    - 按字数初步分块
    - 检查块结尾是否是完整语义（完整句子）
    - 如果不完整，向后扩展到最近的句子边界
    - 允许块大小超出 chunk_size 最多 20%
    """

    # 语义边界定义（优先级从高到低）
    SENTENCE_BOUNDARIES_CN = ['。', '？', '！', '；']
    SENTENCE_BOUNDARIES_EN = ['. ', '? ', '! ', '; ']
    PARAGRAPH_BOUNDARY = '\n\n'

    def __init__(
        self,
        chunk_size: int = None,
        chunk_overlap: int = None,
        separator: str = "\n\n",
        max_overflow_ratio: float = 0.2,  # 允许超出的最大比例
    ):
        if chunk_size is None or chunk_overlap is None:
            from app.services.runtime_settings import get_runtime_settings
            defaults = get_runtime_settings()
            chunk_size = defaults.chunk_size if chunk_size is None else chunk_size
            chunk_overlap = defaults.chunk_overlap if chunk_overlap is None else chunk_overlap
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        if self.chunk_size <= 0 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("CHUNK_CONFIGURATION_INVALID")
        self.separator = separator.replace("\\n", "\n") if separator else "\n\n"
        self.max_overflow_ratio = max_overflow_ratio
        self.max_chunk_size = int(self.chunk_size * (1 + max_overflow_ratio))

        # 初始化 LangChain 的 Token Splitter (如果可用)
        self.langchain_splitter = None
        if HAS_LANGCHAIN:
            try:
                # 使用 tiktoken 计算长度，但仍按字符递归切分以保持语义
                # 指定 cl100k_base (GPT-4/Embedding) 编码器，对中文支持更好且更通用
                # 注意：Qwen 和 DeepSeek 的词表对中文压缩率更高（Token 数更少）
                # 使用 cl100k_base 估算长度是安全的（Safe Upper Bound），不会导致上下文溢出
                self.langchain_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
                    encoding_name="cl100k_base",
                    chunk_size=self.chunk_size,
                    chunk_overlap=self.chunk_overlap,
                    separators=[
                        "\n\n",
                        "\n",
                        "。", "！", "？", "；", # 中文标点
                        ". ", "! ", "? ", "; ", # 英文标点
                        " ",
                        ""
                    ]
                )
                print(f"🔧 [TextSplitter] 初始化 LangChain Token-based Splitter (chunk_size={self.chunk_size} tokens, 编码器=cl100k_base, 兼容 GPT-4/Qwen/DeepSeek)", flush=True)
            except Exception as e:
                raise_if_effect_error(e)
                print(f"⚠️ [TextSplitter] 初始化 LangChain Splitter 失败: {e}，回退到基于字符的切分", flush=True)

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        splitter = getattr(self, 'langchain_splitter', None)
        if splitter is not None:
            length_fn = getattr(splitter, '_length_function', None)
            if callable(length_fn):
                try:
                    return length_fn(text)
                except Exception:
                    pass
            tokenizer = getattr(splitter, '_tokenizer', None)
            if tokenizer is not None:
                try:
                    return len(tokenizer.encode(text))
                except Exception:
                    pass
        return len(text)

    def _find_sentence_boundary(self, text: str, start: int, end: int, forward: bool = True) -> int:

        """
        在指定范围内查找句子边界

        Args:
            text: 文本
            start: 搜索起始位置
            end: 搜索结束位置
            forward: True 向后查找，False 向前查找

        Returns:
            找到的边界位置（包含边界符号），如果没找到返回 -1
        """
        if start >= end:
            return -1

        search_text = text[start:end]

        # 所有可能的边界符号
        boundaries = [self.PARAGRAPH_BOUNDARY] + self.SENTENCE_BOUNDARIES_CN + self.SENTENCE_BOUNDARIES_EN

        if forward:
            # 向后查找：找最近的边界
            best_pos = -1
            for boundary in boundaries:
                pos = search_text.find(boundary)
                if pos != -1:
                    actual_pos = start + pos + len(boundary)
                    if best_pos == -1 or actual_pos < best_pos:
                        best_pos = actual_pos
            return best_pos
        else:
            # 向前查找：找最后一个边界
            best_pos = -1
            for boundary in boundaries:
                pos = search_text.rfind(boundary)
                if pos != -1:
                    actual_pos = start + pos + len(boundary)
                    if actual_pos > best_pos:
                        best_pos = actual_pos
            return best_pos

    def split(self, text: str) -> List[str]:
        """
        切分文本

        如果 HAS_LANGCHAIN 为 True，则优先使用基于 Token 的 RecursiveCharacterTextSplitter。
        否则使用基于字符的语义完整性切分。
        """
        if not text:
            return []

        # 优先使用 LangChain 的 Token Splitter
        if self.langchain_splitter:
            try:
                # RecursiveCharacterTextSplitter.split_text 返回 List[str]
                return self.langchain_splitter.split_text(text)
            except Exception as e:
                raise_if_effect_error(e)
                print(f"❌ [TextSplitter] LangChain 切分失败: {e}，回退到规则切分", flush=True)

        # 回退到基于字符的切分逻辑
        """
        算法流程：
        1. 按 chunk_size 初步定位切分点 end
        2. 在 [end, end + chunk_size * 0.2] 范围内查找最近的句子边界（向后扩展）
        3. 如果找到，扩展到该边界
        4. 如果找不到，在 [end - chunk_size * 0.3, end] 范围内向前查找
        5. 仍找不到则强制切分
        """
        if not text:
            return []

        text = text.strip()
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        text_len = len(text)

        while start < text_len:
            # 如果剩余长度小于块大小，直接作为一个块
            remaining = text_len - start
            if remaining <= self.chunk_size:
                chunk = text[start:].strip()
                if chunk:
                    chunks.append(chunk)
                break

            # 初步切分点
            end = start + self.chunk_size
            best_end = end

            # 步骤 2: 向后扩展查找边界 [end, end + chunk_size * 0.2]
            # 注意：如果 max_overflow_ratio 为 0（例如二次切分模式），则 forward_search_end = end，不会进行向后查找
            forward_search_end = min(end + int(self.chunk_size * self.max_overflow_ratio), text_len)

            forward_boundary = -1
            if forward_search_end > end:
                 forward_boundary = self._find_sentence_boundary(text, end, forward_search_end, forward=True)

            if forward_boundary != -1:
                best_end = forward_boundary
            else:
                # 步骤 4: 向前查找边界 [end - chunk_size * 0.3, end]
                backward_search_start = max(start + int(self.chunk_size * 0.5), start)
                backward_boundary = self._find_sentence_boundary(text, backward_search_start, end, forward=False)

                if backward_boundary != -1 and backward_boundary > start:
                    best_end = backward_boundary
                # 否则强制切分（保持 best_end = end）

            # 提取 chunk
            chunk = text[start:best_end].strip()
            if chunk:
                chunks.append(chunk)

            # 下一块的起始位置，考虑重叠
            next_start = best_end - self.chunk_overlap

            # 防止死循环：确保至少前进一步
            if next_start <= start:
                next_start = best_end

            start = next_start

        return chunks


@dataclass
class HeadingInfo:
    """标题信息"""
    level: int  # 1, 2, 3
    text: str   # 标题文本
    start_pos: int  # 在原文中的起始位置
    end_pos: int    # 在原文中的结束位置（标题行结束）


@dataclass
class StructuredChunk:
    """结构化分块结果"""
    content: str
    heading_level: Optional[int]
    heading_text: Optional[str]
    heading_path: List[str]  # 标题层级路径
    parent_id: Optional[str]  # 父 chunk ID
    chunk_type: str  # "heading" | "content"
    token_count: int = 0


class StructuredSplitter:
    """
    结构化分块器

    按文档标题层级拆分，支持：
    - PDF（通过字体大小识别）
    - Word（Heading 样式）
    - TXT（Markdown # 语法）
    """

    def __init__(
        self,
        heading_level: int = 2,  # 按哪个级别拆分
        chunk_size: int = 500,
        chunk_overlap: int = 50, # 新增：支持自定义重叠
        max_overflow_ratio: float = 0.5,  # 结构化模式允许更大的溢出
        use_semantic: bool = False,
        embedding_model: str = None,
        embedding_base_url: str = None,
        embedding_api_key: str = None, # 新增：API Key
        embedding_max_tokens: int = 8192,
        semantic_max_tokens: int = None, # 新增：语义模型最大上下文
    ):
        self.heading_level = heading_level
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chunk_size = int(chunk_size * (1 + max_overflow_ratio))
        self.use_semantic = use_semantic
        self.embedding_model = embedding_model
        self.embedding_base_url = embedding_base_url
        self.embedding_api_key = embedding_api_key
        self.embedding_max_tokens = embedding_max_tokens
        # 如果未指定语义模型限制，默认与向量模型一致，或者给一个安全值
        self.semantic_max_tokens = semantic_max_tokens or embedding_max_tokens
        self.text_splitter = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

        self.semantic_splitter = None
        if self.use_semantic and SemanticChunker is not None:
            self._init_semantic_splitter()
        elif self.use_semantic:
            event('ingest.semantic_split', status='UNAVAILABLE', reason='dependency_not_available')

    def _init_semantic_splitter(self):
        """初始化语义切分器"""
        # 优先使用传入的 base_url，否则使用全局配置
        raw_base_url = settings.EMBEDDING_BASE_URL if self.embedding_base_url is None else self.embedding_base_url

        # 清洗 base_url (EmbeddingService 内部也会处理，但为了 log 清晰)
        base_url = raw_base_url.rstrip("/")

        # 优先使用传入的模型，否则使用全局默认配置
        model_name = settings.EMBEDDING_MODEL if self.embedding_model is None else self.embedding_model
        api_key = settings.EMBEDDING_API_KEY if self.embedding_api_key is None else self.embedding_api_key

        print(f"🔧 [StructuredSplitter] Initializing EmbeddingServiceAdapter with base_url: {base_url}, model: {model_name}, has_key: {bool(api_key)}")

        # Use the configured provider adapter for semantic embeddings.
        embeddings = EmbeddingServiceAdapter(
            base_url=base_url,
            api_key=api_key,
            model=model_name
        )

        self.semantic_splitter = SemanticChunker(
            embeddings=embeddings,
            breakpoint_threshold_type="percentile",
            breakpoint_threshold_amount=95,
            buffer_size=1,
            min_chunk_size=100,  # 避免过小的碎片
            sentence_split_regex=r"(?<=[。？！\n])"  # 支持中文切分
        )

    def _count_tokens(self, text: str) -> int:
        """计算文本 Token 数"""
        if not text:
            return 0
        if self.text_splitter.langchain_splitter:
            try:
                if hasattr(self.text_splitter.langchain_splitter, '_length_function'):
                    return self.text_splitter.langchain_splitter._length_function(text)
                elif hasattr(self.text_splitter.langchain_splitter, '_tokenizer'):
                    return len(self.text_splitter.langchain_splitter._tokenizer.encode(text))
            except Exception:
                pass
        # 回退估算：中文约 1.5 char/token，英文约 4 char/token，保守起见用 1 char = 1 token (对于 context limit check)
        # 或者为了安全，用 len(text)
        return len(text)

    def _split_content(self, content: str, ignore_limit_check: bool = False) -> List[str]:
        """根据配置选择切分策略"""
        if not content.strip():
            return []

        # 1. 计算 Token 数量
        # 解释：这里必须计算 Token，原因如下：
        # (1) 无论是否使用语义切分，我们都需要确保最终生成的 Chunk 不会超过 向量模型(Embedding Model) 的限制。
        #     如果超过了，向量化时会报错。
        # (2) 如果使用语义切分，我们需要确保传给 语义模型 的文本片段不会超过它的上下文窗口。
        #     虽然我们不想"多此一举"，但为了程序的健壮性，防止 API 报错，这是必要的"安全检查"。
        token_count = self._count_tokens(content)

        # 2. 检查是否超过向量模型限制 (Vector Model Context)
        # 如果未超过限制，直接保留完整块（优先保证结构树）
        # 注意：如果 ignore_limit_check 为 True (如纯语义切分模式)，则跳过此检查，强制进行语义切分尝试
        if not ignore_limit_check and token_count <= self.embedding_max_tokens:
            return [content]

        if not ignore_limit_check:
            print(f"⚠️ [StructuredSplitter] 内容长度 {token_count} 超过向量模型限制 {self.embedding_max_tokens}，准备切分", flush=True)
        else:
            print(f"🧠 [StructuredSplitter] 强制语义切分模式 (Tokens: {token_count})", flush=True)

        # 3. 超过向量限制，判断使用语义切分还是固定切分
        # 逻辑优化 (响应用户建议)：
        # 不再预先估算并进行物理切分，而是直接将内容传给语义切分模型。
        # 理由：SemanticChunker 内部是按句子进行 Embedding 的，只要单句不超限，整体长文本通常也能处理。
        # 如果切分后的结果依然过大，再进行二次处理。

        print(f"🔍 [StructuredSplitter] Checking semantic split: use_semantic={self.use_semantic}, has_splitter={self.semantic_splitter is not None}")

        if self.use_semantic and self.semantic_splitter:
            print(f"🧠 [StructuredSplitter] 使用语义切分 (Content Tokens: {token_count})", flush=True)
            try:
                # SemanticChunker 返回的是 Document 对象列表
                docs = self.semantic_splitter.create_documents([content])
                chunks = [doc.page_content for doc in docs]
                print(f"🧠 [StructuredSplitter] 语义切分完成，获得 {len(chunks)} 个片段", flush=True)

                # 再次检查切分后的片段是否满足向量模型限制
                # 如果某个片段依然过大（虽然语义完整，但 Embedding 模型吃不下），需要二次切分
                final_chunks = []
                for chunk in chunks:
                    chunk_tokens = self._count_tokens(chunk)
                    if chunk_tokens > self.embedding_max_tokens:
                        print(f"⚠️ [StructuredSplitter] 语义片段依然过大 ({chunk_tokens} > {self.embedding_max_tokens})，进行固定二次切分", flush=True)
                        fixed_splitter = TextSplitter(
                            chunk_size=self.chunk_size,
                            chunk_overlap=self.chunk_overlap,
                            max_overflow_ratio=0
                        )
                        final_chunks.extend(fixed_splitter.split(chunk))
                    else:
                        final_chunks.append(chunk)
                return final_chunks
            except Exception as e:
                raise_if_effect_error(e)
                print(f"❌ [StructuredSplitter] 语义切分失败: {e}，回退到固定切分", flush=True)
                # Fallback to fixed split below

        # 4. 回退或默认使用固定切分 (TextSplitter)
        print(f"🔨 [StructuredSplitter] 使用固定切分 (Tokens: {token_count}, Chunk Size: {self.chunk_size})", flush=True)
        # 对应需求："对超出的部分...使用固定切分的方式如：500token切分一个chuck"

        # 这里使用初始化时配置的 chunk_size (通常是 500)
        return self.text_splitter.split(content)

    def split_by_headings(
        self,
        text: str,
        headings: List[HeadingInfo],
        generate_id_func
    ) -> List[StructuredChunk]:
        """
        根据提取的标题信息进行结构化分块

        Args:
            text: 完整文本
            headings: 标题信息列表
            generate_id_func: 生成 chunk ID 的函数

        Returns:
            结构化分块列表
        """
        if not headings:
            # 没有识别到标题，使用普通切分
            return self._fallback_split(text, generate_id_func)

        chunks = []
        heading_path = []  # 当前的标题层级路径
        parent_ids = {}    # 记录每个层级的父 ID
        accumulated_headings = [] # 暂存空内容的标题，用于合并到下一个块

        # 0. 处理前言/概述 (Preamble)
        # 如果第一个标题不在文件开头，说明前面有内容
        if headings and headings[0].start_pos > 0:
            preamble_content = text[:headings[0].start_pos].strip()
            if preamble_content:
                # 优化：对前言部分也进行切分检查 (支持语义切分)
                # 如果前言过长，会调用 _split_content 进行语义/固定切分
                preamble_sub_chunks = self._split_content(preamble_content)

                for idx, pc in enumerate(preamble_sub_chunks):
                    chunks.append(StructuredChunk(
                        content=pc,
                        heading_level=0,
                        heading_text="前言",
                        heading_path=["前言"],
                        parent_id=None,
                        chunk_type="content",
                        token_count=self._count_tokens(pc)
                    ))

        for i, heading in enumerate(headings):
            # 1. 更新标题路径 (先更新路径，确保即使内容为空，路径也是正确的)
            while heading_path and len(heading_path) >= heading.level:
                heading_path.pop()
            heading_path.append(heading.text)

            # 构建完整的标题上下文 (用于拼接到每个 chunk 前面)
            # 例如: "1. Project Overview\n1.1 Project Background"
            full_heading_context = "\n".join(heading_path)

            # 2. 确定内容范围
            content_start = heading.end_pos
            if i + 1 < len(headings):
                content_end = headings[i + 1].start_pos
            else:
                content_end = len(text)

            content = text[content_start:content_end].strip()

            # 修复逻辑：检查内容是否为空，避免生成空块
            # 但不应跳过标题路径更新
            if not content and heading.level > self.heading_level:
                 continue

            # 确定父 chunk ID
            parent_level = heading.level - 1
            parent_id = parent_ids.get(parent_level) if parent_level > 0 else None

            # 判断是否需要按此级别拆分
            if heading.level <= self.heading_level:
                # 修复逻辑：如果有后续标题且当前内容为空，则跳过生成独立 Chunk，
                # 让当前标题通过 heading_path 自动合并到下一个 Chunk 中。
                # 同时维护 parent_ids 以保持层级连通性 (将父节点ID传递给当前层级，相当于当前层级透明化)。
                if not content and i < len(headings) - 1:
                    parent_ids[heading.level] = parent_id
                    continue

                # 创建标题块
                chunk_id = generate_id_func()
                parent_ids[heading.level] = chunk_id

                # 如果内容过长，需要二次切分
                # 优化逻辑：优先判断是否超过向量模型限制 (embedding_max_tokens)
                # 必须考虑加上标题上下文后的总长度
                token_count = self._count_tokens(content)
                heading_tokens = self._count_tokens(full_heading_context)

                # 如果 (标题 + 内容) 超过限制，则进行切分
                if (token_count + heading_tokens) > self.embedding_max_tokens:
                    # 创建标题 chunk (保留作为父节点结构)
                    # 内容包含完整标题路径，确保检索上下文
                    chunks.append(StructuredChunk(
                        content=full_heading_context, # 使用完整路径作为内容
                        heading_level=heading.level,
                        heading_text=heading.text,
                        heading_path=list(heading_path),
                        parent_id=parent_id,
                        chunk_type="heading",
                        token_count=heading_tokens
                    ))

                    # 内容进行二次切分
                    # 如果启用了语义切分 (use_semantic=True)，并且内容超过了向量限制，
                    # 那么 _split_content 内部会自动处理语义切分逻辑
                    # (即: content > embedding_max_tokens -> 尝试语义切分)
                    # 所以这里不需要额外传递 ignore_limit_check=True，除非我们想在未超限时也强行切分
                    # 但结构化切分的原则是“尽量保留结构”，所以仅在超限时切分是合理的。
                    sub_chunks = self._split_content(content)
                    for sub_content in sub_chunks:
                        # 关键修复：将标题上下文拼接到每个子块的内容前面
                        # 确保 "1.1 Project Background" 的切片包含 "1. Project Overview" 信息
                        chunk_content_with_context = f"{full_heading_context}\n\n{sub_content}"

                        chunks.append(StructuredChunk(
                            content=chunk_content_with_context,
                            heading_level=heading.level,
                            heading_text=heading.text,
                            heading_path=list(heading_path),
                            parent_id=chunk_id,
                            chunk_type="content",
                            token_count=self._count_tokens(chunk_content_with_context)
                        ))
                else:
                    # 标题和内容合并为一个块
                    # 同样使用完整标题上下文
                    full_content = f"{full_heading_context}\n\n{content}" if content else full_heading_context
                    chunks.append(StructuredChunk(
                        content=full_content,
                        heading_level=heading.level,
                        heading_text=heading.text,
                        heading_path=list(heading_path),
                        parent_id=parent_id,
                        chunk_type="heading"
                    ))
            else:
                # 低于目标层级的标题，内容合并到上一个块或作为子块
                if content:
                    # 使用完整标题上下文
                    full_content = f"{full_heading_context}\n\n{content}"
                    # 找到最近的父级块
                    nearest_parent_id = None
                    for lvl in range(heading.level - 1, 0, -1):
                        if lvl in parent_ids:
                            nearest_parent_id = parent_ids[lvl]
                            break

                    chunks.append(StructuredChunk(
                        content=full_content,
                        heading_level=heading.level,
                        heading_text=heading.text,
                        heading_path=list(heading_path),
                        parent_id=nearest_parent_id,
                        chunk_type="content"
                    ))

        return chunks

    def _fallback_split(self, text: str, generate_id_func) -> List[StructuredChunk]:
        """当没有识别到标题时，使用普通切分"""
        # Fallback 时，是否应该忽略 limit check?
        # 如果是 Structure 模式但没找到标题，通常还是希望尽量保留完整块，除非超长
        # 所以这里保持 ignore_limit_check=False
        text_chunks = self._split_content(text)
        return [
            StructuredChunk(
                content=tc,
                heading_level=None,
                heading_text=None,
                heading_path=[],
                parent_id=None,
                chunk_type="content",
                token_count=self._count_tokens(tc)
            )
            for tc in text_chunks
        ]

    @staticmethod
    def extract_markdown_headings(text: str) -> List[HeadingInfo]:
        """
        从 Markdown 格式文本中提取标题

        支持：
        - # 一级标题
        - ## 二级标题
        - ### 三级标题

        优化：
        - 排除代码块 (```...```) 中的注释行
        """
        import re
        headings = []

        # 匹配代码块 OR 标题
        # Group 1: 代码块
        # Group 2: 标题的 # 符号
        # Group 3: 标题内容
        # 优化：支持标题前有空格缩进 (^\s*)
        pattern = re.compile(r'(```[\s\S]*?```)|^\s*(#{1,6})\s+(.+)$', re.MULTILINE)

        print(f"🔍 [Parser] Extracting headings from text length: {len(text)}")

        for match in pattern.finditer(text):
            if match.group(1):
                # 匹配到代码块，跳过
                continue

            if match.group(2):
                # 匹配到标题
                level = len(match.group(2))
                title = match.group(3).strip()
                # 过滤掉仅包含 # 的行或空标题
                if not title:
                    continue

                print(f"  -> Found heading match: '{match.group(0).strip()}'")
                print(f"     Group 2 (Hashes): '{match.group(2)}' (Len: {len(match.group(2))})")
                print(f"     Group 3 (Title): '{title}'")

                print(f"  -> Final Level: {level}")

                headings.append(HeadingInfo(
                    level=level,
                    text=title,
                    start_pos=match.start(),
                    end_pos=match.end()
                ))

        print(f"🔍 [Parser] Total headings found: {len(headings)}")
        return headings

    @staticmethod
    def extract_docx_headings(doc) -> List[HeadingInfo]:
        """
        从 Word 文档中提取标题

        通过 paragraph.style.name 识别：
        - Heading 1 → H1
        - Heading 2 → H2
        - Heading 3 → H3
        """
        headings = []
        current_pos = 0

        for para in doc.paragraphs:
            para_text = para.text.strip()
            para_len = len(para_text) + 1  # +1 for newline

            if para.style and para.style.name:
                style_name = para.style.name.lower()

                level = None
                if 'heading 1' in style_name or style_name == 'heading1':
                    level = 1
                elif 'heading 2' in style_name or style_name == 'heading2':
                    level = 2
                elif 'heading 3' in style_name or style_name == 'heading3':
                    level = 3

                if level and para_text:
                    headings.append(HeadingInfo(
                        level=level,
                        text=para_text,
                        start_pos=current_pos,
                        end_pos=current_pos + len(para_text)
                    ))

            current_pos += para_len

        return headings

    @staticmethod
    def extract_pdf_headings(page, avg_font_size: float) -> List[HeadingInfo]:
        """
        从 PDF 页面中提取标题

        通过字体大小判断：
        - 字体大小 > 正文平均字体 * 1.5 → H1
        - 字体大小 > 正文平均字体 * 1.3 → H2
        - 字体大小 > 正文平均字体 * 1.15 → H3
        - 加粗 + 独占一行 → 更可能是标题
        """
        headings = []
        blocks = page.get_text("dict", sort=True)["blocks"]

        current_pos = 0
        for block in blocks:
            if block["type"] != 0:  # 非文本块
                continue

            for line in block["lines"]:
                line_text = ""
                max_font_size = 0
                is_bold = False

                for span in line["spans"]:
                    line_text += span["text"]
                    font_size = span["size"]
                    max_font_size = max(max_font_size, font_size)

                    # 检查是否加粗
                    flags = span.get("flags", 0)
                    if flags & 2 ** 4:  # Bold flag
                        is_bold = True

                line_text = line_text.strip()
                if not line_text:
                    continue

                # 判断是否为标题
                level = None
                if max_font_size > avg_font_size * 1.5:
                    level = 1
                elif max_font_size > avg_font_size * 1.3:
                    level = 2
                elif max_font_size > avg_font_size * 1.15 and is_bold:
                    level = 3
                elif is_bold and len(line_text) < 50:  # 加粗的短行可能是标题
                    level = 3

                if level:
                    headings.append(HeadingInfo(
                        level=level,
                        text=line_text,
                        start_pos=current_pos,
                        end_pos=current_pos + len(line_text)
                    ))

                current_pos += len(line_text) + 1

        return headings

    @staticmethod
    def calculate_avg_font_size(doc) -> float:
        """计算 PDF 文档的平均字体大小"""
        font_sizes = []
        for page in doc:
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        if span["text"].strip():
                            font_sizes.append(span["size"])

        if not font_sizes:
            return 12.0  # 默认值

        # 返回中位数，避免被大标题影响
        font_sizes.sort()
        mid = len(font_sizes) // 2
        return font_sizes[mid]


class FileParser:
    """文件解析器"""

    def __init__(self, kb_id: str, *, image_namespace=None, parser_settings=None, audit_flat=False, xlsx_first_row_as_header=False):
        self.kb_id = kb_id
        self.settings = parser_settings or settings
        self.audit_flat = audit_flat
        if type(xlsx_first_row_as_header) is not bool:
            raise ValueError('XLSX_HEADER_CONFIGURATION_INVALID')
        self.xlsx_first_row_as_header = xlsx_first_row_as_header
        self.image_prefix = kb_id if image_namespace is None else f"{kb_id}/{image_namespace}"
        self.image_dir = os.path.join(self.settings.IMAGE_DIR, self.image_prefix)
        os.makedirs(self.image_dir, exist_ok=True)

    @observe("ingest.parser")
    def parse(self, file_path, chunk_mode="auto", chunk_size=None, chunk_overlap=None,
              separator="\n\n", heading_level=2, use_semantic=False,
              embedding_model=None, embedding_base_url=None, embedding_api_key=None,
              embedding_max_tokens=8192, semantic_max_tokens=None,
              defer_chunking=False, parent_child_config=None):
        """Compatibility facade: extract source units, then invoke ONE chunker.

        Task workers defer until image captions have joined the extracted text.
        Deferred values are intermediate units, never published index chunks.
        """
        from types import SimpleNamespace
        if chunk_size is None or chunk_overlap is None:
            from app.services.runtime_settings import get_runtime_settings
            defaults = get_runtime_settings()
            chunk_size = defaults.chunk_size if chunk_size is None else chunk_size
            chunk_overlap = defaults.chunk_overlap if chunk_overlap is None else chunk_overlap
        if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
            raise ValueError('CHUNK_CONFIGURATION_INVALID')
        self.splitter = SimpleNamespace(chunk_size=chunk_size, chunk_overlap=chunk_overlap, count_tokens=len)
        self.chunk_mode = chunk_mode
        self.heading_level = heading_level
        self._chunk_parameters = dict(size=chunk_size, overlap=chunk_overlap,
            mode=chunk_mode, separators=tuple(dict.fromkeys([separator, '\n', '。'])))
        self._semantic_parameters = dict(embedding_model=embedding_model,
            embedding_base_url=embedding_base_url, embedding_api_key=embedding_api_key,
            embedding_max_tokens=embedding_max_tokens, semantic_max_tokens=semantic_max_tokens)
        self._document = self.extract(file_path)
        return self._document.blocks if defer_chunking else self.chunk_document(
            self._document.blocks, parent_child_config=parent_child_config)

    def extract(self, file_path):
        from app.services.document_extractors import extract_document
        return extract_document(self, file_path)

    def chunk_document(self, blocks, *, parent_child_config=None, persist_manifest=True):
        from app.services.document_chunker import ChunkConfig, chunk_document
        from app.services.parsed_document import ParsedDocument
        from app.services.pdf_provenance import write_manifest
        parameters = getattr(self, '_chunk_parameters', dict(size=4096, overlap=0, mode='legacy'))
        document = ParsedDocument(blocks, self._document.source_sha256, self._document.pages)
        semantic = None
        if parameters['mode'] == 'semantic':
            adapter = StructuredSplitter(self.heading_level, parameters['size'],
                chunk_overlap=parameters['overlap'], use_semantic=True, **self._semantic_parameters)
            if adapter.semantic_splitter is None:
                raise ValueError('SEMANTIC_SPLITTER_UNAVAILABLE')
            semantic = lambda text: [doc.page_content for doc in adapter.semantic_splitter.create_documents([text])]
        chunks = chunk_document(document, ChunkConfig(**parameters, parent_child=parent_child_config),
                                count_tokens=self.splitter.count_tokens, semantic_split=semantic)
        if persist_manifest:
            self.pdf_manifest_path = write_manifest(self.settings.PDF_AUDIT_DIR, document.source_sha256,
                document.pages, chunks, schema='unified-document-chunks-v1', flat=self.audit_flat)
        return chunks


    def _save_image(self, image_bytes: bytes, file_id: str, page_num: int, img_index: int, ext: str = "png") -> str:
        """保存图片到本地"""
        image_filename = f"{file_id}_p{page_num}_i{img_index}.{ext}"
        image_path = os.path.join(self.image_dir, image_filename)

        with open(image_path, "wb") as f:
            f.write(image_bytes)

        # 返回相对路径或文件名，用于前端展示和后续处理
        # 这里返回文件名，前端通过 /static/images/{kb_id}/{filename} 访问
        # 或者后端统一处理路径
        return f"{self.image_prefix}/{image_filename}"


    def _parse_image_file(self, file_path: str) -> List[ParsedChunk]:
        """解析单独的图片文件"""
        chunks = []
        file_name = os.path.basename(file_path)
        # file_path format: .../kb_id/file_id_filename
        # we need file_id
        parts = file_name.split("_")
        file_id = parts[0] if parts else "unknown"

        ext = os.path.splitext(file_name)[1].lower().lstrip(".")
        if ext == "jpeg": ext = "jpg"

        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            # 保存图片到 standalone 图片目录
            # 创建 standalone 目录
            standalone_dir = os.path.join(self.image_dir, "standalone")
            os.makedirs(standalone_dir, exist_ok=True)

            image_filename = f"{file_id}.{ext}"
            image_path = os.path.join(standalone_dir, image_filename)

            with open(image_path, "wb") as f:
                f.write(image_bytes)

            # 相对路径: kb_id/standalone/filename
            saved_path = f"{self.image_prefix}/standalone/{image_filename}"

            chunks.append(ParsedChunk(
                content=f"[图片: {saved_path}]",
                page_number=1,
                content_type="image",
                image_path=saved_path,
                metadata={
                    "timestamp": datetime.now().isoformat(),
                    "original_name": file_name,
                    "is_standalone_image": True
                }
            ))
        except Exception as e:
            raise_if_effect_error(e)
            print(f"Error parsing image file {file_path}: {e}")
            # 如果出错，可能返回空或者抛出异常
            raise e

        return chunks

    def _extract_excel(self, file_path: str) -> List[ParsedChunk]:
        """Read a workbook and bind locators to the exact bytes parsed."""
        from io import BytesIO
        from pathlib import Path
        import re
        from app.services.pdf_provenance import digest_bytes, write_manifest

        source = Path(file_path).read_bytes()
        chunks = []
        from app.services.excel_rows import prepare_excel_workbook
        prepared_source = prepare_excel_workbook(source)
        with pd.ExcelFile(BytesIO(prepared_source)) as workbook:
            for sheet in workbook.sheet_names:
                df = pd.read_excel(workbook, sheet_name=sheet, header=None, keep_default_na=False, dtype=object)
                from app.services.excel_rows import prepare_excel_rows
                df, labels, header_rows = prepare_excel_rows(
                    prepared_source, sheet, df, self.xlsx_first_row_as_header)
                # Scan once per sheet; read-only openpyxl rows can contain EmptyCell.
                sheet_formats = []
                format_status = "UNKNOWN"
                if workbook.engine == "openpyxl" and len(df.columns):
                    try:
                        worksheet = workbook.book[sheet]
                        for row_number, row in enumerate(worksheet.iter_rows(
                                min_row=1, max_row=len(df) + header_rows, max_col=len(df.columns)), start=1):
                            for column_number, cell in enumerate(row, start=1):
                                if cell.value is None:
                                    continue
                                fmt = cell.number_format or ""
                                # [$-409] is a locale marker, not a dollar sign.
                                symbol_format = re.sub(r"\[\$-[^\]]+\]", "", fmt)
                                if not any(s in symbol_format for s in ("$", "€", "£", "¥", "₹")):
                                    continue
                                if (sheet_formats and sheet_formats[-1]["row"] == row_number
                                        and sheet_formats[-1]["end_column"] == column_number - 1
                                        and sheet_formats[-1]["number_format"] == fmt):
                                    sheet_formats[-1]["end_column"] = column_number
                                    sheet_formats[-1]["end_cell"] = cell.coordinate
                                else:
                                    sheet_formats.append({"row": row_number, "start_column": column_number,
                                                          "end_column": column_number, "start_cell": cell.coordinate,
                                                          "end_cell": cell.coordinate, "number_format": fmt})
                        format_status = "AVAILABLE"
                    except Exception:
                        sheet_formats = []
                for i in range(len(df)):
                    row = df.iloc[i]
                    values = [(labels[j], str(value)) for j, value in enumerate(row)
                              if pd.notna(value) and value != ""]
                    if not values:
                        continue
                    locator = {"sheet": sheet, "row_start": i + header_rows + 1,
                               "row_end": i + header_rows + 1,
                               "column_start": 1, "column_end": len(df.columns)}
                    # Preserve display-format evidence without assigning a currency to
                    # values in other cells or inferring a currency code from a symbol.
                    formats = [item for item in sheet_formats if (header_rows and item["row"] == 1)
                               or locator["row_start"] <= item["row"] <= locator["row_end"]]
                    format_text = ""
                    if formats:
                        format_text = "\n[原始货币显示格式（仅适用于列出的单元格）] " + "; ".join(
                            f'{item["start_cell"]}:{item["end_cell"]} (列 {item["start_column"]}-{item["end_column"]}): {item["number_format"]}'
                            for item in formats
                        ) + "\n"
                    chunks.append(ParsedChunk(
                        content=",".join(f"{key}: {value}" for key, value in values) + "\n",
                        page_number=None, content_type="text",
                        metadata={"source_locator": locator, "currency_format_status": format_status,
                                  "currency_formats": formats, "excel_parse_version": "row-kv-v1",
                                  "xlsx_first_row_as_header": self.xlsx_first_row_as_header},
                    ))
        return chunks

    def _parse_excel(self, file_path):
        """Legacy caller adapter; Excel extraction itself only emits row units."""
        return self.parse(file_path, chunk_size=getattr(getattr(self, 'splitter', None), 'chunk_size', 4096),
                          chunk_overlap=getattr(getattr(self, 'splitter', None), 'chunk_overlap', 0))


    def _parse_csv(self, file_path):
        """Legacy caller adapter; all formats share the same chunking facade."""
        return self.parse(file_path, chunk_mode=getattr(self, 'chunk_mode', 'legacy'),
                          chunk_size=getattr(getattr(self, 'splitter', None), 'chunk_size', 4096),
                          chunk_overlap=getattr(getattr(self, 'splitter', None), 'chunk_overlap', 0))
