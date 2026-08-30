"""AI Financial Agent - 终端 Demo（CLI 壳层）。

Agent 执行逻辑已服务化到 app/agent/orchestrator.py（唯一真源：
SYSTEM_PROMPT / TOOL_SCHEMAS / TOOL_DISPATCH / MODEL / MAX_TOOL_ROUNDS 与
run_agent / create_client 均定义于此，本文件不再保留副本）。

本文件仅保留：
1. CLI 入口 main()：横幅、用户问题输入、进度输出、结果展示、页脚；
2. 向后兼容再导出：from main import run_agent, create_client, SYSTEM_PROMPT,
   TOOL_SCHEMAS, TOOL_DISPATCH, MODEL, MAX_TOOL_ROUNDS 继续可用
   （app/evaluation/sampling.py 与历史测试依赖）。

流程：
1. 用户输入问题（默认"分析贵州茅台 600519"），DeepSeek 识别意图；
2. 模型请求调用工具（支持一次请求并行调用多个工具）；
3. Python 实际执行工具（get_stock_price / get_technical_analysis /
   get_stock_fundamentals / get_valuation_analysis）；
4. 工具结果以 role="tool" 消息返回模型，循环直到模型不再请求工具；
5. 模型基于工具返回的真实数据生成结构化综合分析报告，禁止编造。

数据说明：
- 行情与财务数据来自 AKShare / 东方财富公开接口及 Tushare；
- 技术指标由 Python 本地计算；
- 数据仅用于研究和分析，不构成投资建议，不执行真实交易。
"""

import sys

from dotenv import load_dotenv

from app.agent import (
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_PROMPT,
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
    create_client,
    run_agent,
)

__all__ = [
    "MAX_TOOL_ROUNDS",
    "MODEL",
    "SYSTEM_PROMPT",
    "TOOL_DISPATCH",
    "TOOL_SCHEMAS",
    "create_client",
    "run_agent",
]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


def main() -> None:
    print("=" * 60)
    print("AI Financial Agent - Terminal Demo")
    print("=" * 60)
    print()

    # 默认演示问题；也可通过命令行参数传入任意问题
    user_question = "分析贵州茅台 600519"
    if len(sys.argv) > 1:
        user_question = " ".join(sys.argv[1:])

    try:
        client = create_client()
    except RuntimeError as exc:
        print(f"错误：{exc}")
        return

    print(f"用户问题：{user_question}")
    print()
    result = run_agent(client, user_question, progress=print)

    if result.error:
        print(f"错误：{result.error}")
    elif result.max_rounds_reached:
        print(f"错误：达到最大工具调用轮数（{MAX_TOOL_ROUNDS}），终止。")
        print("数据仅用于研究和分析，不构成投资建议。")
    else:
        print("-" * 60)
        print(result.answer or "（模型未返回文本内容）")
        print("-" * 60)

    print()
    print("数据来源：AKShare / 东方财富 / Tushare")
    print("技术指标：Python 本地计算")
    print("数据仅用于研究和分析，不构成投资建议。")


if __name__ == "__main__":
    main()
