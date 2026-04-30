from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv


AUTH_REGISTRY: dict[str, str] = {
    "donaldgarcia@example.net": "7912",
    "glee@example.net": "4582",
    "michellejames@example.com": "1520",
}
CUSTOMER_CONTEXT_REGISTRY: dict[str, dict[str, str]] = {
    "donaldgarcia@example.net": {
        "first_name": "Donald",
        "last_order_id": "A100",
        "last_order_status": "In transit, expected tomorrow by 5 PM",
        "primary_request": "Track my recent order"
    },
    "glee@example.net": {
        "first_name": "Grace",
        "last_order_id": "B219",
        "last_order_status": "Delivered on Apr 28",
        "primary_request": "Check return eligibility for a delivered item"
    },
    "michellejames@example.com": {
        "first_name": "Michelle",
        "last_order_id": "C771",
        "last_order_status": "Processing at fulfillment center",
        "primary_request": "Confirm shipping window and delivery estimate"
    },
}
ORDER_TOOL_KEYWORDS = ("order", "tracking", "history", "shipment")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "google/gemini-1.5-flash"
SAFE_FALLBACK_MODEL = "google/gemini-1.5-flash"
ESCALATION_KEYWORDS = ("urgent", "angry", "manager", "complaint", "refund", "chargeback")

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")
SYSTEM_PROMPT_PATH = BACKEND_DIR / "prompts" / "system_prompt.txt"
AUTH_INSTRUCTIONS_PATH = BACKEND_DIR / "prompts" / "auth_instructions.txt"
TOOL_POLICY_PATH = BACKEND_DIR / "prompts" / "tool_policy.txt"
logger = logging.getLogger("meridian-backend")
logging.basicConfig(level=logging.INFO)


class SessionState(BaseModel):
    authenticated: bool = False
    email: str | None = None


class ChatRequest(BaseModel):
    message: str
    session: SessionState | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    reply: str
    session: SessionState
    request_id: str


class AuthVerifyRequest(BaseModel):
    email: str
    pin: str


class AuthVerifyResponse(BaseModel):
    authenticated: bool
    email: str | None
    message: str
    customer_context: dict[str, str] | None = None


@dataclass(frozen=True)
class Settings:
    model: str
    default_model: str
    fallback_model: str | None
    escalation_model: str | None
    escalation_enabled: bool
    escalation_on_order_auth: bool
    escalation_on_keywords: bool
    temperature: float
    max_tokens: int
    http_timeout_seconds: float
    tool_loop_limit: int
    max_user_message_chars: int
    max_tool_arguments_chars: int
    max_retries: int
    retry_backoff_seconds: float


class MCPService:
    def __init__(self, server_url: str, client: httpx.AsyncClient) -> None:
        self.server_url = server_url.rstrip("/")
        self.client = client
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
        }

    async def _jsonrpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.client.post(
            self.server_url,
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": "meridian-backend",
                "method": method,
                "params": params or {},
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise httpx.HTTPError(f"MCP JSON-RPC error: {payload['error']}")
        return payload.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        # Try REST-style MCP first.
        try:
            response = await self.client.get(f"{self.server_url}/list_tools", headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("tools", [])
        except httpx.HTTPError:
            # Fallback for Streamable HTTP MCP JSON-RPC endpoints (e.g. /mcp).
            result = await self._jsonrpc("tools/list")
            return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        # Try REST-style MCP first.
        try:
            response = await self.client.post(
                f"{self.server_url}/call_tool",
                headers=self.headers,
                json={"name": name, "arguments": arguments},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("content", data)
        except httpx.HTTPError:
            # Fallback for Streamable HTTP MCP JSON-RPC endpoints (e.g. /mcp).
            result = await self._jsonrpc(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                },
            )
            return result.get("content", result)


class OpenRouterService:
    def __init__(self, api_key: str, client: httpx.AsyncClient, settings: Settings) -> None:
        self.api_key = api_key
        self.client = client
        self.settings = settings

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": model or self.settings.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_tokens,
            },
        )
        response.raise_for_status()
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message")
        if not message:
            raise RuntimeError("OpenRouter returned no message")
        return message


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def is_order_tool(name: str) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in ORDER_TOOL_KEYWORDS)


