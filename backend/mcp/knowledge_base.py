"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "finance_knowledge_v1"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        import chromadb
        # 优先连接独立 ChromaDB 服务（Chroma 默认 embedding 通常在客户端执行，需要可用模型缓存）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用默认 embedding function，首次运行可能需要下载模型；演示走本地词法适配器。
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "FinClario RAG 知识库"},
        )

        # 如果知识库为空，导入默认文档
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for raw in documents:
            doc = validate_document(raw)
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.sha256(f"{doc['doc_id']}_{doc['version']}_{i}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({**{k:v for k,v in doc.items() if k != "content"}, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ID 由文档内容稳定生成；upsert 使重复导入幂等，同时允许同 ID 内容更新。
            # ChromaDB 会自动生成或更新 Embedding。
            self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库写入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        count = self._collection.count()
        if count == 0:
            return []
        results = self._collection.query(
            query_texts=[query],
            n_results=min(max(top_k*3,10),count),
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                today = date.today().isoformat()
                if meta.get("source_type") == "unverified" or not meta.get("effective_from", "9999") <= today <= meta.get("effective_to", "0000"):
                    continue
                product_ids = set(re.findall(r"EM\d{3}",query.upper()))
                if product_ids and meta.get("product_id") not in product_ids | {"ALL"}:
                    continue
                items.append({
                    **meta,
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items[:max(1,min(top_k,10))]

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        from finance.store import DATA_DIR
        self.add_documents(json.loads((DATA_DIR / 'knowledge.json').read_text()))


def validate_document(raw):
    if not isinstance(raw,dict) or not raw.get('title') or not raw.get('content'):
        raise ValueError('文档必须包含非空 title 和 content')
    d={k:str(raw.get(k,'')) for k in ('title','content','source','source_url','version','effective_from','effective_to','product_id','source_type','doc_id')}
    d['doc_id']=d['doc_id'] or hashlib.sha256((d['title']+d['content']).encode()).hexdigest()
    d['source_type']=d['source_type'] or 'unverified'
    if d['source_type'] not in ('simulation','public_education','unverified'):
        raise ValueError('source_type 必须为 simulation/public_education/unverified')
    d['product_id']=d['product_id'] or 'ALL'
    if d['source_type'] != 'unverified':
        for k in ('source','version','effective_from','effective_to'):
            if not d[k]: raise ValueError('已核验资料必须包含 '+k)
        date.fromisoformat(d['effective_from']); date.fromisoformat(d['effective_to'])
        if d['effective_from'] > d['effective_to']: raise ValueError('适用时间范围无效')
    return d
