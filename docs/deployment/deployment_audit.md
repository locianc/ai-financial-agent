# Deployment Audit

> Read-only audit of the v1.0 deployment chain (Docker Compose -> Dockerfile -> Nginx -> FastAPI -> SSE -> React).
> Date: 2026-08-29 | No project files were modified during the audit except this report.

## 1. Project Structure

Deployment-relevant files only (node_modules / .venv / caches / .git excluded):

| Path | Role |
|---|---|
| `docker-compose.yml` | Orchestration: `backend` + `frontend` services, volumes, healthcheck |
| `Dockerfile` | Backend image: `python:3.12-slim`, installs deps, runs uvicorn |
| `Dockerfile.frontend` | Multi-stage: node:22-alpine build -> `nginx:1.27-alpine` static host + proxy |
| `nginx.conf` | Frontend container site config (static / `/api` / `/chat/stream`) |
| `app/` | FastAPI backend (`app/api/routes.py`, `app/api/stream.py`, `app/api/schemas.py`, `app/agent/`, `app/store/`, `app/config.py`, `app/logging_conf.py`) |
| `tools/` | Backend runtime dependency (`stock_tool.py`, `market_data.py`) — copied into image |
| `frontend/` | React 19 + Vite 7 + Tailwind 4 (`src/api/client.ts`, `src/components/ChatWindow.tsx`, `vite.config.ts`, `package.json` + lockfile) |
| `tests/` | pytest suite (incl. `verify_nginx_proxy.py`, `verify_sse.py`) |
| `requirements.txt` | Backend pinned deps (fastapi, uvicorn, openai, akshare, tushare, ...) |
| `.env` / `.env.example` | Secrets + config template (`DEEPSEEK_API_KEY`, `TUSHARE_TOKEN`, `LOG_LEVEL`, `LOG_DIR`, `AGENT_DB_PATH`, `UVICORN_WORKERS`) |
| `data/` | Host source of the SQLite bind mount (`./data:/data`) |
| `logs/` | Host source of the log bind mount (`./logs:/app/logs`) |
| `main.py` | CLI shell (imported by legacy tests; **not** used by Docker CMD) |
| `docs/` | Audits (this file, `phase22_audit.md`) |

Notes:
- No `README` at repo root (only `docs/`). Deployment instructions live in comments of `docker-compose.yml` / `Dockerfile`.
- `.env` is gitignored (confirmed: absent from `git status`); `.env.example` committed with placeholders only.
- `.dockerignore` excludes `.env`, `*.log`, `logs/`, `data/`, `tests/`, `frontend/node_modules`, `frontend/dist`, `*.db` — secrets and build junk stay out of the image and build context.

## 2. Docker Configuration

`docker-compose.yml` (2 services, default bridge network; no explicit `networks:` block):

| Service | Image/build | Ports | env | Volumes | Healthcheck | Restart | depends_on |
|---|---|---|---|---|---|---|---|
| `backend` | `Dockerfile` (`python:3.12-slim`) | **none published** (`EXPOSE 8000` is informational only) | `env_file: .env` + `LOG_DIR=/app/logs`, `LOG_LEVEL`, `AGENT_DB_PATH=/data/agent_runs.db`, `UVICORN_WORKERS=2` | `./logs:/app/logs`, `./data:/data` | `python urllib -> http://127.0.0.1:8000/health` interval 10s, timeout 5s, retries 5, start_period 20s | `unless-stopped` | — |
| `frontend` | `Dockerfile.frontend` (`nginx:1.27-alpine`) | `80:80` (only public entry) | none (static build, no env needed) | none | none | `unless-stopped` | `backend: service_healthy` |

Answers to the audit questions:

