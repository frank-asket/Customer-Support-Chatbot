import httpx
from fastapi.testclient import TestClient

from backend.app import main


client = TestClient(main.app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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


def test_chat_rejects_stream_mode() -> None:
    response = client.post("/chat", json={"message": "hello", "stream": True})
    assert response.status_code == 400
    assert "stream=true" in response.json()["detail"]


def test_chat_rejects_too_long_message(monkeypatch) -> None:
    monkeypatch.setenv("MAX_USER_MESSAGE_CHARS", "5")
    response = client.post("/chat", json={"message": "this is too long"})
    assert response.status_code == 400
    assert "exceeds max length" in response.json()["detail"]


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

    response = client.post(
        "/chat",
        json={
            "message": "track my order",
            "session": {"authenticated": True, "email": "donaldgarcia@example.net"},
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
