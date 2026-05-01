import base64
import hashlib
import hmac
import json
import time

import httpx
from fastapi.testclient import TestClient

from backend.app import main


client = TestClient(main.app)


def issue_token(email: str) -> str:
    return main.create_auth_token(email, ttl_seconds=3600)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["revocation_backend"] in {"memory", "redis"}


def test_capabilities_returns_live_tool_prompts(monkeypatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {"name": "search_products"},
            {"name": "list_orders"},
            {"name": "create_order"},
        ]

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)

    response = client.get("/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert "search_products" in payload["tools"]
    assert any("Search products" in prompt for prompt in payload["suggested_prompts"])
    assert any("Show my recent orders" == prompt for prompt in payload["suggested_prompts"])
    assert any("Create a new order" in prompt for prompt in payload["suggested_prompts"])
    assert payload["helper_message"] == "I can search products, look up orders, and create new orders right now."


def test_auth_verify_success() -> None:
    response = client.post(
        "/auth/verify",
        json={"email": "donaldgarcia@example.net", "pin": "7912"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["email"] == "donaldgarcia@example.net"
    assert payload["customer_context"]["last_order_id"] == "A100"
    assert payload["auth_token"]


def test_auth_verify_failure() -> None:
    response = client.post(
        "/auth/verify",
        json={"email": "donaldgarcia@example.net", "pin": "0000"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is False
    assert payload["email"] is None
    assert payload["customer_context"] is None


def test_auth_verify_prefers_mcp_customer_context(monkeypatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_customer_profile", "input_schema": {"type": "object"}}]

    async def fake_call_tool(self, name, arguments):
        assert name == "get_customer_profile"
        assert arguments.get("email") == "donaldgarcia@example.net"
        return {
            "first_name": "Don",
            "last_order_id": "LIVE-700",
            "last_order_status": "Out for delivery",
            "primary_request": "Check delivery ETA",
        }

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)

    response = client.post(
        "/auth/verify",
        json={"email": "donaldgarcia@example.net", "pin": "7912"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["customer_context"]["first_name"] == "Don"
    assert payload["customer_context"]["last_order_id"] == "LIVE-700"


def test_auth_verify_merges_profile_and_order_tools(monkeypatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {"name": "get_customer_profile", "input_schema": {"type": "object"}},
            {"name": "get_latest_order_status", "input_schema": {"type": "object"}},
        ]

    async def fake_call_tool(self, name, arguments):
        assert arguments.get("email") == "donaldgarcia@example.net"
        if name == "get_customer_profile":
            return {
                "first_name": "Donald",
                "primary_request": "Track my latest order",
            }
        if name == "get_latest_order_status":
            return {
                "order_id": "LIVE-999",
                "order_status": "Delivered today",
            }
        raise AssertionError(f"Unexpected tool call: {name}")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)

    response = client.post(
        "/auth/verify",
        json={"email": "donaldgarcia@example.net", "pin": "7912"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["customer_context"]["first_name"] == "Donald"
    assert payload["customer_context"]["last_order_id"] == "LIVE-999"
    assert payload["customer_context"]["last_order_status"] == "Delivered today"
    assert payload["customer_context"]["primary_request"] == "Track my latest order"


def test_auth_verify_uses_realistic_mcp_tool_names(monkeypatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {"name": "get_customer", "input_schema": {"type": "object"}},
            {"name": "list_orders", "input_schema": {"type": "object"}},
            {"name": "get_order", "input_schema": {"type": "object"}},
        ]

    async def fake_call_tool(self, name, arguments):
        if name == "get_customer":
            return {"first_name": "Donald", "primary_request": "Track my recent order"}
        if name == "list_orders":
            return {"orders": [{"order_id": "ORD-123", "order_status": "Processing"}]}
        if name == "get_order":
            assert arguments.get("order_id") == "ORD-123"
            return {"order_status": "Out for delivery"}
        raise AssertionError(f"Unexpected tool: {name}")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)

    response = client.post(
        "/auth/verify",
        json={"email": "donaldgarcia@example.net", "pin": "7912"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["customer_context"]["first_name"] == "Donald"
    assert payload["customer_context"]["last_order_id"] == "ORD-123"
    assert payload["customer_context"]["last_order_status"] == "Out for delivery"


def test_chat_blocks_order_tool_without_auth(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_order_status", "description": "order status", "input_schema": {"type": "object"}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_order_status", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "Please verify email and PIN first."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fail_if_called(self, name, arguments):
        raise AssertionError("Order tool should not be called before authentication")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fail_if_called)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "Track my order"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "Please verify email and PIN first."
    assert payload["session"]["authenticated"] is False
    assert payload["request_id"]


def test_chat_ignores_client_authenticated_flag_without_token(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_order_status", "input_schema": {"type": "object"}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_order_status", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "Please verify email and PIN first."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fail_if_called(self, name, arguments):
        raise AssertionError("Order tool should not be called without valid token auth")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fail_if_called)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post(
        "/chat",
        json={
            "message": "track my order",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": None,
        },
    )
    assert response.status_code == 200
    assert response.json()["session"]["authenticated"] is False


def test_chat_hides_verify_tool_when_session_already_authenticated(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {"name": "verify_customer_pin", "input_schema": {"type": "object"}},
            {"name": "get_order_status", "input_schema": {"type": "object"}},
        ]

    async def fake_chat(self, messages, tools, *, model=None):
        tool_names = [entry.get("function", {}).get("name") for entry in tools]
        assert "verify_customer_pin" not in tool_names
        assert "get_order_status" in tool_names
        return {"role": "assistant", "content": "Order A100 is in transit."}

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "track my recent order",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["authenticated"] is True
    assert payload["session"]["email"] == "donaldgarcia@example.net"


def test_chat_rewrites_email_like_customer_id_to_uuid_for_order_tools(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    expected_customer_uuid = "83f08f88-b8f9-4424-a0d2-4f24195ff6ac"

    async def fake_list_tools(self):
        return [
            {"name": "get_customer", "input_schema": {"type": "object"}},
            {"name": "list_orders", "input_schema": {"type": "object"}},
        ]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "list_orders",
                        "arguments": '{"customer_id":"donaldgarcia@example.net"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "I found your recent orders."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fake_call_tool(self, name, arguments):
        if name == "get_customer":
            return {"customer_id": expected_customer_uuid}
        if name == "list_orders":
            assert arguments.get("customer_id") == expected_customer_uuid
            return {"orders": [{"order_id": "A100", "order_status": "Processing"}]}
        raise AssertionError(f"Unexpected tool call: {name}")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "show my recent orders",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["authenticated"] is True


def test_chat_drops_email_from_customer_id_for_list_orders_schema_variant(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {
                "name": "list_orders",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                        "status": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                    },
                },
            },
            {
                "name": "get_customer",
                "input_schema": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
            },
        ]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "list_orders",
                        "arguments": '{"customer_id":"donaldgarcia@example.net"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "Here are your recent orders."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fake_call_tool(self, name, arguments):
        if name == "get_customer":
            # Current MCP schema variant requires customer_id, so email lookup fails.
            request = httpx.Request("POST", "https://mcp.example.com")
            response = httpx.Response(status_code=400, request=request)
            raise httpx.HTTPStatusError("bad customer_id", request=request, response=response)
        if name == "list_orders":
            # Backend should sanitize email-like customer_id and drop it.
            assert "customer_id" not in arguments
            return {"orders": [{"order_id": "A100", "order_status": "Processing"}]}
        raise AssertionError(f"Unexpected tool call: {name}")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "show my recent orders",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["authenticated"] is True


def test_chat_overrides_redundant_pin_prompt_for_authenticated_user(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_order", "input_schema": {"type": "object"}}]

    async def fake_chat(self, messages, tools, *, model=None):
        return {
            "role": "assistant",
            "content": "To assist you with tracking your order, please provide your email address and 4-digit PIN for verification.",
        }

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "Track my recent order",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "already verified" in payload["reply"].lower()
    assert payload["session"]["authenticated"] is True


def test_chat_blocks_sensitive_customer_tool_without_auth(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_customer", "input_schema": {"type": "object", "required": ["customer_id"]}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-customer",
                    "type": "function",
                    "function": {"name": "get_customer", "arguments": '{"customer_id":"abc"}'},
                }
            ],
        },
        {"role": "assistant", "content": "Please verify first."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fail_if_called(self, name, arguments):
        raise AssertionError("Sensitive tool should not be called before auth")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fail_if_called)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "show customer details"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Please verify first."


def test_chat_requires_explicit_confirmation_before_create_order(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {
                "name": "create_order",
                "input_schema": {"type": "object", "required": ["customer_id", "items"]},
            }
        ]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-create",
                    "type": "function",
                    "function": {"name": "create_order", "arguments": '{"customer_id":"abc","items":[{"sku":"COM-0001","quantity":1}]}'},
                }
            ],
        },
        {"role": "assistant", "content": "Please confirm order creation first."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fail_if_called(self, name, arguments):
        raise AssertionError("create_order should be blocked until explicit confirmation")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fail_if_called)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "create an order for me",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "Please confirm order creation first."


def test_chat_blocks_required_args_when_empty_or_wrong_type(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [
            {
                "name": "create_order",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "items": {"type": "array"},
                    },
                    "required": ["customer_id", "items"],
                },
            }
        ]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-create-bad",
                    "type": "function",
                    "function": {
                        "name": "create_order",
                        "arguments": '{"customer_id":"   ","items":"not-an-array"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "Please provide valid order details."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    async def fail_if_called(self, name, arguments):
        raise AssertionError("create_order should not run with invalid required args")

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fail_if_called)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "yes confirm create order now",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "Please provide valid order details."


def test_auth_token_secret_required_in_production(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("ENV", "production")
    try:
        try:
            main.create_auth_token("donaldgarcia@example.net", ttl_seconds=60)
            raise AssertionError("Expected runtime error for missing production auth token secret")
        except RuntimeError as error:
            assert "AUTH_TOKEN_SECRET" in str(error)
    finally:
        monkeypatch.delenv("ENV", raising=False)


def test_auth_refresh_revokes_previous_token(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "rotate-secret")
    monkeypatch.delenv("AUTH_TOKEN_AUDIENCE", raising=False)
    first = main.create_auth_token("u@example.com", ttl_seconds=3600)
    assert main.validate_auth_token(first)[0] is True
    response = client.post("/auth/refresh", json={"auth_token": first})
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["auth_token"]
    assert payload["auth_token"] != first
    assert main.validate_auth_token(first)[0] is False
    assert main.validate_auth_token(payload["auth_token"])[0] is True


def test_auth_refresh_uses_redis_revocation_store_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "redis-secret")
    monkeypatch.delenv("AUTH_TOKEN_AUDIENCE", raising=False)

    class FakeRedis:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def setex(self, key: str, ttl: int, value: str) -> None:
            assert ttl > 0
            self.data[key] = value

        def exists(self, key: str) -> int:
            return 1 if key in self.data else 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "get_revocation_redis_client", lambda: fake_redis)
    monkeypatch.setattr(main, "revocation_redis_prefix", lambda: "auth:revoked_jti:")

    first = main.create_auth_token("redis@example.com", ttl_seconds=3600)
    response = client.post("/auth/refresh", json={"auth_token": first})
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True

    old_jti, _old_exp = main._extract_v2_jti_and_exp(first)
    assert old_jti is not None
    assert f"auth:revoked_jti:{old_jti}" in fake_redis.data
    assert main.validate_auth_token(first)[0] is False


def test_auth_logout_revokes_token(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "logout-secret")
    monkeypatch.delenv("AUTH_TOKEN_AUDIENCE", raising=False)
    token = main.create_auth_token("u@example.com", ttl_seconds=3600)
    assert main.validate_auth_token(token)[0] is True
    response = client.post("/auth/logout", json={"auth_token": token})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert main.validate_auth_token(token)[0] is False


def test_auth_token_wrong_audience_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "aud-secret")
    monkeypatch.delenv("AUTH_TOKEN_AUDIENCE", raising=False)
    token = main.create_auth_token("x@y.com", ttl_seconds=600)
    assert main.validate_auth_token(token)[0] is True
    monkeypatch.setenv("AUTH_TOKEN_AUDIENCE", "other-audience")
    assert main.validate_auth_token(token)[0] is False


def test_legacy_hmac_auth_token_still_accepted(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "legacy-sec")
    email = "legacy@example.com"
    exp = int(time.time()) + 600
    payload = f"{email}|{exp}"
    signature = hmac.new(b"legacy-sec", payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{email}|{exp}|{signature}"
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")
    ok, parsed = main.validate_auth_token(token)
    assert ok is True
    assert parsed == email


def test_v2_token_contains_jti_iat_aud(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "v2-secret")
    monkeypatch.delenv("AUTH_TOKEN_AUDIENCE", raising=False)
    token = main.create_auth_token("v2@example.com", ttl_seconds=120)
    decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
    body, sig = decoded.rsplit("|", 1)
    claims = json.loads(body)
    assert claims["sub"] == "v2@example.com"
    assert claims["aud"] == "meridian-support"
    assert "jti" in claims and claims["jti"]
    assert "iat" in claims and "exp" in claims
    assert isinstance(sig, str) and len(sig) == 64


def test_chat_rejects_stream_mode() -> None:
    response = client.post("/chat", json={"message": "hello", "stream": True})
    assert response.status_code == 400
    assert "stream=true" in response.json()["detail"]


def test_chat_rejects_too_long_message(monkeypatch) -> None:
    monkeypatch.setenv("MAX_USER_MESSAGE_CHARS", "5")
    response = client.post("/chat", json={"message": "this is too long"})
    assert response.status_code == 400
    assert "exceeds max length" in response.json()["detail"]


def test_health_rate_limit_enforced(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("RATE_LIMIT_HEALTH_REQUESTS", "1")
    with main._RATE_LOCK:
        main._RATE_BUCKETS.clear()

    first = client.get("/health")
    second = client.get("/health")

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["error"] == "RATE_LIMITED"
    assert "retry_after_seconds" in body


def test_disallows_unknown_tool_name(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "allowed_tool", "input_schema": {"type": "object"}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-x",
                    "type": "function",
                    "function": {"name": "forbidden_tool", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "Tool not available in this session."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "use forbidden tool"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Tool not available in this session."


def test_verify_pin_allows_order_tool_call(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_order_status", "input_schema": {"type": "object"}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-auth",
                    "type": "function",
                    "function": {
                        "name": "verify_customer_pin",
                        "arguments": '{"email":"donaldgarcia@example.net","pin":"7912"}',
                    },
                },
                {
                    "id": "call-order",
                    "type": "function",
                    "function": {"name": "get_order_status", "arguments": '{"order_id":"A100"}'},
                },
            ],
        },
        {"role": "assistant", "content": "Your order A100 is in transit."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    called_tools: list[str] = []

    async def fake_call_tool(self, name, arguments):
        called_tools.append(name)
        return {"status": "in_transit", "order_id": arguments.get("order_id")}

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "track my order"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["authenticated"] is True
    assert payload["session"]["email"] == "donaldgarcia@example.net"
    assert "get_order_status" in called_tools


def test_chat_keeps_existing_auth_if_model_reverifies_without_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")

    async def fake_list_tools(self):
        return [{"name": "get_order_status", "input_schema": {"type": "object"}}]

    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-auth-empty",
                    "type": "function",
                    "function": {"name": "verify_customer_pin", "arguments": "{}"},
                },
                {
                    "id": "call-order",
                    "type": "function",
                    "function": {"name": "get_order_status", "arguments": '{"order_id":"A100"}'},
                },
            ],
        },
        {"role": "assistant", "content": "Order A100 is still in transit."},
    ]

    async def fake_chat(self, messages, tools, *, model=None):
        return responses.pop(0)

    called_tools: list[str] = []

    async def fake_call_tool(self, name, arguments):
        called_tools.append(name)
        return {"status": "in_transit", "order_id": arguments.get("order_id")}

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.MCPService, "call_tool", fake_call_tool)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    auth_token = issue_token("donaldgarcia@example.net")
    response = client.post(
        "/chat",
        json={
            "message": "track my order",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
            "auth_token": auth_token,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["authenticated"] is True
    assert payload["session"]["email"] == "donaldgarcia@example.net"
    assert "get_order_status" in called_tools


def test_model_chain_default_fallback_escalation_order(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")
    monkeypatch.setenv("OPENROUTER_DEFAULT_MODEL", "provider/default-model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL", "provider/fallback-model")
    monkeypatch.setenv("OPENROUTER_ESCALATION_MODEL", "provider/escalation-model")
    monkeypatch.setenv("MODEL_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("MODEL_ESCALATE_ON_KEYWORDS", "true")
    monkeypatch.setenv("MODEL_ESCALATE_ON_ORDER_AUTH", "true")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")

    async def fake_list_tools(self):
        return []

    called_models: list[str] = []

    async def fake_chat(self, messages, tools, *, model=None):
        called_models.append(model or "")
        if model in {"provider/default-model", "provider/fallback-model"}:
            raise httpx.ConnectError(f"simulated failure for {model}")
        return {"role": "assistant", "content": "Resolved by escalation fallback"}

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "hello there"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Resolved by escalation fallback"
    assert called_models == [
        "provider/default-model",
        "provider/fallback-model",
        "provider/escalation-model",
    ]


def test_model_chain_escalation_first_path(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com")
    monkeypatch.setenv("OPENROUTER_DEFAULT_MODEL", "provider/default-model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL", "provider/fallback-model")
    monkeypatch.setenv("OPENROUTER_ESCALATION_MODEL", "provider/escalation-model")
    monkeypatch.setenv("MODEL_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("MODEL_ESCALATE_ON_KEYWORDS", "true")
    monkeypatch.setenv("MODEL_ESCALATE_ON_ORDER_AUTH", "true")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")

    async def fake_list_tools(self):
        return []

    called_models: list[str] = []

    async def fake_chat(self, messages, tools, *, model=None):
        called_models.append(model or "")
        if model == "provider/escalation-model":
            raise httpx.ConnectError("simulate escalation failure")
        return {"role": "assistant", "content": "Resolved by default after escalation failure"}

    monkeypatch.setattr(main.MCPService, "list_tools", fake_list_tools)
    monkeypatch.setattr(main.OpenRouterService, "chat", fake_chat)

    response = client.post("/chat", json={"message": "I need an urgent refund now"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Resolved by default after escalation failure"
    assert called_models == [
        "provider/escalation-model",
        "provider/default-model",
    ]
