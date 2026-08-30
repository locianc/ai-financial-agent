# GitHub 公布前只读审计

> Read-only audit for public GitHub release readiness.
> Date: 2026-08-29 | No project files were modified except this report.
> 说明：全部检查均为只读；未安装依赖、未修改任何源码/配置。

## 1. Verdict

**NEEDS FIXES**（公开前需要修复）

仓库当前没有任何提交（0 commits，全部文件未跟踪），因此不存在可泄露密钥的 git 历史；源码、前端、Docker 镜像、日志中均未发现真实 Secret。但公开前必须补齐：README、LICENSE 缺失，`.gitignore` 覆盖不全（根目录开发日志、评测产物、`.claude/`、`.iml` 等会在首次 `git add .` 时被一并提交）。代码质量本身良好（291 项测试全绿），阻塞项集中在"仓库卫生 + 文档 + 许可"，而非代码缺陷。

## 2. Security Findings

| Severity | Finding | Location | Note |
|---|---|---|---|
| HIGH | 所有 API 端点无认证。任何能访问部署端口的人都可以调用 Agent（消耗付费 DeepSeek Token）并读取全部会话/问答历史。 | `app/api/routes.py`、`app/api/stream.py` | 对公开部署生效；仓库发布本身不受影响，但应在 README 中声明并部署前补齐。 |
| HIGH | 无 TLS/HTTPS，`listen 80`，流量明文。 | `nginx.conf` | 公开部署前必须在 nginx 或外部 LB 终止 TLS。 |
| MEDIUM | LLM 端点（`/chat`、`/chat/stream`）无速率限制，公开部署时存在成本/滥用放大风险。 | `nginx.conf` | 建议 `limit_req` + 每 IP 配额。 |
| MEDIUM | FastAPI `/docs` + `/openapi.json` 默认暴露 API 面。 | `app/api/routes.py` | 生产应 `docs_url=None, redoc_url=None` 或 nginx 拒绝。 |
| MEDIUM | nginx `/api/` `proxy_read_timeout 60s` < 后端 120s 请求预算，非流式长请求会 504。 | `nginx.conf:33` vs `app/agent/runtime.py:11` | 功能缺陷，非安全项。 |
| MEDIUM | 后端容器以 root 运行。 | `Dockerfile` | 应加非 root `USER` 并调整绑定挂载属主。 |
| MEDIUM | `UVICORN_WORKERS=2` × SQLite，并发写可能 `database is locked`。 | `docker-compose.yml:26`、`app/store/__init__.py:96` | WAL + timeout=10 已缓解；并发增长时应换服务端 DB 或降为 1 worker。 |
| LOW | 无安全响应头（CSP / X-Frame-Options / X-Content-Type-Options / HSTS），`server_tokens` 默认开启（泄露 nginx 版本）。 | `nginx.conf` | 公开部署前补齐。 |
| LOW | compose 无容器资源上限（CPU/内存），失控 Agent 运行可能拖垮宿主机。 | `docker-compose.yml` | 加 `mem_limit` / `cpus`。 |
| LOW | `tools/network_adapter.py` 含本机特定网络适配：强制 `NO_PROXY="*"`（绕过本机 127.0.0.1:7897 代理）、固定东方财富 IP `61.129.129.48`、强制 IPv4。该文件被 `COPY tools ./tools` 带入 Docker 镜像，容器内也会生效。 | `tools/network_adapter.py` | 属本机 workaround，可公开但建议改为可选配置/文档化，避免影响其他网络环境。 |
| LOW | 代码注释含本机绝对路径：`app/config.py:18`、多个测试文件 docstring（`E:/github/ai-financial-agent`）。 | `app/config.py`、`tests/*.py` | 仅注释，无功能引用；建议公开前清理为相对描述。 |

## 3. Secret / Credential Findings

**未发现真实 Secret。** 按审计规则报告全部疑似项（均为占位符/测试值，可安全公开）：

