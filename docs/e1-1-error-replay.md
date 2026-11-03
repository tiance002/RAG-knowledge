# E1 错题证据级复盘：反射面（历史运行）

该次用户核心判定为 FAIL：冻结 Gold 需要 `shiny object` 和 `mirror`，原 PDF 第 9 页明确列出两者；此次回答列出微波炉光亮表面，却明确称资料没有镜子实例。所保存的当次输入中没有该词，不能仅凭这个现象断言模型凭空忽略了已看到的镜子。执行完成不是正确性通过。

这份复盘只读取历史 E1 账本、SSE、观测事件和当时私有快照，与后续 E1.1 r6 代码及其新采集字段无关；不把当前实现倒灌到旧运行。没有重发模型请求。

## 问题、配置及评审身份

```json
{
  "qid": "mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0958",
  "run_id": "p1-http-20261029-145902-5b9d1ff3",
  "case": "case-02",
  "trace_id": "883e966cd104857ce8fafcc003d422a1",
  "question": "What are two examples of reflecting surfaces?",
  "kb_scope": [
    "v200-pdf-regression-after"
  ],
  "search_mode": "fulltext",
  "top_k": 20,
  "score_threshold": 0.0,
  "use_rewrite": false,
  "model": "zai-org/GLM-5.3-Flash",
  "gold": [
    "shiny object",
    "mirror"
  ],
  "reviewer_type": "USER",
  "core_correctness": "FAIL",
  "historical_rubric_version": "NOT_CAPTURED",
  "derived_import_rubric_version": "e11-historical-import-v1"
}
```

问题和用户消息来自历史 prompt 快照；`use_rewrite=false` 来自同批 `result.json`，配置和阈值来自历史事件。由原始问题且未改写可合理推测检索 Query 与问题相同，但历史 `retrieval.search` 未直接保存 Query 或 Query 快照，**实际检索 Query 的直接证据为 NOT_CAPTURED**，不能将推测当作观察事实。模式 fulltext，不存在可证实的向量、Hybrid 归一化或融合阶段；Rerank 未启用，不能生成其分数或候选。

Prompt 快照是历史 `grounded.request` 显式记录的发送前消息列表（不是供应商回读）：一条 system 消息，长度 9,267 字符，含 20 条当时引用上下文；一条 user 消息，长度 45 字符，内容如上；无其他历史消息。该快照以 SHA 命名且字节哈希匹配；不包含英文 `mirror`。这是实际保存的发送前材料，不表示从 Cloud 可直接回读全文或证明全部上游源文件均已送入。完整消息见下方文件清单。

## 召回和候选

历史 `retrieval.bm25`、`retrieval.before_dedup`、`retrieval.after_dedup`、`qa.citations` 的有序 ID 均为 20 条且顺序相同；前者缺 file/page 元数据，下表使用后续已丰富的 `before_dedup` 事件。下列分数是该事件中的 BM25 分数；不是向量或 Rerank 分数：

```json
[
  {
    "rank": 1,
    "chunk_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c-11",
    "file_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c",
    "page_number": 5,
    "bm25_score": 8.797007845803922
  },
  {
    "rank": 2,
    "chunk_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c-10",
    "file_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c",
    "page_number": 5,
    "bm25_score": 8.602386306277747
  },
  {
    "rank": 3,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-40",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 13,
    "bm25_score": 7.886844277443431
  },
  {
    "rank": 4,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-37",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 13,
    "bm25_score": 6.956996224615343
  },
  {
    "rank": 5,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-38",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 13,
    "bm25_score": 6.678588118624012
  },
  {
    "rank": 6,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-36",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 13,
    "bm25_score": 6.573365792143304
  },
  {
    "rank": 7,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-43",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 14,
    "bm25_score": 5.9627306584919415
  },
  {
    "rank": 8,
    "chunk_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c-9",
    "file_id": "v200-pdf-regression-after-adb165075d0ca113cfeee9d32c29ada4e38a23d73b44d11fbda6d4aca8d27b5c",
    "page_number": 5,
    "bm25_score": 5.637275873440943
  },
  {
    "rank": 9,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-58",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 16,
    "bm25_score": 5.521836591286788
  },
  {
    "rank": 10,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-137",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 33,
    "bm25_score": 4.4170451437235
  },
  {
    "rank": 11,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-122",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 30,
    "bm25_score": 4.302273782153942
  },
  {
    "rank": 12,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-186",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 44,
    "bm25_score": 4.302273782153942
  },
  {
    "rank": 13,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-20",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 8,
    "bm25_score": 4.166933137503699
  },
  {
    "rank": 14,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-68",
    "file_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54",
    "page_number": 19,
    "bm25_score": 4.166933137503699
  },
  {
    "rank": 15,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-93",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 23,
    "bm25_score": 4.089740269834408
  },
  {
    "rank": 16,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-13",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 5,
    "bm25_score": 3.991158113623249
  },
  {
    "rank": 17,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-104",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 25,
    "bm25_score": 3.9436280347780537
  },
  {
    "rank": 18,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-109",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 26,
    "bm25_score": 3.920285003755539
  },
  {
    "rank": 19,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-84",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 21,
    "bm25_score": 3.8972166896837055
  },
  {
    "rank": 20,
    "chunk_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42-184",
    "file_id": "v200-pdf-regression-after-95639c95f6c1f36315b5e5eb2f3c397481331fbc874aeb4555ba33af2d065a42",
    "page_number": 44,
    "bm25_score": 3.8972166896837055
  }
]
```