1. **Complete production chain?** Yes. `docker compose up --build` produces: nginx (static SPA) -> proxy to `backend:8000` -> uvicorn -> FastAPI -> Agent -> DeepSeek/AKShare. Ordering is enforced (`depends_on: service_healthy`), containers auto-restart.
2. **Frontend<->backend communication?** Frontend container's nginx proxies `/api/*` and `/chat/stream` to `http://backend:8000` over the compose bridge network by service name. Backend port 8000 is never published to the host — good isolation.
3. **Nginx as reverse proxy?** Yes — the `frontend` container is nginx acting as static host + reverse proxy for API and SSE.
4. **Port exposure issues?** Backend is correctly internal-only. Only `80:80` is public. No accidental exposure of 8000 or any DB port.
5. **Env/Secret risks?** Secrets come via `env_file: .env` (gitignored, excluded from image and build context). Residual: `./data` and `./logs` host bind mounts hold chat transcripts / question logs in cleartext — not secrets, but need host-level permission/backup policy.
6. **Production config issues?**
   - Backend runs as **root** (no `USER` directive in `Dockerfile`).
   - `UVICORN_WORKERS=2` × SQLite: concurrent write contention possible under parallel load (WAL + `timeout=10` mitigate; see Findings).
   - No container resource limits (`mem_limit` / `cpus`) in compose.
   - `env_file: .env` makes compose fail-fast if `.env` is absent — acceptable, template provided.

`Dockerfile` review: deps installed then compilers purged (small image); copies `app/`, `tools/`, `main.py`; does **not** copy `.env`. CMD: `uvicorn app.api.routes:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-2}"`.

`Dockerfile.frontend` review: `npm ci` (lockfile present -> reproducible), `npm run build` (tsc + vite), then nginx stage copies `nginx.conf` + `dist`. Correct multi-stage pattern.

## 3. Nginx Configuration

`nginx.conf` (installed as `/etc/nginx/conf.d/default.conf` in the frontend container), single `server` block:

| Block | Config | Assessment |
|---|---|---|
| `listen 80; server_name _;` | HTTP only | No TLS anywhere. OK for LAN; blocks public deployment without an external TLS terminator |
| `location /assets/` | `expires 30d`, `Cache-Control: public, immutable` | Correct for hashed Vite assets |
| `location /api/` | `proxy_pass http://backend:8000/;` (strips `/api` prefix), `proxy_http_version 1.1`, `proxy_read_timeout 60s` | Prefix-strip matches vite dev proxy and FastAPI routes (`/sessions`, `/chat`, `/health`...). **Read timeout 60s < backend 120s request budget — mismatch (Finding 3)** |
| `location /chat/stream` | `proxy_pass http://backend:8000;` (path preserved), `Connection ""`, `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 3600s`, `proxy_send_timeout 3600s` | **SSE correctly configured: buffering disabled, long timeouts, HTTP/1.1** |
| `location /` | `try_files $uri $uri/ /index.html` | Correct SPA fallback |

- **SSE buffering:** Not buffered. `proxy_buffering off` + `proxy_cache off` + `Connection ""` at nginx, plus `X-Accel-Buffering: no` and `Cache-Control: no-cache` emitted by FastAPI (`app/api/stream.py`). Streaming should flow chunk-by-chunk.
- **WebSocket:** None configured — not needed. The chat stream is SSE over plain HTTP POST (fetch reader), not WebSocket.
- **CORS:** Neither nginx nor FastAPI sets CORS headers (verified: no `CORSMiddleware`, no `Access-Control-*` in `app/`). This works because frontend and API are same-origin behind the same nginx. If the SPA were ever served from a different origin, CORS would break — no change needed in the current topology.
- **Missing hardening (LOW/MEDIUM):** no `server_tokens off` (nginx version disclosed), no security headers (CSP / X-Frame-Options / X-Content-Type-Options / HSTS), no `limit_req` rate limiting, no `client_max_body_size` (default 1m is fine for chat payloads), no gzip.
- **HTTP/HTTPS:** HTTP only. HTTPS must be added (or terminated upstream) for public deployment.

## 4. FastAPI API Routes

Source: `app/api/routes.py`, `app/api/stream.py` (`router` included via `app.include_router(stream_router)`).

| Method | Path | Purpose | Auth | SSE | Request model | Response model |
|---|---|---|---|---|---|---|
| GET | `/health` | Liveness probe (used by compose healthcheck) | none | no | — | `HealthResponse {status:"ok"}` |
| POST | `/chat` | Full agent run, non-streaming JSON | none | no | `ChatRequest {question, session_id?}` | `ChatResponse {answer, tool_calls[], tool_rounds, max_rounds_reached, error?, run_id?, session_id?}` |
| GET | `/sessions` | List sessions (sidebar) | none | no | — | `List[SessionInfo]` |
| GET | `/sessions/{session_id}/runs` | History of one session | none | no | — | `List[RunRecord]` |
| POST | `/chat/stream` | SSE streaming chat | none | **yes** | `ChatStreamRequest {message, session_id?}` | `text/event-stream` |