def resolve_customer_context(
    email: str,
    profile_source: dict[str, Any] | None = None,
    order_source: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    fallback = CUSTOMER_CONTEXT_REGISTRY.get(email)
    if profile_source is None and order_source is None and fallback is None:
        return None

    first_name = (
        str(profile_source.get("first_name") or profile_source.get("name") or profile_source.get("customer_name") or "").strip()
        if profile_source
        else ""
    )
    last_order_id = str(
        (order_source or {}).get("last_order_id")
        or (order_source or {}).get("order_id")
        or (order_source or {}).get("latest_order_id")
        or (profile_source or {}).get("last_order_id")
        or (profile_source or {}).get("order_id")
        or (profile_source or {}).get("latest_order_id")
        or ""
    ).strip()
    last_order_status = str(
        (order_source or {}).get("last_order_status")
        or (order_source or {}).get("order_status")
        or (order_source or {}).get("latest_order_status")
        or (profile_source or {}).get("last_order_status")
        or (profile_source or {}).get("order_status")
        or (profile_source or {}).get("latest_order_status")
        or ""
    ).strip()
    primary_request = str(
        (profile_source or {}).get("primary_request")
        or (order_source or {}).get("primary_request")
        or (profile_source or {}).get("reason")
        or (order_source or {}).get("reason")
        or (profile_source or {}).get("intent")
        or (order_source or {}).get("intent")
        or ""
    ).strip()

    return {
        "first_name": first_name or (fallback.get("first_name") if fallback else "Customer"),
        "last_order_id": last_order_id or (fallback.get("last_order_id") if fallback else "N/A"),
        "last_order_status": last_order_status or (fallback.get("last_order_status") if fallback else "No recent order status found"),
        "primary_request": primary_request or (fallback.get("primary_request") if fallback else "General account support"),
    }


def first_tool_name(tools: list[dict[str, Any]], *keywords: str) -> str | None:
    for tool in tools:
        name = str(tool.get("name", "")).lower()
        if all(keyword in name for keyword in keywords):
            return str(tool.get("name"))
    return None


def normalize_tool_response(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    return None


def normalize_tool_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("orders", "items", "results", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def to_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted = []
    for tool in tools:
        formatted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return formatted


def parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {}


def parse_cors_origins() -> list[str]:
    value = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid int env var %s=%s. Falling back to %s.", name, value, default)
        return default


def parse_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float env var %s=%s. Falling back to %s.", name, value, default)
        return default


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid bool env var %s=%s. Falling back to %s.", name, value, default)
    return default


def parse_optional_str_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def unique_models(*models: str | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


def normalize_model_slug(model: str | None) -> str | None:
    if model is None:
        return None
    value = model.strip()
    return value or None


def load_settings() -> Settings:
    default_model = normalize_model_slug(
        parse_optional_str_env("OPENROUTER_DEFAULT_MODEL") or os.getenv("OPENROUTER_MODEL", OPENROUTER_MODEL)
    ) or SAFE_FALLBACK_MODEL
    fallback_model = normalize_model_slug(parse_optional_str_env("OPENROUTER_FALLBACK_MODEL"))
    escalation_model = normalize_model_slug(parse_optional_str_env("OPENROUTER_ESCALATION_MODEL"))
    return Settings(
        model=default_model,
        default_model=default_model,
        fallback_model=fallback_model,
        escalation_model=escalation_model,
        escalation_enabled=parse_bool_env("MODEL_ESCALATION_ENABLED", True),
        escalation_on_order_auth=parse_bool_env("MODEL_ESCALATE_ON_ORDER_AUTH", True),
        escalation_on_keywords=parse_bool_env("MODEL_ESCALATE_ON_KEYWORDS", True),
        temperature=parse_float_env("OPENROUTER_TEMPERATURE", 0.2),
        max_tokens=parse_int_env("OPENROUTER_MAX_TOKENS", 400),
        http_timeout_seconds=parse_float_env("HTTP_TIMEOUT_SECONDS", 30.0),
        tool_loop_limit=parse_int_env("TOOL_LOOP_LIMIT", 4),
        max_user_message_chars=parse_int_env("MAX_USER_MESSAGE_CHARS", 2000),
        max_tool_arguments_chars=parse_int_env("MAX_TOOL_ARGUMENTS_CHARS", 4000),
        max_retries=parse_int_env("HTTP_MAX_RETRIES", 2),
        retry_backoff_seconds=parse_float_env("HTTP_RETRY_BACKOFF_SECONDS", 0.5),
    )


def should_use_escalation(settings: Settings, *, user_message: str, authenticated: bool) -> bool:
    if not settings.escalation_enabled or not settings.escalation_model:
        return False
    lowered = user_message.lower()
    has_order_auth_need = settings.escalation_on_order_auth and ((not authenticated) and any(k in lowered for k in ORDER_TOOL_KEYWORDS))
    has_escalation_signal = settings.escalation_on_keywords and any(k in lowered for k in ESCALATION_KEYWORDS)
    return has_order_auth_need or has_escalation_signal


def select_model_chain(settings: Settings, *, use_escalation: bool) -> list[str]:
    if use_escalation:
        return unique_models(settings.escalation_model, settings.default_model, settings.fallback_model, SAFE_FALLBACK_MODEL)
    return unique_models(
        settings.default_model,
        settings.fallback_model,
        settings.escalation_model if settings.escalation_enabled else None,
        SAFE_FALLBACK_MODEL,
    )


def is_retryable_http_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.TimeoutException):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    return True


async def call_with_retries(coro_factory, *, retries: int, backoff_seconds: float):
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except (httpx.TimeoutException, httpx.HTTPError) as error:
            if isinstance(error, httpx.HTTPError) and not is_retryable_http_error(error):
                raise
            last_error = error
            if attempt == retries:
                raise
            wait_seconds = backoff_seconds * (2**attempt)
            logger.warning("External call failed (attempt %s/%s). Retrying in %.2fs", attempt + 1, retries + 1, wait_seconds)
            import asyncio

            await asyncio.sleep(wait_seconds)
    raise last_error if last_error else RuntimeError("Call failed")


async def fetch_customer_context_from_mcp(email: str, server_url: str) -> dict[str, str] | None:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        mcp = MCPService(server_url, client)
        tools = await mcp.list_tools()

        # Dedicated customer/profile tool.
        profile_tool = (
            first_tool_name(tools, "customer", "profile")
            or first_tool_name(tools, "customer", "details")
            or first_tool_name(tools, "lookup", "customer")
            or first_tool_name(tools, "get", "customer")
            or first_tool_name(tools, "customer")
        )
        # Dedicated latest-order tool.
        order_tool = (
            first_tool_name(tools, "latest", "order")
            or first_tool_name(tools, "recent", "order")
            or first_tool_name(tools, "order", "summary")
            or first_tool_name(tools, "order", "status")
            or first_tool_name(tools, "list", "orders")
            or first_tool_name(tools, "get", "order")
            or first_tool_name(tools, "orders")
        )
        if not profile_tool and not order_tool:
            return resolve_customer_context(email)

        candidate_args = [
            {"email": email},
            {"customer_email": email},
            {"user_email": email},
            {"customerId": email},
        ]
        async def fetch_payload(tool_name: str | None) -> dict[str, Any] | None:
            if not tool_name:
                return None
            for args in candidate_args:
                try:
                    raw = await mcp.call_tool(tool_name, args)
                except httpx.HTTPError:
                    continue
                payload = normalize_tool_response(raw)
                if payload is not None:
                    return payload
            return None

        profile_payload = await fetch_payload(profile_tool)
        order_payload = await fetch_payload(order_tool)

        # If we can list orders, derive latest order and hydrate details.
        if order_tool and "list" in order_tool.lower():
            order_list_payload: dict[str, Any] | None = order_payload
            order_items = normalize_tool_items(order_list_payload)
            if order_items:
                latest = order_items[0]
                latest_order_id = (
                    latest.get("order_id")
                    or latest.get("id")
                    or latest.get("orderId")
                    or latest.get("number")
                )
                if isinstance(latest_order_id, str) and latest_order_id.strip():
                    get_order_tool = (
                        first_tool_name(tools, "get", "order")
                        or first_tool_name(tools, "order", "details")
                    )
                    if get_order_tool:
                        detail_candidate_args = [
                            {"order_id": latest_order_id},
                            {"id": latest_order_id},
                            {"orderId": latest_order_id},
                        ]
                        for args in detail_candidate_args:
                            try:
                                detail_raw = await mcp.call_tool(get_order_tool, args)
                            except httpx.HTTPError:
                                continue
                            detail_payload = normalize_tool_response(detail_raw)
                            if detail_payload:
                                order_payload = {**latest, **detail_payload}
                                break
                        else:
                            order_payload = latest
                    else:
                        order_payload = latest

        return resolve_customer_context(email, profile_payload, order_payload)


app = FastAPI(title="Meridian AI Support Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/verify", response_model=AuthVerifyResponse)
async def verify_auth(payload: AuthVerifyRequest) -> AuthVerifyResponse:
    email = payload.email.strip().lower()
    pin = payload.pin.strip()
    authenticated = AUTH_REGISTRY.get(email) == pin
    if authenticated:
        customer_context = None
        mcp_server_url = os.getenv("MCP_SERVER_URL")
        if mcp_server_url:
            try:
                customer_context = await fetch_customer_context_from_mcp(email, mcp_server_url)
            except (httpx.TimeoutException, httpx.HTTPError) as error:
                logger.warning("MCP customer context lookup failed for %s: %s", email, str(error))
        if customer_context is None:
            customer_context = resolve_customer_context(email, None)
        return AuthVerifyResponse(
            authenticated=True,
            email=email,
            message="Verification successful. You can now access order-related support.",
            customer_context=customer_context,
        )
    return AuthVerifyResponse(
        authenticated=False,
        email=None,
        message="Verification failed. Please check your email and PIN.",
        customer_context=None,
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    request_id = str(uuid.uuid4())
    settings = load_settings()
    user_message = payload.message.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(user_message) > settings.max_user_message_chars:
        raise HTTPException(
            status_code=400,
            detail=f"message exceeds max length ({settings.max_user_message_chars} characters)",
        )
    if payload.stream:
        raise HTTPException(
            status_code=400,
            detail="stream=true is not supported for tool-calling mode yet. Use non-stream chat.",
        )

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
    mcp_server_url = os.getenv("MCP_SERVER_URL")
    if not openrouter_api_key or not mcp_server_url:
        raise HTTPException(status_code=500, detail="Missing OPENROUTER_API_KEY or MCP_SERVER_URL")

    system_prompt = load_prompt(SYSTEM_PROMPT_PATH)
    auth_instructions = load_prompt(AUTH_INSTRUCTIONS_PATH)
    tool_policy = load_prompt(TOOL_POLICY_PATH)

    session = payload.session or SessionState()
    authenticated = session.authenticated
    authenticated_email = session.email

    timeout = httpx.Timeout(settings.http_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        mcp = MCPService(mcp_server_url, client)
        llm = OpenRouterService(openrouter_api_key, client, settings)

        try:
            available_tools = await call_with_retries(
                lambda: mcp.list_tools(),
                retries=settings.max_retries,
                backoff_seconds=settings.retry_backoff_seconds,
            )
        except httpx.TimeoutException as error:
            raise HTTPException(status_code=504, detail="MCP timeout while listing tools") from error
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail="MCP failed while listing tools") from error

        tool_definitions = to_tool_definitions(available_tools)
        allowed_tool_names = {str(tool.get("name", "")) for tool in available_tools}

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system_prompt}\n\n{auth_instructions}\n\n{tool_policy}"},
            {"role": "user", "content": user_message},
        ]

        for _ in range(settings.tool_loop_limit):
            use_escalation = should_use_escalation(
                settings,
                user_message=user_message,
                authenticated=authenticated,
            )
            model_chain = select_model_chain(settings, use_escalation=use_escalation)
            last_llm_error: Exception | None = None
            assistant_message: dict[str, Any] | None = None
            for model in model_chain:
                try:
                    assistant_message = await call_with_retries(
                        lambda current_model=model: llm.chat(messages, tool_definitions, model=current_model),
                        retries=settings.max_retries,
                        backoff_seconds=settings.retry_backoff_seconds,
                    )
                    if model != settings.default_model:
                        logger.warning("Model fallback engaged: using '%s' for this turn.", model)
                    break
                except (httpx.TimeoutException, httpx.HTTPError) as error:
                    last_llm_error = error
                    logger.warning("OpenRouter call failed for model '%s': %s", model, str(error))
                    continue

            if assistant_message is None:
                if isinstance(last_llm_error, httpx.TimeoutException):
                    raise HTTPException(status_code=504, detail="OpenRouter timeout across model route") from last_llm_error
                raise HTTPException(status_code=502, detail="OpenRouter request failed across model route") from last_llm_error

            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                return ChatResponse(
                    reply=assistant_message.get("content") or "I could not generate a response.",
                    session=SessionState(authenticated=authenticated, email=authenticated_email),
                    request_id=request_id,
                )

            for call in tool_calls:
                tool_name = call.get("function", {}).get("name", "")
                if tool_name not in allowed_tool_names and tool_name != "verify_customer_pin":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "TOOL_NOT_ALLOWED",
                                    "message": f"Tool '{tool_name}' is not available for this session.",
                                }
                            ),
                        }
                    )
                    continue

                raw_args = str(call.get("function", {}).get("arguments", "{}"))
                if len(raw_args) > settings.max_tool_arguments_chars:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "TOOL_ARGS_TOO_LARGE",
                                    "message": "Tool arguments exceeded allowed size.",
                                }
                            ),
                        }
                    )
                    continue
                tool_args = parse_tool_args(raw_args)

                if tool_name == "verify_customer_pin":
                    email = str(tool_args.get("email", ""))
                    pin = str(tool_args.get("pin", ""))
                    has_credentials = bool(email and pin)
                    verified = AUTH_REGISTRY.get(email) == pin if has_credentials else False
                    # Preserve existing verified session if the model redundantly calls
                    # verify_customer_pin without valid credentials.
                    if verified:
                        authenticated = True
                        authenticated_email = email
                    elif not authenticated:
                        authenticated = False
                        authenticated_email = None
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "authenticated": authenticated,
                                    "email": authenticated_email,
                                    "already_authenticated": authenticated and not verified,
                                }
                            ),
                        }
                    )
                    continue

                if is_order_tool(tool_name) and not authenticated:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "AUTH_REQUIRED",
                                    "message": "Email and PIN verification is required before order-related actions.",
                                }
                            ),
                        }
                    )
                    continue

                try:
                    tool_result = await call_with_retries(
                        lambda: mcp.call_tool(tool_name, tool_args),
                        retries=settings.max_retries,
                        backoff_seconds=settings.retry_backoff_seconds,
                    )
                except httpx.TimeoutException as error:
                    raise HTTPException(status_code=504, detail=f"MCP timeout on tool '{tool_name}'") from error
                except httpx.HTTPError as error:
                    raise HTTPException(status_code=502, detail=f"MCP failed on tool '{tool_name}'") from error
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": tool_name,
                        "content": json.dumps(tool_result),
                    }
                )

    return ChatResponse(
        reply="I could not complete the request after multiple tool attempts.",
        session=SessionState(authenticated=authenticated, email=authenticated_email),
        request_id=request_id,
    )