目标 PDF 的 file ID 为 `v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54`；其在此 20 条中出现页码 8、13、14、16、19，**没有物理第 9 页**。这是已保存的 20 条候选和引用的观察，不等于该文档没有第 9 页索引 chunk，也不能解释候选为何未出现：历史 tokenization、未进入 Top-K 的候选、各项淘汰理由、完整同粒度 qrel 均为 NOT_CAPTURED。不可计算 Recall，不能推断是去重、阈值、解析或排序导致。历史事件中的 `span_id`/`parent_id` 不是已回读核实的逐阶段 SDK Observation ID，旧事件缺失的 Observation ID 保留 NOT_CAPTURED。

## 原始来源与实际引用

原 PDF 文件字节 SHA-256 与历史来源哈希一致；第 9 页原始图像左下说明可直接读到：

> Reflecting surface, e.g. shiny object or mirror

页图路径及哈希见下。实际 SSE 中，回答使用的一基编号 [3]、[5]、[6] 指向下列第 13 页片段（保留当次原文和 Chunk ID）：

```json
[
  {
    "citation_id": 3,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-40",
    "file_name": "b3m5kaeqm2w8n4bwcesw-140602121350-phpapp02_95.pdf",
    "location": "第 13 页",
    "excerpt": "doesn’t use particles; you can\nfeel its effects (warmth)\nShiny surfaces at the bottom; | Kinetic energy is given to water\nreflects infrared rays back\nor Fat molecules\ntowards food\nFrequency:\nHelps heat surfaces of the\nenergy is transferred by\nThe higher the Frequency the\nfood\nconduction or convection\nmore energy is has and the\nmore dangerous it is\nEnergy is transferred by\nShiny surfaces reflect any\nconduction (solid) and\nwaves back to food\nWavelength: must be short\nconvection (Fluids)"
  },
  {
    "citation_id": 5,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-38",
    "file_name": "b3m5kaeqm2w8n4bwcesw-140602121350-phpapp02_95.pdf",
    "location": "第 13 页",
    "excerpt": "Infrared Radiation=Energy is transferred by conduction (solid) and convection (Fluids) | Microwaves=Shiny surfaces reflect any waves back to food"
  },
  {
    "citation_id": 6,
    "chunk_id": "v200-pdf-regression-after-6d75cf3356c0fe7091c7fdeb7778b6e95934a7f29ddf1c8a66efb3b38d80cc54-36",
    "file_name": "b3m5kaeqm2w8n4bwcesw-140602121350-phpapp02_95.pdf",
    "location": "第 13 页",
    "excerpt": "Infrared Radiation=Shiny surfaces at the bottom; reflects infrared rays back towards Food | Microwaves=Kinetic energy is given to water or Fat molecules"
  }
]
```