SSE event protocol (`app/api/stream.py`, `_sse()` = `event: <type>\ndata: <json>\n\n`):

- `tool_call` `{tool, args}` — emitted before a tool executes
- `tool_result` `{tool, status: "ok"|"error"}`
- `token` `{content}` — final answer deltas (only after output-compliance validation passes)
- `degraded` `{message, violations}` — answer intercepted by validator
- `done` `{session_id, run_id}` — terminal event (HTTP is always 200; errors are delivered as events, never as non-2xx except 422 body validation)
- `error` `{message}`

Confirmed behaviors:

1. **Routes:** as listed; health path `/health` matches the compose healthcheck URL.
2. **Request validation:** FastAPI 422 for empty/missing fields; `session_id` optional.
3. **Error handling:** layered — 422 body → 503 (missing API key, `/chat` only) → 200 + `error` field / `error` event for agent-level failures. Stream endpoint never raises 503.
4. **Timeout:** `RuntimeLimits` — 8 tool rounds / 20 tool calls / **120 s** request budget (`app/agent/runtime.py`), enforced inside `run_agent` / `_stream_agent_events`.
5. **Client disconnect:** `asyncio.CancelledError` (a `BaseException`) is explicitly caught in `chat_stream`, the producer thread's `stop` event is set (halting LLM inference and tool chain), then re-raised. `run_agent_streaming` finally-block also sets `stop`. No tokens leak after disconnect. Frontend additionally aborts via `AbortController`.
6. **CORS:** none (same-origin, see Nginx section).
7. **Persistence:** SQLite via `app/store/__init__.py` — per-connection, `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `timeout=10`; path from `AGENT_DB_PATH` (Docker: `/data/agent_runs.db` on a bind mount; local default `app/store/agent_runs.db`). Schema auto-migrates (idempotent).
8. **Dependency on local files:** SQLite path (env-controlled), log dir `LOG_DIR` (env-controlled, `RotatingFileHandler` 5MB × 3 in `app/logging_conf.py`), `.env` loaded from project root by `app/config.py` (`load_env`, `override=False` so container-injected env wins). DeepSeek base URL `https://api.deepseek.com` (`app/agent/orchestrator.py:62`).
9. **Auth:** none on any endpoint (see Finding 1).

## 5. Frontend API Integration

Source: `frontend/src/api/client.ts`, `frontend/src/components/ChatWindow.tsx`, `vite.config.ts`.

- **API Base URL:** relative paths only — `/api/sessions`, `/api/sessions/{id}/runs`, `/chat/stream`. **No hardcoded host** anywhere in `frontend/src` (grep for `localhost` / `127.0.0.1` / `EventSource` returned no matches in `src/`).
- **Docker/production address:** correct by construction — browser talks to same origin; nginx routes `/api/*` (prefix-stripped) and `/chat/stream` (as-is) to `backend:8000`.
- **SSE consumption:** `fetchChatStream` uses `fetch` + `ReadableStream.getReader()` with manual `event:`/`data:` frame parsing — correct choice over `EventSource` because the endpoint is a POST with a JSON body.
- **Cancellation:** one `AbortController` per send; aborted on the Stop button and on component unmount; `AbortError` is swallowed so no spurious error bubble is shown. Tool cards are backfilled to `"stopped"`.
- **`localhost`/`127.0.0.1` occurrences:** only in `vite.config.ts` dev proxy (`http://127.0.0.1:8000`) — dev-only, not part of the production build. Not a deployment issue.
- **Production env concerns:** none — no build-time API URL is baked in; no `.env` needed at build time for the frontend.

## 6. Current Deployment Topology