| 位置 | 类型 | 判定 |
|---|---|---|
| `.env.example:12` | `DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` 占位符 | 占位符，无真实值，可公开 |
| `.env.example:16` | `TUSHARE_TOKEN=your_tushare_token_here` 占位符 | 占位符，可公开 |
| `tests/test_agent_orchestrator.py:164` | `api_key="test-key"` 测试夹具 | 测试用假值，可公开 |

核查结论：

- **`.env` 是否被 git 跟踪：否**。`git check-ignore .env` 命中，且不在 `git status` 列表中（`git status` 未显示 `.env`）。
- **git 历史是否存在泄漏：不适用**。仓库 0 commits，无历史可查。
- **源码/前端硬编码密钥：无**。全仓 `sk-`/`AKIA`/`ghp_`/`AIza`/`password=`/`secret=` 模式扫描仅命中上述 3 处，均为占位或测试值。
- **Docker 镜像内密钥：无**。`Dockerfile` 无 `COPY .env`；`.dockerignore` 排除 `.env`、`*.log`、`*.db`、`data/`、`logs/`。
- **日志/评测产物中的凭据模式：无**。对 `backend_dev.log`、`frontend/frontend_dev.log`、`eval_phase20_run7.log`、`phase11_live_e2e.log`、`phase22_final_regression.log`、`phase22_benchmark_results.json` 的凭据模式扫描均未命中。

## 4. Repository Hygiene

分类规则：**KEEP**（应发布）/ **IGNORE**（建议加入 `.gitignore`，无需删除）/ **REMOVE BEFORE PUBLICATION**（公开前处理，勿提交）。

### KEEP（应发布）
| 路径 | 说明 |
|---|---|
| `app/` | 后端核心（api / agent / store / evaluation / data / fundamentals / news / output_quality / analysis） |
| `frontend/src`、`frontend/package.json`、`frontend/vite.config.ts`、lockfile | React 前端源码与配置 |
| `tests/` | 291 项测试 + `conftest.py` + `fixtures/`（fixtures 为 mock 模块，无密钥） |
| `tools/` | 后端运行时工具（`stock_tool.py`、`market_data.py`、`network_adapter.py`） |
| `docs/` | `deployment_audit.md`、`phase22_audit.md`（本项目审计文档） |
| `main.py` | CLI 壳层 |
| `Dockerfile`、`Dockerfile.frontend`、`docker-compose.yml`、`nginx.conf` | 部署配置 |
| `requirements.txt`、`.env.example`、`.gitignore`、`.dockerignore` | 依赖与模板 |
| `scripts/eval_phase20.py` | 评测脚本（无密钥） |
| `app/evaluation/cases/phase2_dataset.json` | 评测用例集（可公开样例） |
| `logo.png` | 188KB 图片资产（若有意作为仓库 logo 则 KEEP，否则 REMOVE） |

### IGNORE（建议补充到 `.gitignore`，当前未忽略）
| 路径 | 说明 |
|---|---|
| `tests/outputs/` | 生成的评测输出：`llm_QA/QB/QC_*.json`、`phase9_live_baseline_*.json`、`llm_run.log`、`phase9_live_run.log` |
| `app/evaluation/reports/` | 生成的评测报告（`evaluation_report_*.json/.md`、`raw_records_*.json`；`.dockerignore` 已排除，但 `.gitignore` 未覆盖） |
| `.claude/` | Claude Code 本地设置（`settings.local.json`，含本机允许命令列表，无密钥，属个人工具配置） |
| `ai-financial-agent.iml` | IntelliJ 模块文件（`.idea/` 已被忽略但 `.iml` 未忽略） |

### REMOVE BEFORE PUBLICATION（或同样加入 `.gitignore`；建议二者皆做）
| 路径 | 说明 |
|---|---|
| `backend_dev.log` | 后端开发日志，含请求轨迹（`127.0.0.1` 访问、HTTP 状态码、部分问答请求） |
| `frontend/frontend_dev.log` | vite 开发日志 |
| `eval_phase20_run7.log` | Phase 20C 评测运行日志（含评测输入问题） |
| `phase11_live_e2e.log` | 真实 CLI 端到端会话记录（含真实用户问题与回答全文） |
| `phase22_benchmark_results.json` | Phase 22 基准评测结果（含 LLM 回答） |
| `phase22_final_regression.log` | pytest 回归日志 |
| `eval_report_phase20.md` | Phase 20 评测报告 |

