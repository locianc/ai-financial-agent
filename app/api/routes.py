"""FastAPI Service Layer 路由（最小 MVP）。

- GET /health：存活探针，返回 {"status": "ok"}。
- POST /chat：把 {question} 交给 run_agent 执行完整 Agent 流程，
  返回结构化 JSON（answer / tool_calls / tool_rounds / max_rounds_reached / error /
  run_id / session_id）。不注入 progress 回调，run_agent 默认静默，
  响应中不含任何 CLI 进度文本。
- GET /sessions/{session_id}/runs：列出某会话的全部 Agent Run（按 id 升序）。

会话（Phase 17）：
- POST /chat 请求体可带 session_id：不传时服务端新建会话（create_session），
  传入时复用该会话；返回的 session_id 回显实际归属会话；
- run 落库时关联 session_id，一次会话内的多次运行可通过历史接口串联。

持久化：
- run_agent 成功后调用 app.store.save_run 保存本次运行快照（question + AgentResult
  字段 + 原始 tool_calls + session_id），返回的自增 id 写入 ChatResponse.run_id；
- 持久化失败不阻塞 Agent 回复：捕获异常，run_id 保持 null，HTTP 200 照常返回；
- 会话创建成功而 run 保存失败时 session_id 仍回显；会话创建本身失败则 session_id
  为 null。

错误分层（按执行顺序）：
1. 请求体非法（question 缺失 / 空串）-> FastAPI 校验 -> HTTP 422；
2. 服务层异常：未配置 DEEPSEEK_API_KEY 时 create_client 抛 RuntimeError
   -> HTTP 503；
3. run_agent 内部异常（DeepSeek API 失败 / 工具异常）已捕获进 result.error
   -> HTTP 200 + error 字段。

_client 的创建放在路由函数体内而非 Depends 依赖：
- 依赖解析先于请求体校验执行，若 client 创建先抛 503，会抢占非法请求
  应有的 422；
- 函数体内创建可保证 body 校验（422）永远优先于 503；
- _get_client 为模块级函数，测试用 mock.patch 替换为 fake client。
未来数据库接入同样以可注入函数形式提供，路由不感知存储细节。
"""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from openai import OpenAI

from app.agent import MODEL, AgentResult, collect_tool_results, create_client, run_agent
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    RunRecord,
    SessionInfo,
    ToolCallInfo,
)
from app.output_quality.validator import build_degraded_answer, validate_report_critical
from app.api.stream import router as stream_router
from app.config import load_env
from app.logging_conf import setup_logging
from app.store import create_session, list_runs, list_sessions, save_run

# Phase 19A：服务入口启动即强制加载 .env 并配置统一日志
# （uvicorn 直接启动、容器化启动均生效；幂等可重复触发）
load_env()
setup_logging()

logger = logging.getLogger("app.api.routes")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("AI Financial Agent service started (model=%s)", MODEL)
    yield
    logger.info("AI Financial Agent service stopped")


# Phase 24：production 环境关闭公开文档（/docs、/redoc、/openapi.json），
# 减少公网攻击面；development（默认）保留完整文档便于本地调试。
# load_env() 已在本模块导入时执行，.env / 容器环境变量中的 ENVIRONMENT 生效。
_is_production = os.environ.get("ENVIRONMENT", "development").lower() == "production"
app = FastAPI(
    title="AI Financial Agent Service",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)
app.include_router(stream_router)


def _get_client() -> OpenAI:
    """创建 DeepSeek 客户端；未配置 DEEPSEEK_API_KEY 时抛 RuntimeError。"""
    return create_client()


def _to_chat_response(result: AgentResult) -> ChatResponse:
    return ChatResponse(
        answer=result.answer,
        tool_calls=[
            ToolCallInfo(
                round=record.round,
                name=record.name,
                arguments=record.arguments,
                result=record.result,
            )
            for record in result.tool_calls
        ],
        tool_rounds=result.tool_rounds,
        max_rounds_reached=result.max_rounds_reached,
        error=result.error,
    )


def _to_run_record(row: Dict[str, Any]) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        question=row["question"],
        answer=row["answer"],
        tool_calls=[ToolCallInfo(**tc) for tc in row["tool_calls"]],
        tool_rounds=row["tool_rounds"],
        max_rounds_reached=row["max_rounds_reached"],
        error=row["error"],
        created_at=row["created_at"],
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    logger.info("POST /chat question=%r", request.question[:80])
    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.error("POST /chat client 创建失败（503）: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    result = run_agent(client, request.question)
    # 输出合规校验（高危两类：无证据编造 / 违规荐股）：
    # 校验不通过时拦截原始结论，替换为受限降级答案（含明确风险提示）。
    violations = validate_report_critical(
        result.answer, collect_tool_results(result.tool_calls)
    )
    if violations:
        logger.warning(
            "POST /chat 输出合规校验未通过（%d 项），降级拦截原始结论", len(violations)
        )
        result.answer = build_degraded_answer(violations)
    response = _to_chat_response(result)
    try:
        session_id = request.session_id if request.session_id is not None else create_session()
        response.session_id = session_id
        response.run_id = save_run(
            question=request.question,
            answer=result.answer,
            tool_calls=[asdict(record) for record in result.tool_calls],
            tool_rounds=result.tool_rounds,
            max_rounds_reached=result.max_rounds_reached,
            error=result.error,
            session_id=session_id,
        )
    except Exception:
        # 持久化失败不影响 Agent 回复：run_id 保持 null，HTTP 200 照常返回
        pass
    return response


@app.get("/sessions", response_model=List[SessionInfo])
def sessions() -> List[SessionInfo]:
    """列出全部会话（最近活动在前，Phase 18 前端侧边栏）。"""
    return [SessionInfo(**row) for row in list_sessions()]


@app.get("/sessions/{session_id}/runs", response_model=List[RunRecord])
def session_runs(session_id: int) -> List[RunRecord]:
    """列出某会话的全部 Agent Run（按 id 升序）；会话不存在时返回空列表。"""
    return [_to_run_record(row) for row in list_runs(session_id)]