```
Browser
  │  HTTP/1.1, :80 (plaintext — no TLS)
  ▼
nginx (frontend container, nginx:1.27-alpine)      <- static /usr/share/nginx/html
  │  /assets/*   -> local static (30d cache)
  │  /api/*      -> proxy_pass http://backend:8000/  (strip /api, HTTP/1.1)
  │  /chat/stream-> proxy_pass http://backend:8000    (SSE, buffering off, 3600s)
  │  /           -> SPA fallback (index.html)
  ▼  compose bridge network, service name "backend", port 8000
uvicorn (backend container, python:3.12-slim, --workers 2)
  │
  ▼
FastAPI app (app/api/routes.py + app/api/stream.py)
  │
  ├─ POST /chat/stream  -> run_agent_streaming (thread producer + asyncio.Queue)
  │                       -> _stream_agent_events -> DeepSeek LLM (tool calling loop)
  │                       -> tools: get_stock_price / get_technical_analysis /
  │                          get_stock_fundamentals / get_valuation_analysis / get_stock_news
  │                       -> final answer validated (validator) -> SSE events
  │
  └─ POST /chat, GET /sessions, GET /sessions/{id}/runs  -> run_agent / SQLite
                                                          (AGENT_DB_PATH=/data/agent_runs.db)
  │
  ▼  outbound HTTPS (from backend container)
DeepSeek API (api.deepseek.com:443)
  +  AKShare / 东方财富 / Tushare (public data endpoints, HTTPS)
```

| Hop | Protocol | Port | Container | Config source |
|---|---|---|---|---|
| Browser → nginx | HTTP/1.1 | 80 | `frontend` (nginx:1.27-alpine) | `nginx.conf`, `docker-compose.yml` (`80:80`) |
| nginx → backend | HTTP/1.1 (compose DNS) | 8000 | `backend` (python:3.12-slim) | `nginx.conf` proxy_pass, `Dockerfile` EXPOSE/CMD |
| backend → FastAPI | in-process (uvicorn ASGI) | 8000 | `backend` | `Dockerfile` CMD: `uvicorn app.api.routes:app` |
| FastAPI → Agent | in-process | — | `backend` | `app/api/stream.py` → `app/agent/orchestrator.py` |
| Agent → DeepSeek | HTTPS outbound | 443 | `backend` | `orchestrator.py` `BASE_URL`, `DEEPSEEK_API_KEY` (env) |
| Agent → AKShare etc. | HTTPS outbound | 443 | `backend` | `tools/stock_tool.py`, `app/data/`, `app/fundamentals/` |

## 7. Findings

| Severity | Finding | File | Recommendation |
|---|---|---|---|
| HIGH | **No authentication on any API endpoint.** Any client reaching the published port can run the agent (burning paid DeepSeek tokens) and read the full chat/session history (data exposure). | `app/api/routes.py`, `app/api/stream.py` | Before exposing beyond a trusted network, add authn/authz (reverse-proxy OIDC, API token, or gateway). At minimum restrict by firewall/VPN. |
| HIGH | **No TLS/HTTPS.** `listen 80` only; all traffic including session history is plaintext. | `nginx.conf` | Terminate TLS at nginx (or an external LB) and redirect 80→443. Required for any public deployment. |
| MEDIUM | **Nginx `/api/` read timeout (60s) is shorter than the backend request budget (120s).** A non-streaming `POST /chat` agent run lasting 61–120 s will be cut off by nginx with 504 before FastAPI finishes. (Frontend is unaffected — it only uses `/chat/stream`.) | `nginx.conf:33` vs `app/agent/runtime.py:11` | Raise `proxy_read_timeout` for `/api/` (e.g., 180s) or align the backend timeout. |
| MEDIUM | **Multi-worker uvicorn (2) × SQLite** — concurrent writes from separate processes can raise `database is locked` under parallel load. WAL + `timeout=10` mitigate but do not eliminate. | `docker-compose.yml:26`, `app/store/__init__.py:96` | Either keep `UVICORN_WORKERS=1`, or move persistence to a server DB if concurrent usage grows. Monitor for lock errors. |
| MEDIUM | **Backend container runs as root.** | `Dockerfile` | Add a non-root `USER` (and fix host bind-mount ownership for `./data`, `./logs`). |
| MEDIUM | **FastAPI `/docs` and `/openapi.json` are exposed** without auth, disclosing API surface. | `app/api/routes.py:75` | Disable docs in production (`docs_url=None, redoc_url=None`) or deny via nginx. |
| MEDIUM | **No rate limiting** on LLM-backed endpoints (`/chat`, `/chat/stream`) — cost/abuse amplification for a public deployment. | `nginx.conf` | Add `limit_req` zones for chat endpoints; consider per-IP quotas. |
| LOW | **No security headers** (CSP, X-Frame-Options, X-Content-Type-Options) and `server_tokens` left at default (nginx version disclosure). | `nginx.conf` | Add headers; `server_tokens off;`. |
| LOW | **No container resource limits** (CPU/mem) in compose — a runaway agent run could starve the host. | `docker-compose.yml` | Add `mem_limit` / `cpus` per service. |
| LOW | **Host bind mounts `./data` and `./logs` hold cleartext chat transcripts and question logs** with no backup/retention policy and no enforced permissions on the host. | `docker-compose.yml:28-29` | Define backup + retention for `./data`; restrict host directory permissions; consider encrypted volumes. |

