from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
try:
    import redis
except Exception:  # pragma: no cover - optional dependency fallback
    redis = None


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
SENSITIVE_TOOL_NAMES = {"get_customer", "list_orders", "get_order", "create_order"}
ORDER_MUTATION_CONFIRM_KEYWORDS = ("confirm", "place order", "submit order", "yes create")

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")
SYSTEM_PROMPT_PATH = BACKEND_DIR / "prompts" / "system_prompt.txt"
AUTH_INSTRUCTIONS_PATH = BACKEND_DIR / "prompts" / "auth_instructions.txt"
TOOL_POLICY_PATH = BACKEND_DIR / "prompts" / "tool_policy.txt"
logger = logging.getLogger("meridian-backend")
logging.basicConfig(level=logging.INFO)

# Revoked token IDs (jti) until their original exp; in-process only (single-instance / dev).
_REVOKED_JTIS: dict[str, int] = {}
_REVOCATION_REDIS_CLIENT: Any | None = None
_REVOCATION_REDIS_INIT_ATTEMPTED = False
_RATE_BUCKETS: dict[tuple[str, str], deque[float]] = {}
_RATE_LOCK = Lock()


class SessionState(BaseModel):
    authenticated: bool = False
    email: str | None = None


class ChatRequest(BaseModel):
    message: str
    session: SessionState | None = None
    auth_token: str | None = None
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
    auth_token: str | None = None
    auth_token_expires_in: int | None = None


class AuthTokenBody(BaseModel):
    auth_token: str


class AuthRefreshResponse(BaseModel):
    authenticated: bool
    email: str | None
    message: str
    auth_token: str | None = None
    auth_token_expires_in: int | None = None


class CapabilitiesResponse(BaseModel):
    tools: list[str]
    helper_message: str
    suggested_prompts: list[str]


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


def filter_tools_for_session(tools: list[dict[str, Any]], authenticated: bool) -> list[dict[str, Any]]:
    if not authenticated:
        return tools
    return [tool for tool in tools if str(tool.get("name", "")).lower() != "verify_customer_pin"]


