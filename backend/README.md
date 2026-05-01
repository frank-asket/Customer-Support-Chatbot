# Backend (FastAPI)

## Run locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

`app.main` automatically loads environment variables from `backend/.env` at startup.

## Environment Variables

- `OPENROUTER_API_KEY`: OpenRouter API key
- `MCP_SERVER_URL`: Meridian MCP server URL
- `FRONTEND_ORIGIN`: Allowed frontend origins for CORS (comma-separated). Example:
  - `http://localhost:3000`
  - `https://meridian-ai-support.vercel.app`
- `OPENROUTER_MODEL`: Legacy single-model env var (still supported as default when `OPENROUTER_DEFAULT_MODEL` is not set)
- `OPENROUTER_DEFAULT_MODEL`: Primary model slug used for normal turns
- `OPENROUTER_FALLBACK_MODEL`: Secondary model slug used when default model fails (timeouts/HTTP errors)
- `OPENROUTER_ESCALATION_MODEL`: Premium model slug used for escalated turns, and as a last route fallback when enabled
- `MODEL_ESCALATION_ENABLED`: Enables escalation routing (`true`/`false`, default: `true`)
- `MODEL_ESCALATE_ON_ORDER_AUTH`: Escalate when order-related requests arrive before authentication (default: `true`)
- `MODEL_ESCALATE_ON_KEYWORDS`: Escalate when urgent/escalation keywords are detected (default: `true`)
- `OPENROUTER_TEMPERATURE`: Model temperature (default: `0.2`)
- `OPENROUTER_MAX_TOKENS`: Max output tokens (default: `400`)
- `HTTP_TIMEOUT_SECONDS`: Timeout for external HTTP calls (default: `30`)
- `HTTP_MAX_RETRIES`: Retry count for upstream failures (default: `2`)
- `HTTP_RETRY_BACKOFF_SECONDS`: Exponential backoff base seconds (default: `0.5`)
- `TOOL_LOOP_LIMIT`: Max assistant tool-call iterations per request (default: `4`)
- `MAX_USER_MESSAGE_CHARS`: Input guardrail for user message size (default: `2000`)
- `AUTH_TOKEN_SECRET`: Secret used to sign auth tokens returned by `/auth/verify` and validated by `/chat` (required in production)
- `AUTH_TOKEN_AUDIENCE`: String claim (`aud`) embedded in each token; validation fails if the server’s value does not match (default: `meridian-support`)
- `AUTH_TOKEN_TTL_SECONDS`: Access token lifetime in seconds (default: `3600`). Clients should call `/auth/refresh` before expiry.
- `AUTH_REVOCATION_REDIS_URL`: Optional Redis URL for distributed token revocation denylist (falls back to in-memory when unset/unavailable)
- `AUTH_REVOCATION_REDIS_PREFIX`: Optional Redis key prefix for revoked `jti` entries (default: `auth:revoked_jti:`)
- **Token lifecycle:** Tokens are signed payloads (`sub`, `iat`, `exp`, `aud`, `jti`). `/auth/refresh` issues a new token and revokes the previous `jti` in Redis when configured (otherwise in-memory fallback).
- `MAX_TOOL_ARGUMENTS_CHARS`: Guardrail for tool argument payload size (default: `4000`)
- `RATE_LIMIT_WINDOW_SECONDS`: Shared rolling window for endpoint limits (default: `60`)
- `RATE_LIMIT_DEFAULT_REQUESTS`: Default request limit per client IP + path per window (default: `120`)
- `RATE_LIMIT_HEALTH_REQUESTS`: `/health` request limit (default: `300`)
- `RATE_LIMIT_CAPABILITIES_REQUESTS`: `/capabilities` request limit (default: `120`)
- `RATE_LIMIT_AUTH_VERIFY_REQUESTS`: `/auth/verify` request limit (default: `20`)
- `RATE_LIMIT_AUTH_REFRESH_REQUESTS`: `/auth/refresh` request limit (default: `60`)
- `RATE_LIMIT_AUTH_LOGOUT_REQUESTS`: `/auth/logout` request limit (default: `60`)
- `RATE_LIMIT_CHAT_REQUESTS`: `/chat` request limit (default: `30`)

### Model Routing Behavior

- The backend builds a per-turn route from env vars: default -> fallback -> escalation.
- If escalation conditions match (order/auth sensitive or escalation keywords), route order becomes escalation -> default -> fallback.
- Any OpenRouter timeout/HTTP failure on one model automatically tries the next model in the route.

## Endpoints

- `GET /health`
- `GET /capabilities`
- `POST /auth/verify`
- `POST /auth/refresh` — body: `{ "auth_token": "<token>" }`; returns new token and TTL
- `POST /auth/logout` — body: `{ "auth_token": "<token>" }`; revokes that token (v2 only)
- `POST /chat`
  - Request supports `message`, `session`, and `stream` (stream currently disabled in tool-calling mode)
  - Response includes `reply`, `session`, and `request_id` for traceability
  - All endpoints are rate-limited per client IP and endpoint path. Exceeded limits return `429 RATE_LIMITED`.

### Streaming

- `stream=true` is currently rejected in `/chat` when tool-calling is enabled.
- This is intentional for MVP reliability. True token streaming with tool-calling requires SSE/WebSocket orchestration and partial tool event framing.

## Run tests

```bash
cd backend
source .venv/bin/activate
pytest -q
```

## Railway deploy note

- When deploying with Docker on Railway, your app must bind to the runtime `PORT` environment variable.
- The project Dockerfile already does this via:
  - `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`
- If port is hardcoded to `8000`, Railway health checks may fail and upstream callers (like Vercel API routes) can return `502`.