这些片段支持关于炉内反射的相应陈述，并未提供第 9 页的镜子例子；回答末尾否认其他例子与原页冲突，不能把当次未送达的资料当成模型上下文。引用编号按 SSE `rag_result.citations` 的一基顺序对应，UI 点击未验证。历史脚本 `python var/e1-20261029/check_mirror_evidence.py` 本次退出 0，输出 `citation_count=20, mirror_matches=[]`。该脚本按哈希 PDF 文件名比较导致 `source_file_locations=[]`，而 SSE 中实际文件名使用别名；不能据此误称没有返回同一来源，第 13 页来源由 file ID 和 lineage 源哈希关联。

## 回答与评判

历史 `grounded.candidate` 指向的可读快照与 SSE `answer_chunk` 拼接字节一致，SHA-256 都为 `1e1547427fb8eece169c083f8d3b6027654babb36d9291c4a6fd37300753ef2c`。历史 SSE `done` 与供应商 `finish_reason=stop` 表示本次生成返回，不等于判定正确。答复如下：

根据参考资料，其中提到的反射面都是**光亮表面（shiny surfaces）**，主要出现在微波炉加热的相关内容中：

1. **微波炉底部的光亮表面**——能将红外线反射回食物[3][6]
2. **能将微波（任何波）反射回食物的光亮表面**[3][5]

需要说明的是，资料中只提到了"shiny surfaces"（光亮表面）这一类反射面，并没有给出其他类型反射面的具体例子（如镜子、抛光金属等）。如果您想了解微波炉以外的反射面例子，参考资料中没有相关信息。

用户的核心 FAIL 来自历史 `ledger-v4.jsonl` 的用户判断；`e11-historical-import-v1` 是后续导入规则的版本，不是伪造的历史 rubric 版本。历史其他质量判定不能从用户核心 FAIL 自动推出；当次引用和来源细项的 AI 复核见 `pdf-hotpot-supplement-review.jsonl` 与 `review24-independent-20261029.jsonl`。费用未知。

## 文件与 SHA-256

```json
{
  "D:\\RAG knowledge\\var\\e1-20261029\\p1-http-20261029-145902-5b9d1ff3\\case-02\\response.sse": "70414ef18c2ea21cc5cbf924eb172eb2160117195670a4fb0c90990e63ff7345",
  "D:\\RAG knowledge\\var\\e1-20261029\\p1-http-20261029-145902-5b9d1ff3\\case-02\\events.jsonl": "5255d180edf638f36af3cbaa83166e4b936b05aeeab682099bc299fd7ab87865",
  "D:\\RAG knowledge\\var\\e1-20261029\\p1-http-20261029-145902-5b9d1ff3\\result.json": "b358af729de57738bc4fccb2c0baecf744b154b34bf410258d076b55605a06b3",
  "D:\\RAG knowledge\\var\\e1-20261029\\p1-http-20261029-145902-5b9d1ff3\\case-02\\private-snapshots\\e790cb09b1c0e3a56e002b986b3207c26d5fa2754612d34be8c74531dc6c6b65.txt": "e790cb09b1c0e3a56e002b986b3207c26d5fa2754612d34be8c74531dc6c6b65",
  "D:\\RAG knowledge\\var\\e1-20261029\\p1-http-20261029-145902-5b9d1ff3\\case-02\\private-snapshots\\1e1547427fb8eece169c083f8d3b6027654babb36d9291c4a6fd37300753ef2c.txt": "1e1547427fb8eece169c083f8d3b6027654babb36d9291c4a6fd37300753ef2c",
  "D:\\RAG knowledge\\var\\corpus\\43be9592854c7da10bf011335faec017b726ff2e23bb4ccae7e8fe88d7801f22.pdf": "43be9592854c7da10bf011335faec017b726ff2e23bb4ccae7e8fe88d7801f22",
  "D:\\RAG knowledge\\var\\e1-20261029\\pdf-page-0958.png": "37536a660b7df189d54d955a8dded91335692e353f5f017e51b964aae7652da2",
  "D:\\RAG knowledge\\var\\e1-20261029\\ledger-v4.jsonl": "a4bf3478616fe2a2c4d2cf8818cf33d31f3996c3dffdebd77b5094774b60a09a"
}
```

验证采用本机 Python 只读解析：核对 4 个阶段 20 个有序 ID 一致、候选中目标 PDF 第 9 页数量为 0、原始 SSE 与回答快照字节相等、源 PDF/页面图及相关文件 SHA-256；均已执行，退出码 0。源页图是视觉复核，不能由图像无文本层推断不存在可检索内容。
