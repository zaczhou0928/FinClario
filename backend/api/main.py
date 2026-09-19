"""
FinClario 智能客服系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import logging
import hashlib
import json
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   FinClario Finance     ║
   ║   基金服务 Agent    ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_finance_store = None
_conversation_locks = {}

def _anthropic_cfg() -> Dict[str, Any]:
    mode=os.getenv('ECHOMIND_MODE','demo')
    if mode not in ('demo','llm'):
        raise RuntimeError('ECHOMIND_MODE 必须为 demo 或 llm')
    if mode=='demo':
        return {'api_key':'offline-demo-no-network','model':'demo-rule-planner'}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    if not os.getenv("ANTHROPIC_MODEL", "").strip():
        raise RuntimeError("LLM 模式请明确配置可用的 ANTHROPIC_MODEL，不再默认使用已退役模型")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.environ["ANTHROPIC_MODEL"].strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    if not base_url and cfg["model"] in {"claude-3-5-sonnet-20241022","claude-3-5-sonnet-20240620"}:
        raise RuntimeError("所配置的 Claude 3.5 Sonnet 官方模型已退役，请配置当前可用模型")
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _finance_store

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request, build_shared_rag_tools
    from core.intent_recognizer import IntentRecognizer
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager

    from finance.store import FinanceStore
    from finance.local_runtime import LocalMemoryManager, LocalKnowledgeBase
    _finance_store = FinanceStore(os.getenv('ECHOMIND_DB_PATH',str(pathlib.Path(_ROOT)/'data'/'finance.sqlite3')))
    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("ECHOMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        finance_store=_finance_store,
    )

    # 默认本地适配器便于单机演示；显式 distributed 保留 Redis + ChromaDB 路径。
    distributed = os.getenv('ECHOMIND_STORAGE','local') == 'distributed'
    if distributed:
        _memory = MemoryManager(
            redis_url=os.getenv('REDIS_URL','redis://localhost:6379/0'),
            chroma_host=os.getenv('CHROMA_HOST','localhost'),
            chroma_port=int(os.getenv('CHROMA_PORT','8001')),
            chroma_path=os.getenv('CHROMA_PERSIST_DIRECTORY',str(pathlib.Path(_ROOT)/'data'/'chroma')),
            api_key=cfg['api_key'],base_url=cfg.get('base_url'),model=cfg['model'])
        kb = KnowledgeBase(
            chroma_host=os.getenv('CHROMA_HOST','localhost'),chroma_port=int(os.getenv('CHROMA_PORT','8001')),
            chroma_path=os.getenv('CHROMA_PERSIST_DIRECTORY',str(pathlib.Path(_ROOT)/'data'/'chroma')))
    else:
        _memory = LocalMemoryManager(_finance_store)
        kb = LocalKnowledgeBase(_finance_store)

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的语义检索。请稍后重试，或转人工客服确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索知识库（基于 ChromaDB 向量检索）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))
    if _orchestrator is not None:
        _orchestrator.set_shared_tools(build_shared_rag_tools(_tool_manager))

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    if os.getenv("ECHOMIND_MODE","demo") == "demo": prom_port = None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=(os.getenv("ALERT_WEBHOOK_URL") or None) if os.getenv("ECHOMIND_ENABLE_WEBHOOK") == "1" else None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("FINANCE_EVAL_BASELINE_PATH", str(pathlib.Path(_ROOT)/"data"/"eval"/"finance_baseline.json")),
    )

    logger.info("FinClario 已就绪")
    yield

    await _monitor.stop()
    if _memory is not None:
        await _memory.close()
    logger.info("FinClario 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="FinClario 金融客服与业务咨询",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(min_length=1,max_length=4000)
    user_id: Optional[str] = None
    conv_id: Optional[str] = Field(default=None,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    request_id: Optional[str] = Field(default=None,min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')


class ChatResponse(BaseModel):
    conv_id:     str
    request_id:  str = ""
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    mode: str = "demo"
    simulation: bool = True
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    policy_warnings: List[str] = Field(default_factory=list)


class ToolTraceResponse(BaseModel):
    request_id: str
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


def current_user(authorization: Optional[str] = Header(default=None)):
    # 公开演示凭证只选择模拟身份，不是真实登录或生产鉴权。
    sessions={'Bearer demo-u1001':'u1001','Bearer demo-u1002':'u1002'}
    user=sessions.get(authorization)
    if not user:
        raise HTTPException(401,'请使用演示身份令牌 demo-u1001 或 demo-u1002')
    return user


def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    import secrets
    token=os.getenv('ECHOMIND_ADMIN_TOKEN','')
    if not token or not x_admin_token or not secrets.compare_digest(token,x_admin_token):
        raise HTTPException(403,'资料导入和运营操作需要配置管理员令牌 X-Admin-Token')


@app.get('/finance/overview')
async def finance_overview(user: str = Depends(current_user)):
    return {'simulation':True,'mode':os.getenv('ECHOMIND_MODE','demo'),
            'account':_finance_store.lookup('accounts',user,user),
            'products':_finance_store.products,
            'transactions':_finance_store.list_records('transactions',user),
            'payments':_finance_store.list_records('payments',user),
            'tickets':_finance_store.list_tickets(user)}


@app.get('/finance/tickets/{ticket_id}')
async def ticket_status(ticket_id: str,user: str = Depends(current_user)):
    result=_finance_store.get_ticket(ticket_id,user)
    if not result['success']: raise HTTPException(404,result['error'])
    return result


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "mode":os.getenv("ECHOMIND_MODE","demo"), "simulation":True, "storage":os.getenv("ECHOMIND_STORAGE","local"), "agents": _orchestrator.get_stats()}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills(admin=Depends(require_admin)):
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: str = Depends(current_user)):
    if _orchestrator is None or _memory is None:
        raise HTTPException(503,'服务未就绪')
    if req.user_id is not None and req.user_id != user:
        raise HTTPException(403,'请求身份与演示会话不一致')
    if not req.message.strip(): raise HTTPException(422,'消息不能为空')
    from agents.agent_orchestrator import Request as OrcReq
    from memory.conversation_memory import MsgRole
    from finance.store import sanitize
    request_id=req.request_id or str(uuid.uuid4())
    conv_id=req.conv_id or str(uuid.uuid5(uuid.NAMESPACE_URL,user+':'+request_id))
    fingerprint=hashlib.sha256(json.dumps({'message':req.message,'conv_id':req.conv_id},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    try:
        _finance_store.bind_conversation(user,conv_id)
        cached=_finance_store.claim_request(user,request_id,fingerprint,conv_id)
        if cached: return ChatResponse(**cached)
    except PermissionError as ex:
        raise HTTPException(403,str(ex)) from ex
    except ValueError as ex:
        raise HTTPException(409,str(ex)) from ex
    # 同一会话串行读写，避免压缩与新消息乱序。进程内锁用于本机单 worker。
    lock=_conversation_locks.setdefault((user,conv_id),asyncio.Lock())
    try:
        async with lock:
            message=sanitize(req.message)
            mem_ctx=await _memory.get_context(user,conv_id,query=message)
            history=[{'role':m.role.value,'content':m.content} for m in mem_ctx.recent_messages[-20:]]
            intent_result=await _orchestrator.recognize_intent(message,history=history)
            orch_req=OrcReq(message=message,user_id=user,conv_id=conv_id,request_id=request_id,
                context=mem_ctx.to_prompt_text(),history=history,entities=intent_result.entities,
                intent=intent_result.intent,intent_group=intent_result.intent_group,
                urgency=intent_result.urgency,intent_confidence=intent_result.confidence)
            result=await _orchestrator.run(orch_req)
            await _memory.add_message(user,conv_id,MsgRole.USER,message)
            await _memory.add_message(user,conv_id,MsgRole.ASSISTANT,result.response)
            # 沟通偏好只允许枚举字段，绝不把风险等级/交易状态写为画像。
            await _memory.update_profile(user,conv_id)
            response=ChatResponse(conv_id=conv_id,request_id=result.request_id,response=result.response,
                intent=result.intent.value if result.intent else 'other',intent_group=intent_result.intent_group,
                agent_type=result.agent_type.value,agent_types=[a.value for a in result.agent_types],
                primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
                supporting_agents=[a.value for a in result.supporting_agents],tools_used=result.tools_used,
                routing_reason=result.routing_reason,routing_confidence=result.routing_confidence,
                escalated=result.escalated,latency_ms=round(result.latency_ms,1),
                knowledge_used='search_knowledge_base' in result.tools_used,
                entities=intent_result.entities,intent_confidence=round(intent_result.confidence,4),
                intent_source_scores=intent_result.source_scores,mode=os.getenv('ECHOMIND_MODE','demo'),
                evidence=[{'tool':t['tool_name'],'result':t.get('result',{})} for t in result.tool_traces],
                policy_warnings=result.policy_warnings)
            _finance_store.finish_request(user,request_id,response.model_dump())
            return response
    except Exception:
        _finance_store.release_request(user,request_id)
        raise


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str, user: str = Depends(current_user)):
    """查看某次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    trace = _orchestrator.get_tool_trace(request_id,user_id=user)
    if trace and trace.get("user_id") != user:
        trace = None
    return ToolTraceResponse(
        request_id=request_id,
        found=trace is not None,
        trace=trace or {},
    )


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(limit: int = 20,user: str = Depends(current_user)):
    """查看最近 N 次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return RecentToolTracesResponse(items=[t for t in _orchestrator.get_recent_tool_traces(limit=200) if t.get("user_id")==user][:max(1,min(limit,100))])


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


class DocInput(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    content: str = Field(min_length=1,max_length=50000)
    doc_id: str = ''
    product_id: str = 'ALL'
    source: str = ''
    source_url: str = ''
    source_type: str = 'unverified'
    version: str = ''
    effective_from: str = ''
    effective_to: str = ''


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput,admin=Depends(require_admin)):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "产品规则待核实", "content": "附带来源、版本和适用日期后由管理员核验..."},
        {"title": "金融平台帮助", "content": "仅供资料整理，未核验文本不进入回答依据..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    try:
        count = await kb.add_documents_async([d.model_dump() for d in body.documents])
    except ValueError as ex:
        raise HTTPException(422,str(ex)) from ex
    _tool_manager._cache.clear()
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...),admin=Depends(require_admin)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    try:
        docs = [DocInput(**d).model_dump() for d in docs]
        count = await kb.add_documents_async(docs)
    except (ValueError,TypeError) as ex:
        raise HTTPException(422,str(ex)) from ex
    _tool_manager._cache.clear()
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {"total_chunks": await kb.doc_count_async()}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None,admin=Depends(require_admin)):
    if os.getenv("ECHOMIND_MODE","demo") == "demo":
        raise HTTPException(409,"演示模式不运行 LLM 评分；请使用 scripts/smoke_finance.py 验证关键流程")
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
    )
    return {
        "pass_rate":       report.pass_rate,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    async with lifespan(app):
        conv_id=None
        while True:
            try: message=input('你: ').strip()
            except (EOFError,KeyboardInterrupt): break
            if message in ('quit','exit','退出'): break
            if not message: continue
            response=await chat(ChatRequest(message=message,conv_id=conv_id),user='u1001')
            conv_id=response.conv_id
            print(f'\nFinClario [{response.agent_type}]: {response.response}\n')


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