已正确忽略（无需处理）：`logs/`、`data/`（含 `agent_runs.db`）、`app/store/agent_runs.db`（`*.db`）、`__pycache__/`（16 处）、`frontend/node_modules/`、`frontend/dist/`、`.venv/`、`.env`、`.idea/`。

> 关键提醒：仓库当前 0 commits，首次 `git add .` 会把上述 REMOVE/IGNORE 项全部纳入版本库。首次提交前必须先扩展 `.gitignore` 或使用显式文件清单。

## 5. README Gaps

**仓库根目录无 README 文件。** 以下 README 清单项全部缺失：

- [x] 项目一句话简介 — **缺失**
- [x] 核心能力（DeepSeek + AKShare 实时行情/基本面/估值/新闻分析）— **缺失**
- [x] 技术栈（FastAPI / Uvicorn / SQLite / React 19 / Vite / Tailwind / Nginx / Docker）— **缺失**
- [x] 架构说明（前端 → nginx → 后端 → Agent → 数据源；SSE 流式链路）— **缺失**
- [x] 本地运行方式（`python -m venv` + `pip install -r requirements.txt` + 启动）— **缺失**
- [x] 环境变量说明（`DEEPSEEK_API_KEY` / `TUSHARE_TOKEN` / `LOG_LEVEL` / `LOG_DIR` / `AGENT_DB_PATH` / `UVICORN_WORKERS`，模板 `.env.example`）— **缺失**
- [x] Docker 启动（`docker compose up --build`）— **缺失**
- [x] API / SSE 文档（`/health`、`/chat`、`/sessions`、`/sessions/{id}/runs`、`/chat/stream` 事件协议）— **缺失**
- [x] 测试与评测说明（`pytest`、`tests/`、`scripts/eval_phase20.py`、`app/evaluation/`）— **缺失**
- [x] 安全说明（认证 / TLS / 速率限制 / 数据存储位置）— **缺失**
- [x] License — **缺失**（见第 8 节）
- [x] Demo 链接 — **缺失**（项目未公网部署，标注 TODO 即可）

**本次审计不重写 README，仅记录缺口。**

## 6. Docker / Deployment Findings

（详见 `docs/deployment_audit.md` 的完整审计；此处为公开前复核摘要）

- **正确项**：compose 链路完整（nginx → backend:8000 → FastAPI → Agent → DeepSeek/AKShare）；后端 8000 端口未发布到宿主机；SSE 全链路禁缓冲（nginx `proxy_buffering off` + `X-Accel-Buffering: no` + 3600s 超时）；前端全部使用相对 URL，无 localhost 硬编码进生产包；客户端断连端到端停止后端推理；`.env`/`*.db`/日志不进入镜像；`depends_on: service_healthy` + `restart: unless-stopped`。
- **公开前需修复**（配置级）：无认证、无 TLS、`/api/` 60s 超时与后端 120s 预算不匹配、容器以 root 运行、`/docs` 暴露、无速率限制、无安全响应头、无容器资源上限、`UVICORN_WORKERS=2` × SQLite 并发写风险。
- **镜像特定项**：`tools/network_adapter.py`（本机固定 IP / 代理绕过）会随 `COPY tools ./tools` 进入镜像并在容器内生效，公开前建议改为可选配置。

## 7. Dependency Findings

- **Python（`requirements.txt`）**：全部为公开 PyPI 包（akshare 1.18.91、fastapi 0.141.1、uvicorn 0.52.4、openai 3.1.0、tushare 1.4.29、pandas、numpy、pydantic 等）。无 `git+`、无 `file:`、无本地路径、无私服/私有包。
- **前端（`frontend/package.json`）**：`private: true` 为标准项目标志（非私有包）；依赖全部公开 npm 包（react ^19.2.8、react-dom、react-markdown、remark-gfm；dev: vite ^7.2.2、tailwindcss、typescript、@vitejs/plugin-react、@tailwindcss/vite、@tailwindcss/typography）。有 lockfile，`npm ci` 可复现构建。
- **提示**：`requirements.txt` 未包含 `pytest`（测试依赖），新环境跑测试需单独安装（或建议补充 dev-requirements）。

