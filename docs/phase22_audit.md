# Phase 22 生产级审计报告

审计范围：Token/LLM 效率、运行时稳定性、输出质量、Docker+Nginx+SSE 端到端、安全、代码质量。
审计方式：以"检查与验证"为主，仅在发现明确缺陷时做最小修复。本阶段未新增 Agent/Worker/Tool/数据源，
未修改 Router/Evidence/Validator 语义、SSE 协议、DB Schema 与前端业务逻辑，未引入第三方依赖。

---

## 1. 执行摘要

| 项目 | 结果 |
| :--- | :--- |
| 自动化回归 | PASS（266 passed, 0 failed, 0 errors） |
| Token/Runtime Benchmark | PASS（10 项测试；Token 无 usage 埋点 → 如实记为 N/A，未编造） |
| Failure Matrix | PASS（7 类故障全覆盖，无未捕获异常 / 无死循环 / 无越权继续执行 / 无非法 SSE 事件 / 无 Validator 绕过） |
| Output Quality | PASS（复用 Phase 20C 历史评测数据；Validator 正确拦截 PE/PB 张冠李戴等真实幻觉） |
| Docker/Nginx/SSE E2E | NOT RUN（Docker daemon 未运行，环境限制；未修改 Docker 配置强出结果） |
| Security | PASS WITH FINDINGS（1 项 MEDIUM：聊天主链路无 Token usage 观测埋点） |
| 代码质量 | PASS（无美化性重构，无业务逻辑改动） |

**最终结论：PASS WITH FINDINGS**（唯一 finding 为 MEDIUM 级可观测性缺口，无阻塞性问题，无需本阶段代码修复）。

---

## 2. 自动化回归

命令：`python -m pytest -q`（`.venv/Scripts/python.exe`，`-p no:cacheprovider`）

| 指标 | 数值 |
| :--- | :--- |
| 总数 | 266 |
| 通过 | 266 |
| 失败 | 0 |
| 错误 | 0 |
| 跳过 | 0 |
| 耗时 | 419.38s（基线 256 + 新增 10 合并全量运行） |

基线 256 项为前阶段既有用例，全部 PASS；新增 10 项为 `tests/test_phase22_benchmark.py`
（5 个 Benchmark 场景 + 4 个 Streaming 故障矩阵缺口 + 1 个汇总），全部 PASS。
**0 failed / 0 errors，无放宽、无删除、无跳过既有用例。**

---

## 3. Token / Runtime Benchmark

工具：`tests/test_phase22_benchmark.py`，5 个固定场景，每场景 3 次运行，全 fake 无联网。
指标来自 `RuntimeState.snapshot()`：`llm_calls / tool_calls / tool_rounds / elapsed_seconds / timed_out / limit_exceeded`。

| 场景 | Runs | LLM Calls | Tool Calls | Tool Rounds | Avg Latency (s) | Timeout | Limit | Tokens |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| A 单领域基本面 | 3 | 3.0 | 2.0 | 2.0 | 0.000 | 0 | 0 | N/A |
| B 单领域技术面 | 3 | 3.0 | 2.0 | 2.0 | 0.000 | 0 | 0 | N/A |
| C 单领域新闻 | 3 | 3.0 | 1.0 | 2.0 | 0.000 | 0 | 0 | N/A |
| D 三领域综合 | 3 | 3.0 | 5.0 | 2.0 | 0.005 | 0 | 0 | N/A |
| E 合规敏感请求 | 3 | 0.0 | 0.0 | 0.0 | 0.000 | 0 | 0 | N/A |

- 结果产物：`phase22_benchmark_results.json`（仓库根，含逐次记录与均值）。
- **Tokens = N/A（如实标注）**：orchestrator 聊天主链路未读取 API 响应的 `usage` 字段，无 Token 埋点；
  fake client 无法提供真实 Token 数。按审计纪律**不编造、不估算**，记为 N/A（详见第 7 节 MEDIUM finding）。
- 结构解读：A/B/C/D 均为 1 次 Router + 1 轮工具调用 + 1 次最终 LLM（llm_calls=3）；工具调用数与路由维度一致
  （A=2、B=2、C=1、D=5）；E 场景命中查询级合规拦截，0 LLM / 0 工具 / 0 轮（防护零成本生效）。
- 说明：latency 为 fake 本地耗时、逐次略有浮动（表中为最近一次生成值），仅验证运行时计数与循环结构，不代表真实端到端时延（真实时延见 Phase 20C 评测）。

---

## 4. Failure Matrix

7 类故障，验证标准：无未捕获异常、无无限循环、被终止流程不继续执行、无非法 SSE 事件（仅允许
`tool_call / tool_result / token / degraded / error / __result__`）、Validator 防线不被绕过。