Additional verified-clean items (no action): secrets never enter the image (`.dockerignore` + no `COPY .env`); `.env` is gitignored; backend port 8000 not published; SSE buffering fully disabled (nginx + `X-Accel-Buffering`); client-disconnect stops backend inference; frontend uses relative URLs (no localhost baking); `depends_on: service_healthy` ordering; `restart: unless-stopped` on both services; healthcheck endpoint matches a real route.

## 8. Production Deployment Checklist

### PASS
- [x] Compose forms a complete chain: nginx → backend:8000 → FastAPI → Agent → DeepSeek/AKShare
- [x] Backend port 8000 not exposed to the host (internal network only)
- [x] SSE streaming not buffered by nginx (`proxy_buffering off` + `Connection ""` + `X-Accel-Buffering: no`, 3600s timeouts)
- [x] Frontend API calls use relative URLs — no `localhost`/`127.0.0.1` hardcoded in `frontend/src`
- [x] Client disconnect handled end-to-end (AbortController + `CancelledError` → producer stop, no token leak)
- [x] Secrets excluded from image and build context; `.env` gitignored
- [x] SQLite persisted via named bind mount, WAL mode, auto-migrating schema
- [x] Healthcheck present, correct path, `depends_on: service_healthy`
- [x] `restart: unless-stopped` on both services
- [x] Reproducible frontend build (`npm ci` with lockfile)
- [x] Same-origin architecture — no CORS mismatch in the current topology

### NEEDS CONFIGURATION
- [ ] HTTPS/TLS certificate and 80→443 redirect (nginx or external terminator)
- [ ] Public domain / `server_name` (currently `_`)
- [ ] Authentication for exposed endpoints (OIDC / API token / gateway) if internet-reachable
- [ ] Rate limiting on `/chat` and `/chat/stream`
- [ ] Firewall scope: restrict 80 to intended network; confirm 8000 never published
- [ ] Backup + retention policy for the `./data` SQLite volume (and host permissions for `./data`, `./logs`)
- [ ] `env_file: .env` present at deploy time (copy from `.env.example`)

### NEEDS FIX
- [ ] Nginx `/api/` `proxy_read_timeout` 60s vs backend 120s request budget mismatch (nginx.conf:33)
- [ ] Backend container running as root → add non-root `USER` (Dockerfile)
- [ ] Disable/deny FastAPI `/docs` + `/openapi.json` in production
- [ ] Add security headers + `server_tokens off`
- [ ] Revisit `UVICORN_WORKERS=2` with SQLite (lock contention) or move to a server DB

## 9. Final Verdict

**NEEDS FIXES BEFORE DEPLOYMENT**

The deployment chain is well-constructed: the compose topology is complete, the backend is correctly isolated from the host, SSE is provably un-buffered, the frontend is free of environment hardcoding, and disconnect/cancellation semantics are solid. However, the `/api/` proxy timeout mismatch (60s vs 120s) is a functional defect for non-streaming clients, and the combination of no authentication, no TLS, and no rate limiting on LLM-backed endpoints blocks a production (internet-facing) release as-is. These are configuration/code-level fixes at the deployment boundary, not architectural changes.