## 8. License

**未发现 LICENSE**（仓库根目录与任何子目录均无许可证文件）。

项目代码虽按私有项目维护，公开到 GitHub 前必须选择一个许可证并添加 LICENSE 文件（本审计不代为选择，需由项目方决定，例如 MIT / Apache-2.0 等）。

## 9. Test Result

执行 `python -m pytest -q`（项目虚拟环境 `.venv/Scripts/python.exe`）：

```
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 74%]
........................................................................ [ 98%]
...                                                                      [100%]
291 passed in 391.69s (0:06:31)
```

- **291 passed / 0 failed / 0 errors / 0 skipped**（与 Phase 23 记录一致）
- 注意：系统 Python（未装依赖）直接跑 pytest 会因缺 `openai`/`pandas`/`tushare` 报 14 个收集错误 —— 属环境缺依赖，非代码问题。全新环境需先 `pip install -r requirements.txt`（含 pytest）。
- 完整套件约 6.5 分钟（含真实数据源相关测试），README 应说明该耗时。

## 10. Required Changes Before Public Release

按优先级：

1. **补 README**（第 5 节全部清单项；含 quickstart、env 说明、Docker 启动、API/SSE 协议、测试说明）。
2. **补 LICENSE**（项目方选定后添加）。
3. **扩展 `.gitignore`**，至少覆盖：`*.log`（根目录开发/评测日志）、`frontend/frontend_dev.log`、`tests/outputs/`、`app/evaluation/reports/`、`.claude/`、`*.iml`。
4. **首次提交使用显式文件清单**（仓库 0 commits，避免 `git add .` 带入日志/评测产物/数据库）。
5. **复核 `tools/network_adapter.py`** 的本机固定 IP 与代理绕过逻辑，公开前改为可选配置或明确文档化。
6. **清理代码注释中的本机绝对路径**（`E:/github/ai-financial-agent`，`app/config.py:18` 及测试 docstring）。
7. **决定 `logo.png` 去留**（188KB 二进制；若作为仓库 logo 保留，建议提交到合适位置）。
8. **部署侧安全加固**（公开部署而非仓库公开时）：认证、TLS、速率限制、关闭 `/docs`、非 root 用户、对齐 `/api/` 超时、容器资源上限、SQLite 并发策略 —— 完整清单见 `docs/deployment_audit.md`。
9. **提交前最后自检**：`git status` 确认 `.env`、`*.db`、`*.log` 不在暂存区；必要时用 `git check-ignore` 验证。

## 11. Final Checklist

### PASS
- [x] 未发现真实 Secret：`.env` 被 gitignore；`.env.example` 仅占位符；源码/前端/Docker/日志无凭据模式
- [x] 无 git 历史泄漏风险（0 commits）
- [x] 依赖全部公开（PyPI / npm），无私有包、无本地路径依赖
- [x] Docker 镜像排除密钥与构建垃圾（`.dockerignore` 完整，无 `COPY .env`）
- [x] 后端端口不对外发布；SSE 全链路禁缓冲；前端无环境硬编码
- [x] 测试全绿：291 passed / 0 failed / 0 errors
- [x] 测试/工具/部署目录结构合理（`app/`、`frontend/`、`tests/`、`tools/`、`docs/`、Docker + nginx）

### NEEDS FIX
- [ ] 根目录无 README（全部清单项缺失）
- [ ] 未发现 LICENSE
- [ ] `.gitignore` 未覆盖根目录 `*.log`、`tests/outputs/`、`app/evaluation/reports/`、`.claude/`、`*.iml`
- [ ] `tools/network_adapter.py` 本机特定网络逻辑（固定 IP / 代理绕过）随镜像发布
- [ ] 代码注释含本机绝对路径（`E:/github/...`）
- [ ] 部署侧安全项（认证 / TLS / 速率限制 / `/docs` / 非 root / 超时对齐 / 资源上限）—— 公开部署前必须处理