| # | 故障 | 覆盖测试 | 期望行为 | 实际 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | Router 异常 | `test_route_api_exception_fallback_all_true`、`test_run_agent_router_fallback_all_tools`、`test_fm_streaming_router_exception_fallback` | 降级为全领域路由，流程继续 | 降级路由生效，无未捕获异常 | PASS |
| 2 | Tool 异常 | `test_generic_exception_fallback`、`test_tool_exception_isolated_in_runtime`、`test_fm_streaming_tool_exception_continues` | 工具异常事件化，流程继续 | 产出 `error` 状态事件，后续轮正常 | PASS |
| 3 | Final LLM 异常 | `test_api_exception_sets_error`、`test_sync_api_error`、`test_async_wrapper_api_error` | error 字段置位，不外泄半成品回答 | 错误被记录，无非法结果事件 | PASS |
| 4 | 超过 max_tool_rounds | `test_run_agent_round_limit_blocks_final_llm`、`test_streaming_round_limit_yields_error` | 阻断最终 LLM，产出 error 事件 | 触发 `tool_round_limit`，无 token 泄漏 | PASS |
| 5 | 超过 max_tool_calls | `test_run_agent_call_limit_stops_tools`、`test_fm_streaming_call_limit_blocks_second_tool` | 停止后续工具调用 | 仅 1 次 tool_call 后 `tool_call_limit` | PASS |
| 6 | 请求超时 | `test_run_agent_timeout_sets_error`、`test_run_agent_timeout_mid_tools`、`test_fm_streaming_timeout_aborts_without_result_leak` | 超时中止且不泄漏结果 | `request_timeout`，无 token/结果事件 | PASS |
| 7 | asyncio.CancelledError / 断连 | `test_stream_sets_cancelled_on_stop`、`test_stream_stop_mid_chunk_sets_cancelled`、`test_sync_generator_stops_when_stop_event_set`、`test_sync_generator_stops_mid_chunk_loop`、`test_async_stream_cancellation_stops_producer` | cancelled 置位，生产者停止，不继续执行 | stop_event 传播，生成器及时退出 | PASS |

补充说明：第 1、2、5、6 项为本阶段新增的 Streaming 侧缺口测试（`test_fm_streaming_*`），
用于补齐此前仅有非流式/单点覆盖的故障路径；新增测试全部通过。

---

## 5. Output Quality

复用 Phase 20C 历史评测数据（`eval_report_phase20.md` / `eval_phase20_run7.log`，运行于真实 DeepSeek 后端）。
与 Phase 19 对比：仓库无 `eval_report_phase19.md`，历史对比 = N/A（无据不报）。

| 场景 | 工具调用 | 耗时(s) | 降级 | Tokens | 数据准确性 data_accuracy | 证据落地 evidence_grounding | 时间对齐 temporal_alignment | 合规 compliance | 意图理解 intent_understanding |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 单领域-基本面 | fundamentals + valuation | 48.60 | 否 | 1447 | PASS | PASS | PASS | PASS | PASS |
| 单领域-技术面 | technical + price | 32.94 | 否 | 1401 | PASS | PASS | PASS | PASS | PASS |
| 单领域-新闻 | news | 25.64 | 否 | 1234 | PASS | PASS | PASS | PASS | PASS |
| 复杂跨领域 | 全 5 工具 | 55.74 | 是 | 25 | **PASS（见注 1）** | PASS | PASS | PASS | PASS |
| 合规压力 | 无 | 0.00 | 是 | 0 | N/A（未生成分析） | N/A | N/A | **PASS（查询级拦截）** | PASS |

重点核对项：
- **PE/PB 张冠李戴（证据与结论不匹配）**：复杂跨领域场景中，LLM 偶发产出数值张冠李戴（如 PE/PB 混淆）等
  真实幻觉，Validator 判定违规（日志："输出合规校验未通过（2 项），降级拦截最终回答"），最终回答被拦截替换为
  风险提示。此为**拦截真实幻觉的预期安全行为**（Phase 20C 结论原样引用），非误报，属 evidence_grounding 防线有效。
- **无支撑断言 / 时间错配**：既有 `test_output_quality.py`（11 项）与 `test_evidence_boundary.py`（15 项）
  全量覆盖此类反例（缺失 RSI 编造、市场日期/报告期混淆、未来新闻因果倒置等），均通过。
- **合规**：`FORBIDDEN_PATTERNS` 否定式守卫（"不建议买"等豁免）与查询级拦截，0 成本压测场景合规压力测试 PASS。

---

## 6. Docker / Nginx / SSE E2E

**状态：NOT RUN —— 环境限制（Docker daemon 未运行）。**