def is_uuid_like(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    try:
        uuid.UUID(candidate)
        return True
    except ValueError:
        return False


def extract_customer_id(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    for key in ("customer_id", "customerId", "id", "uuid", "customer_uuid", "customerUuid"):
        value = payload.get(key)
        if is_uuid_like(value):
            return str(value).strip()
    return None


async def resolve_authenticated_customer_id(
    mcp: MCPService,
    tools: list[dict[str, Any]],
    authenticated_email: str | None,
) -> str | None:
    if not authenticated_email:
        return None
    customer_lookup_tool = (
        first_tool_name(tools, "get", "customer")
        or first_tool_name(tools, "customer", "details")
        or first_tool_name(tools, "customer", "profile")
        or first_tool_name(tools, "lookup", "customer")
        or first_tool_name(tools, "customer")
    )
    if not customer_lookup_tool:
        return None

    lookup_args = [
        {"email": authenticated_email},
        {"customer_email": authenticated_email},
        {"user_email": authenticated_email},
    ]
    for args in lookup_args:
        try:
            raw = await mcp.call_tool(customer_lookup_tool, args)
        except httpx.HTTPError:
            continue
        payload = normalize_tool_response(raw)
        customer_id = extract_customer_id(payload)
        if customer_id:
            return customer_id

    # Some MCP schemas require customer_id for get_customer but allow list_orders
    # without customer_id. Try deriving customer_id from order rows tied to email.
    list_orders_tool = first_tool_name(tools, "list", "orders")
    if not list_orders_tool:
        return None
    try:
        order_raw = await mcp.call_tool(list_orders_tool, {})
    except httpx.HTTPError:
        return None
    for item in normalize_tool_items(order_raw):
        email_value = str(item.get("email") or item.get("customer_email") or "").strip().lower()
        if email_value and email_value == authenticated_email.strip().lower():
            customer_id = extract_customer_id(item)
            if customer_id:
                return customer_id
    return None


def inject_authenticated_identity_args(
    tool_name: str,
    tool_args: dict[str, Any],
    authenticated_email: str | None,
    authenticated_customer_id: str | None,
) -> dict[str, Any]:
    if not is_order_tool(tool_name):
        return tool_args
    enriched = dict(tool_args)
    if authenticated_email and not enriched.get("email"):
        enriched["email"] = authenticated_email
    customer_id_keys = ("customer_id", "customerId", "customer_uuid", "customerUuid")
    for key in customer_id_keys:
        current = enriched.get(key)
        if isinstance(current, str) and "@" in current:
            # MCP expects UUID-like customer identifiers, never email strings.
            enriched.pop(key, None)
    if authenticated_customer_id:
        for key in customer_id_keys:
            current = enriched.get(key)
            if current is None or (isinstance(current, str) and not current.strip()):
                enriched[key] = authenticated_customer_id
    return enriched


def required_tool_args(tool_schema: dict[str, Any] | None) -> list[str]:
    if not isinstance(tool_schema, dict):
        return []
    required = tool_schema.get("required")
    if isinstance(required, list):
        return [str(item) for item in required if isinstance(item, str)]
    return []


def tool_arg_schema_for_field(tool_schema: dict[str, Any] | None, field: str) -> dict[str, Any] | None:
    if not isinstance(tool_schema, dict):
        return None
    properties = tool_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    schema = properties.get(field)
    return schema if isinstance(schema, dict) else None


def expected_types_for_field(field_schema: dict[str, Any] | None) -> set[str]:
    if not isinstance(field_schema, dict):
        return set()
    expected: set[str] = set()
    schema_type = field_schema.get("type")
    if isinstance(schema_type, str):
        expected.add(schema_type)
    any_of = field_schema.get("anyOf")
    if isinstance(any_of, list):
        for variant in any_of:
            if isinstance(variant, dict):
                variant_type = variant.get("type")
                if isinstance(variant_type, str):
                    expected.add(variant_type)
    return expected


def type_matches_schema(value: Any, expected_types: set[str]) -> bool:
    if not expected_types:
        return True
    type_map = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }
    return any(checker(value) for key, checker in type_map.items() if key in expected_types)


def invalid_required_args(tool_args: dict[str, Any], tool_schema: dict[str, Any] | None) -> list[str]:
    invalid: list[str] = []
    for field in required_tool_args(tool_schema):
        if field not in tool_args:
            invalid.append(field)
            continue
        value = tool_args.get(field)
        if value is None:
            invalid.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            invalid.append(field)
            continue
        if isinstance(value, list) and len(value) == 0:
            invalid.append(field)
            continue
        field_schema = tool_arg_schema_for_field(tool_schema, field)
        expected = expected_types_for_field(field_schema)
        if not type_matches_schema(value, expected):
            invalid.append(field)
    return invalid


def needs_order_confirmation(user_message: str) -> bool:
    lowered = user_message.lower()
    return not any(keyword in lowered for keyword in ORDER_MUTATION_CONFIRM_KEYWORDS)


def parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {}


def is_redundant_verification_prompt(reply: str) -> bool:
    lowered = reply.lower()
    asks_email = "email" in lowered
    asks_pin = "pin" in lowered or "4-digit" in lowered or "4 digit" in lowered
    asks_verify = "verify" in lowered or "verification" in lowered
    return asks_email and asks_pin and asks_verify


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


def auth_token_secret() -> str:
    configured = os.getenv("AUTH_TOKEN_SECRET")
    environment = os.getenv("ENV", os.getenv("ENVIRONMENT", "development")).strip().lower()
    if configured and configured.strip():
        return configured.strip()
    if environment in {"prod", "production"}:
        raise RuntimeError("AUTH_TOKEN_SECRET is required in production")
    return "local-dev-auth-secret-change-me"


def auth_token_audience() -> str:
    return (os.getenv("AUTH_TOKEN_AUDIENCE") or "meridian-support").strip() or "meridian-support"


def auth_token_ttl_seconds() -> int:
    return parse_int_env("AUTH_TOKEN_TTL_SECONDS", 3600)


def revocation_redis_url() -> str | None:
    return parse_optional_str_env("AUTH_REVOCATION_REDIS_URL") or parse_optional_str_env("REDIS_URL")


def revocation_redis_prefix() -> str:
    return parse_optional_str_env("AUTH_REVOCATION_REDIS_PREFIX") or "auth:revoked_jti:"


def get_revocation_redis_client() -> Any | None:
    global _REVOCATION_REDIS_CLIENT, _REVOCATION_REDIS_INIT_ATTEMPTED
    if _REVOCATION_REDIS_CLIENT is not None:
        return _REVOCATION_REDIS_CLIENT
    if _REVOCATION_REDIS_INIT_ATTEMPTED:
        return None
    _REVOCATION_REDIS_INIT_ATTEMPTED = True
    redis_url = revocation_redis_url()
    if not redis_url or redis is None:
        return None
    try:
        _REVOCATION_REDIS_CLIENT = redis.from_url(redis_url, decode_responses=True)
        return _REVOCATION_REDIS_CLIENT
    except Exception as error:
        logger.warning("Redis revocation store unavailable; falling back to in-memory denylist: %s", str(error))
        _REVOCATION_REDIS_CLIENT = None
        return None


def _prune_revoked_jtis() -> None:
    now = int(time.time())
    stale = [jti for jti, exp in _REVOKED_JTIS.items() if exp < now]
    for jti in stale:
        _REVOKED_JTIS.pop(jti, None)


def revoke_auth_token_jti(jti: str, exp: int) -> None:
    now = int(time.time())
    ttl = max(exp - now, 1)
    redis_client = get_revocation_redis_client()
    if redis_client is not None:
        key = f"{revocation_redis_prefix()}{jti}"
        try:
            redis_client.setex(key, ttl, "1")
            return
        except Exception as error:
            logger.warning("Redis revoke write failed; falling back to in-memory denylist: %s", str(error))
    _prune_revoked_jtis()
    _REVOKED_JTIS[jti] = exp


def is_auth_token_jti_revoked(jti: str) -> bool:
    redis_client = get_revocation_redis_client()
    if redis_client is not None:
        key = f"{revocation_redis_prefix()}{jti}"
        try:
            return bool(redis_client.exists(key))
        except Exception as error:
            logger.warning("Redis revoke lookup failed; falling back to in-memory denylist: %s", str(error))
    _prune_revoked_jtis()
    return jti in _REVOKED_JTIS


def create_auth_token(email: str, *, ttl_seconds: int | None = None) -> str:
    if ttl_seconds is None:
        ttl_seconds = auth_token_ttl_seconds()
    now = int(time.time())
    exp = now + int(ttl_seconds)
    audience = auth_token_audience()
    claims = {
        "sub": email.strip().lower(),
        "iat": now,
        "exp": exp,
        "aud": audience,
        "jti": str(uuid.uuid4()),
    }
    body = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(auth_token_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    token_raw = f"{body}|{signature}"
    return base64.urlsafe_b64encode(token_raw.encode("utf-8")).decode("utf-8")


def _validate_legacy_auth_token(decoded: str) -> tuple[bool, str | None]:
    try:
        email, exp_raw, signature = decoded.split("|", 2)
        exp = int(exp_raw)
    except Exception:
        return False, None
    now = int(time.time())
    if exp < now:
        return False, None
    payload = f"{email}|{exp}"
    expected = hmac.new(auth_token_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False, None
    return True, email.strip().lower()


def _validate_v2_auth_token(decoded: str) -> tuple[bool, str | None]:
    try:
        body, signature = decoded.rsplit("|", 1)
        if not body.startswith("{"):
            return False, None
        expected = hmac.new(auth_token_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False, None
        data = json.loads(body)
        email = data.get("sub")
        exp = int(data.get("exp", 0))
        iat = int(data.get("iat", 0))
        aud = data.get("aud")
        jti = data.get("jti")
        now = int(time.time())
        if not isinstance(email, str) or not email.strip():
            return False, None
        if exp < now or iat > now:
            return False, None
        if aud != auth_token_audience():
            return False, None
        if not isinstance(jti, str) or not jti.strip():
            return False, None
        if is_auth_token_jti_revoked(jti):
            return False, None
        return True, email.strip().lower()
    except Exception:
        return False, None


def validate_auth_token(token: str | None) -> tuple[bool, str | None]:
    if not token:
        return False, None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return False, None
    if "|" in decoded and decoded.strip().startswith("{"):
        return _validate_v2_auth_token(decoded)
    return _validate_legacy_auth_token(decoded)


def unique_models(*models: str | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


def get_rate_limit_window_seconds() -> int:
    return max(1, parse_int_env("RATE_LIMIT_WINDOW_SECONDS", 60))


def get_rate_limit_for_path(path: str) -> int:
    default_limit = max(1, parse_int_env("RATE_LIMIT_DEFAULT_REQUESTS", 120))
    endpoint_overrides = {
        "/health": max(1, parse_int_env("RATE_LIMIT_HEALTH_REQUESTS", 300)),
        "/capabilities": max(1, parse_int_env("RATE_LIMIT_CAPABILITIES_REQUESTS", 120)),
        "/auth/verify": max(1, parse_int_env("RATE_LIMIT_AUTH_VERIFY_REQUESTS", 20)),
        "/auth/refresh": max(1, parse_int_env("RATE_LIMIT_AUTH_REFRESH_REQUESTS", 60)),
        "/auth/logout": max(1, parse_int_env("RATE_LIMIT_AUTH_LOGOUT_REQUESTS", 60)),
        "/chat": max(1, parse_int_env("RATE_LIMIT_CHAT_REQUESTS", 30)),
    }
    return endpoint_overrides.get(path, default_limit)


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


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


def build_capabilities_payload(tool_names: list[str]) -> CapabilitiesResponse:
    lowered = {name.lower() for name in tool_names}
    capabilities: list[str] = []

    can_search_products = any("search_products" in name or "list_products" in name for name in lowered)
    can_product_details = any("get_product" in name for name in lowered)
    can_customer_lookup = any("get_customer" in name for name in lowered)
    can_verify_pin = any("verify_customer_pin" in name for name in lowered)
    can_list_orders = any("list_orders" in name for name in lowered)
    can_get_order = any("get_order" in name for name in lowered)
    can_create_order = any("create_order" in name for name in lowered)

    if can_search_products:
        capabilities.append("search products")
    if can_product_details:
        capabilities.append("check product details")
    if can_customer_lookup:
        capabilities.append("look up customer account information")
    if can_verify_pin:
        capabilities.append("verify your account PIN")
    if can_list_orders or can_get_order:
        capabilities.append("look up orders")
    if can_create_order:
        capabilities.append("create new orders")

    suggestions: list[str] = []
    if can_search_products:
        suggestions.append("Search products for wireless noise-cancelling headphones")
    if can_product_details:
        suggestions.append("Get product details for iPhone 15 Pro")
    if can_list_orders:
        suggestions.append("Show my recent orders")
    if can_get_order:
        suggestions.append("Track my recent order")
    if can_create_order:
        suggestions.append("Create a new order for 1 unit of iPhone 15 Pro")
    if not suggestions:
        suggestions = ["How can you help me today?"]

    if capabilities:
        if len(capabilities) == 1:
            capabilities_text = capabilities[0]
        elif len(capabilities) == 2:
            capabilities_text = f"{capabilities[0]} and {capabilities[1]}"
        else:
            capabilities_text = f"{', '.join(capabilities[:-1])}, and {capabilities[-1]}"
        helper = f"I can {capabilities_text} right now."
    else:
        helper = "I can answer general support questions right now."

    return CapabilitiesResponse(
        tools=tool_names,
        helper_message=helper,
        suggested_prompts=suggestions,
    )


app = FastAPI(title="Meridian AI Support Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in {"/openapi.json", "/docs", "/redoc"}:
        return await call_next(request)

    window_seconds = get_rate_limit_window_seconds()
    limit = get_rate_limit_for_path(request.url.path)
    client_ip = get_client_ip(request)
    key = (client_ip, request.url.path)
    now = time.time()

    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(key, deque())
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMITED",
                    "message": "Too many requests. Please retry later.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        remaining = max(0, limit - len(bucket))

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Window-Seconds"] = str(window_seconds)
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    mcp_server_url = os.getenv("MCP_SERVER_URL")
    if not mcp_server_url:
        return build_capabilities_payload([])

    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        mcp = MCPService(mcp_server_url, client)
        try:
            tools = await mcp.list_tools()
        except (httpx.TimeoutException, httpx.HTTPError):
            return build_capabilities_payload([])

    tool_names = [str(tool.get("name", "")).strip() for tool in tools if str(tool.get("name", "")).strip()]
    return build_capabilities_payload(tool_names)


@app.post("/auth/verify", response_model=AuthVerifyResponse)
async def verify_auth(payload: AuthVerifyRequest) -> AuthVerifyResponse:
    email = payload.email.strip().lower()
    pin = payload.pin.strip()
    authenticated = AUTH_REGISTRY.get(email) == pin
    if authenticated:
        token_ttl_seconds = auth_token_ttl_seconds()
        token = create_auth_token(email, ttl_seconds=token_ttl_seconds)
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
            auth_token=token,
            auth_token_expires_in=token_ttl_seconds,
        )
    return AuthVerifyResponse(
        authenticated=False,
        email=None,
        message="Verification failed. Please check your email and PIN.",
        customer_context=None,
        auth_token=None,
        auth_token_expires_in=None,
    )


def _extract_v2_jti_and_exp(token: str) -> tuple[str | None, int]:
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        if not decoded.strip().startswith("{"):
            return None, 0
        body, _sig = decoded.rsplit("|", 1)
        data = json.loads(body)
        jti = data.get("jti")
        exp = int(data.get("exp", 0))
        if isinstance(jti, str) and jti.strip():
            return jti.strip(), exp
    except Exception:
        pass
    return None, 0


@app.post("/auth/refresh", response_model=AuthRefreshResponse)
async def refresh_auth(payload: AuthTokenBody) -> AuthRefreshResponse:
    ok, email = validate_auth_token(payload.auth_token)
    if not ok or not email:
        return AuthRefreshResponse(
            authenticated=False,
            email=None,
            message="Invalid or expired session. Please verify again.",
            auth_token=None,
            auth_token_expires_in=None,
        )
    ttl = auth_token_ttl_seconds()
    new_token = create_auth_token(email, ttl_seconds=ttl)
    old_jti, old_exp = _extract_v2_jti_and_exp(payload.auth_token)
    if old_jti:
        revoke_auth_token_jti(old_jti, old_exp)
    return AuthRefreshResponse(
        authenticated=True,
        email=email,
        message="Session refreshed.",
        auth_token=new_token,
        auth_token_expires_in=ttl,
    )


@app.post("/auth/logout")
async def logout_auth(payload: AuthTokenBody) -> dict[str, bool]:
    ok, _email = validate_auth_token(payload.auth_token)
    if ok:
        old_jti, old_exp = _extract_v2_jti_and_exp(payload.auth_token)
        if old_jti:
            revoke_auth_token_jti(old_jti, old_exp)
    return {"ok": True}


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
    token_authenticated, token_email = validate_auth_token(payload.auth_token)
    authenticated = token_authenticated
    authenticated_email = token_email

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

        session_tools = filter_tools_for_session(available_tools, authenticated=authenticated)
        tool_definitions = to_tool_definitions(session_tools)
        allowed_tool_names = {str(tool.get("name", "")) for tool in session_tools}
        tool_specs_by_name = {
            str(tool.get("name", "")): tool
            for tool in session_tools
            if str(tool.get("name", ""))
        }
        if authenticated:
            allowed_tool_names.add("verify_customer_pin")
        authenticated_customer_id = None
        if authenticated:
            authenticated_customer_id = await resolve_authenticated_customer_id(
                mcp, available_tools, authenticated_email
            )

        session_context = (
            f"Session state: authenticated={authenticated}, email={authenticated_email or 'unknown'}.\n"
            "If authenticated is true, do NOT ask for email/PIN again unless the customer explicitly asks to re-verify."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system_prompt}\n\n{auth_instructions}\n\n{tool_policy}"},
            {"role": "system", "content": session_context},
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
                reply_text = assistant_message.get("content") or "I could not generate a response."
                if authenticated and is_redundant_verification_prompt(reply_text):
                    reply_text = (
                        "You are already verified. I can help with order tracking right away. "
                        "Please share your order ID or ask me to show your recent orders."
                    )
                return ChatResponse(
                    reply=reply_text,
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
                tool_args = inject_authenticated_identity_args(
                    tool_name,
                    tool_args,
                    authenticated_email=authenticated_email,
                    authenticated_customer_id=authenticated_customer_id,
                )

                if tool_name in SENSITIVE_TOOL_NAMES and not authenticated:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "AUTH_REQUIRED",
                                    "message": "Please verify your email and PIN before account or order actions.",
                                }
                            ),
                        }
                    )
                    continue

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
                        authenticated_customer_id = await resolve_authenticated_customer_id(
                            mcp, available_tools, authenticated_email
                        )
                    elif not authenticated:
                        authenticated = False
                        authenticated_email = None
                        authenticated_customer_id = None
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

                tool_spec = tool_specs_by_name.get(tool_name)
                schema = (tool_spec or {}).get("input_schema") or (tool_spec or {}).get("inputSchema")
                invalid_required = invalid_required_args(tool_args, schema)
                if invalid_required:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "INVALID_REQUIRED_ARGS",
                                    "message": f"Required argument(s) missing/invalid: {', '.join(invalid_required)}",
                                }
                            ),
                        }
                    )
                    continue

                if tool_name == "create_order" and needs_order_confirmation(user_message):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "error": "ORDER_CONFIRMATION_REQUIRED",
                                    "message": "Please confirm you want to place this order before I create it.",
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
