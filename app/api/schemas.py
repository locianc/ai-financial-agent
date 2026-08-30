"""API 请求/响应模型（纯数据，不 import app.agent，保持 API 契约独立）。

从 AgentResult 显式转换为 ChatResponse，而非直接序列化内部 dataclass：
未来数据库接入、内部结构演进时，HTTP 契约保持稳定。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    # 所属会话；不传时服务端新建会话，返回时回显 session_id
    session_id: Optional[int] = None


class ToolCallInfo(BaseModel):
    round: int
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]


class ChatResponse(BaseModel):
    answer: str = ""
    tool_calls: List[ToolCallInfo] = Field(default_factory=list)
    tool_rounds: int = 0
    max_rounds_reached: bool = False
    error: Optional[str] = None
    # 持久化后的记录 id；保存失败或未持久化时为 None
    run_id: Optional[int] = None
    # 所属会话 id；新建会话失败或未持久化时为 None
    session_id: Optional[int] = None


class RunRecord(BaseModel):
    """一次 Agent Run 的历史记录（GET /sessions/{session_id}/runs 列表项）。"""

    id: int
    session_id: Optional[int] = None
    question: str
    answer: str = ""
    tool_calls: List[ToolCallInfo] = Field(default_factory=list)
    tool_rounds: int = 0
    max_rounds_reached: bool = False
    error: Optional[str] = None
    created_at: str


class SessionInfo(BaseModel):
    """一个会话的元信息（GET /sessions 列表项；新建会话标题可空）。"""

    id: int
    title: Optional[str] = None
    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str = "ok"