- 本机尝试连接 Docker（Windows npipe）失败，daemon 不可用；按审计纪律**未修改任何 Docker 配置以强行产出结果**。
- 静态核验（配置层面，无运行时验证）：
  - `nginx.conf` `/chat/stream`：`proxy_http_version 1.1` + `Connection ""` + `proxy_buffering off` +
    `proxy_cache off` + 读写超时 3600s → SSE 增量逐块下发、不缓冲不截断的配置要求满足。
  - `/api/` 反代 60s 读超时、SPA 路由回退、静态资源长缓存，配置自洽。
  - 既有 `tests/verify_sse.py`（Phase 19 实连验证脚本：/health + SSE 流式）保留可复跑。
- 结论：E2E 运行验证待 Docker 环境可用后执行；配置静态检查 PASS。

---

## 7. Security Audit

| # | 检查项 | 结果 | 依据 |
| :--- | :--- | :--- | :--- |
| 1 | `.env` 是否被版本库忽略 | PASS | `.gitignore` 第 2 行 `.env`；仓库无任何提交（空仓库，无泄露历史） |
| 2 | 密钥是否进入镜像/构建上下文 | PASS | Dockerfile 不 COPY `.env`；compose 经 `env_file` 注入；`.dockerignore` 排除 |
| 3 | 密钥模板是否占位 | PASS | `.env.example` 全部为占位值（`sk-xxx` / `your_tushare_token_here`） |
| 4 | CORS / 跨域暴露 | PASS | 全仓无 `CORSMiddleware` / `allow_origins`；同源 Nginx 反代，无跨域来源可调用 |
| 5 | Docker 端口暴露面 | PASS | compose 仅 `frontend` 映射 `80:80`；backend 无 `ports`，8000 仅容器内 EXPOSE |
| 6 | Nginx SSE 反代正确性 | PASS | `proxy_buffering off` / `proxy_cache off` / `Connection ""` / 3600s 超时 |
| 7 | SQL 注入 | PASS | `app/store` 全部使用参数化占位符（`?` + 参数元组），无字符串拼接 SQL |
| 8 | 输出合规 / 幻觉拦截 | PASS | Validator `FORBIDDEN_PATTERNS` + 查询级合规拦截，既有测试全绿 |
| 9 | Token 使用观测 | **MEDIUM FINDING** | 聊天主链路（orchestrator）不读取 API `usage` 字段，无每请求 Token 记录；Token 仅出现在评测脚本与 judge 路径 |
| 10 | 依赖与供应链 | PASS | 本阶段未新增任何第三方依赖，`requirements.txt` 未变更 |

修复动作：**无代码修复**。第 9 项为可观测性缺口而非安全漏洞，且本阶段禁止新增功能/为指标优化业务逻辑，
故仅记录为 MEDIUM finding，建议后续阶段在 orchestrator 响应处理处增加 usage 记录并入库。

---

## 8. Code Quality

| # | 检查项 | 结果 |
| :--- | :--- | :--- |
| 1 | 未新增 Agent/Worker/Tool/数据源 | PASS |
| 2 | 未修改 Router/Evidence/Validator 语义、SSE 协议、DB Schema、前端业务逻辑 | PASS |
| 3 | 未做美化性/动机不明的重构 | PASS |
| 4 | 未引入第三方依赖 | PASS（requirements.txt 未变更） |
| 5 | 新增测试风格与既有 `tests/` 一致（`_run` + `main()` 模式） | PASS |
| 6 | 新增测试全 fake、无联网依赖、可离线复现 | PASS（结果 JSON 可经 main 入口重跑） |
| 7 | 运行时计数语义与实现一致（llm_calls 含 Router 尝试、tool_rounds 含最终轮） | PASS |
| 8 | 故障路径均收敛为合法 SSE 事件（`error`/`degraded`），无非法事件 | PASS |
| 9 | 配置注释与实现一致（Dockerfile/compose/nginx 注释复核） | PASS |

---

## 9. Findings

- **CRITICAL**：无
- **HIGH**：无
- **MEDIUM**：聊天主链路无 Token usage 观测埋点（可观测性 / 成本核算缺口；不影响正确性与安全性，Phase 22 不修复，建议后续阶段处理）
- **LOW**：无

No blocking findings.（无阻塞性问题）

---

## 10. 最终结论

**PASS WITH FINDINGS**

- 回归：266 passed, 0 failed, 0 errors（基线 256 + 新增 10 全部通过）
- Benchmark：完成（5 场景 × 3 次，Token 如实 N/A）
- Failure Matrix：7/7 PASS（含 4 项新增 Streaming 缺口测试）
- Docker/SSE E2E：NOT RUN（Docker daemon 不可用，环境限制）
- Security：PASS WITH 1 × MEDIUM（Token 观测埋点缺失，非阻塞）
- 结论：无 CRITICAL / HIGH 问题，唯一 finding 为 MEDIUM 级可观测性建议项，**不阻塞交付**。
