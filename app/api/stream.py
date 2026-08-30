"""Phase 18：POST /chat/stream SSE 流式端点。

事件协议（text/event-stream，event: 类型 + data: JSON，空行分隔）：
- tool_call：{"tool", "args"} —— 即将执行某工具，先于执行发出，前端据此展示
  "执行中"卡片（AKShare 调用可能较慢，避免前端误以为卡死）；
- tool_result：{"tool", "status": "ok"|"error"} —— 工具执行完成；
- token：{"content"} —— 最终回答文本增量，逐片下发（仅通过输出合规校验后才会下发）；
- degraded：{"message", "violations"} —— 最终回答被 Validator 判定存在"无证据编造"
  或"违规荐股"，后端已拦截原始结论，改发受限降级答案（含明确风险提示）；
- done：{"session_id", "run_id"} —— 流结束（内部 __result__ 已消费并落库）；
- error：{"message"} —— 任何异常（HTTP 始终 200，错误统一走 error 事件，
  前端 fetch 流式路径无需区分状态码）。

断连处理（防 Token 泄漏）：
- 客户端断开后 FastAPI 会取消本生成器（注入 asyncio.CancelledError）。
  CancelledError 是 BaseException 而非 Exception，必须显式捕获；
- 捕获后立即停止 run_agent_streaming 的 producer 线程（内部 stop 标记），
  终止后台 LLM 推理与工具调用链，随后 re-raise，不再下发任何事件。

会话（Phase 17/18）：
- 请求体可带 session_id：传入时校验存在（get_session），校验通过则复用；
- 不传时服务端新建会话，标题取首条消息前 30 字（create_session(title=...)）；
- 会话创建失败不阻塞 Agent 回复，session_id 保持 null。

持久化：
- run_agent_streaming 的内部 __result__ 事件携带完整 AgentResult 快照，
  端点消费后调用 save_run 落库（含 session_id），run_id 写入 done 事件；
  保存失败捕获，run_id 置 null，不影响已流出的回答与 HTTP 200。

错误分层（与 POST /chat 不同：本端点不抛 503）：
- client 创建失败（未配置 DEEPSEEK_API_KEY）-> error 事件；
- run_agent 内部异常已捕获进 __result__.error / error 事件；
- 生成器本身异常兜底捕获 -> error 事件。

_get_client / run_agent_streaming / save_run / get_session / create_session
均为模块级引用，测试用 mock.patch 替换，零联网可测。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel, Field

from app.agent import create_client, run_agent_streaming
from app.store import create_session, get_session, save_run

router = APIRouter(tags=["chat"])

logger = logging.getLogger("app.api.stream")


class ChatStreamRequest(BaseModel):
    message: str = Field(min_length=1)
    # 所属会话；不传时服务端新建会话（标题取消息前 30 字），返回时回显
    session_id: Optional[int] = None


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _get_client() -> OpenAI:
    """创建 DeepSeek 客户端；未配置 DEEPSEEK_API_KEY 时抛 RuntimeError。"""
    return create_client()


@router.post("/chat/stream")
async def chat_stream(request: ChatStreamRequest) -> StreamingResponse:
    """SSE 流式聊天：tool_call / tool_result / token / done / error 事件。"""

    async def event_generator() -> AsyncIterator[str]:
        logger.info("POST /chat/stream message=%r session_id=%s", request.message[:80], request.session_id)
        # 1. client 创建失败 -> error 事件（HTTP 已 200，不抛 503）；
        #    统一以 done 事件收尾，前端拿到确定性的结束信号
        try:
            client = _get_client()
        except RuntimeError as exc:
            logger.error("POST /chat/stream client 创建失败: %s", exc)
            yield _sse("error", {"message": str(exc)})
            yield _sse("done", {"session_id": None, "run_id": None})
            return

        # 2. 会话解析：有 session_id 且存在则复用；无则新建（标题取消息前 30 字）
        session_id: Optional[int] = None
        if request.session_id is not None and get_session(request.session_id) is not None:
            session_id = request.session_id
        elif request.session_id is None:
            try:
                session_id = create_session(title=request.message[:30])
            except Exception:
                session_id = None  # 会话创建失败不阻塞 Agent 回复

        # 3. 流式事件转发；__result__ 消费后落库，其余事件直接下发
        run_id: Optional[int] = None
        try:
            async for event_type, payload in run_agent_streaming(client, request.message):
                if event_type == "__result__":
                    try:
                        run_id = save_run(
                            question=request.message,
                            answer=payload["answer"],
                            tool_calls=payload["tool_calls"],
                            tool_rounds=payload["tool_rounds"],
                            max_rounds_reached=payload["max_rounds_reached"],
                            error=payload["error"],
                            session_id=session_id,
                        )
                    except Exception:
                        run_id = None  # 持久化失败不影响已流出的回答
                else:
                    yield _sse(event_type, payload)
        except asyncio.CancelledError:
            # 客户端断连：FastAPI 取消本生成器。CancelledError 是 BaseException，
            # 不会被上面的 except Exception 捕获；必须显式处理——run_agent_streaming
            # 的 finally 会置位 stop 标记，终止后台 producer 线程（LLM 推理 / 工具链）。
            # 随后 re-raise 让取消语义继续传播，不再下发任何事件。
            logger.info("POST /chat/stream 客户端断开，终止后台推理与工具链")
            raise
        except Exception as exc:
            logger.error("POST /chat/stream 流异常: %s", exc)
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

        yield _sse("done", {"session_id": session_id, "run_id": run_id})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
