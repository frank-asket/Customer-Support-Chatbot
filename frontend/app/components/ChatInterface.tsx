"use client";

import { useEffect, useState } from "react";
import MessageBubble from "@/app/components/MessageBubble";

type Session = {
  authenticated: boolean;
  email: string | null;
  customerContext?: {
    first_name: string;
    last_order_id: string;
    last_order_status: string;
    primary_request: string;
  } | null;
};

type ChatEntry = {
  role: "user" | "assistant";
  text: string;
};

const QUICK_PROMPTS = [
  "Do you have iPhone 15 Pro in stock?",
  "Track my recent order",
  "What is your return policy?"
];
const SESSION_KEY = "meridian_support_session";

function buildWelcomeMessage(session: Session): string {
  const context = session.customerContext;
  const name = context?.first_name || "there";
  if (!context) {
    return `Welcome ${name}! Your account is verified. You can now ask about orders, returns, and product support.`;
  }
  return [
    `Welcome ${name}! Your account is verified.`,
    `I can already see your recent request: "${context.primary_request}".`,
    `Latest order on file: ${context.last_order_id} (${context.last_order_status}).`,
    "Ask any follow-up question and I will continue from here."
  ].join(" ");
}

export default function ChatInterface() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [session, setSession] = useState<Session>({ authenticated: false, email: null });
  const [loading, setLoading] = useState(false);
  const [lastRequestId, setLastRequestId] = useState<string | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Session;
      if (typeof parsed.authenticated === "boolean") {
        const nextSession = {
          authenticated: parsed.authenticated,
          email: parsed.email ?? null,
          customerContext: parsed.customerContext ?? null
        };
        setSession(nextSession);
        if (nextSession.authenticated) {
          setMessages([{ role: "assistant", text: buildWelcomeMessage(nextSession) }]);
        }
      }
    } catch {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  }, [session]);

  async function onSend() {
    const nextInput = input.trim();
    if (!nextInput || loading) return;

    setMessages((prev) => [...prev, { role: "user", text: nextInput }]);
    setInput("");
    setLoading(true);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: nextInput,
          session: { authenticated: session.authenticated, email: session.email }
        })
      });

      const data = (await response.json()) as {
        reply?: string;
        error?: string;
        details?: string;
        session?: Session;
        request_id?: string;
      };

      if (!response.ok) {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            text: `Error: ${data.error ?? "Request failed"}${data.details ? ` (${data.details})` : ""}`
          }
        ]);
      } else {
        setMessages((prev) => [...prev, { role: "assistant", text: data.reply ?? "No reply." }]);
      }

      if (data.session) {
        setSession((prev) => ({
          authenticated: data.session?.authenticated ?? prev.authenticated,
          email: data.session?.email ?? prev.email,
          customerContext: data.session?.authenticated ? prev.customerContext ?? null : null
        }));
      }
      if (data.request_id) setLastRequestId(data.request_id);
    } catch {
      setMessages((prev) => [...prev, { role: "assistant", text: "Network error. Please try again." }]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="chat-wrap">
      <section className="chat-shell">
        <h1 className="chat-title">Meridian Electronics Support</h1>
        {session.authenticated ? (
          <div className="chat-verified-banner">
            <p className="chat-verified-text">
              Already verified as <strong>{session.email ?? "customer"}</strong>.
            </p>
            <button
              type="button"
              onClick={() => {
                setSession({ authenticated: false, email: null });
                localStorage.removeItem(SESSION_KEY);
                setMessages([]);
              }}
              className="chat-chip-btn"
            >
              Sign out
            </button>
          </div>
        ) : null}
        <p className="chat-subtitle">
          Session auth: {session.authenticated ? `verified (${session.email})` : "not verified"}
        </p>
        {lastRequestId ? <p className="chat-request-id">Last request: {lastRequestId}</p> : null}

        <div className="chat-chips-row">
          {QUICK_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => setInput(prompt)}
              disabled={loading}
              className="chat-chip-btn"
            >
              {prompt}
            </button>
          ))}
          <button
            type="button"
            onClick={() => {
              setMessages([]);
              setLastRequestId(null);
              setSession({ authenticated: false, email: null });
              localStorage.removeItem(SESSION_KEY);
            }}
            className="chat-chip-btn chat-chip-secondary"
          >
            Clear chat + auth
          </button>
        </div>

        <div className="chat-thread">
          {messages.map((message, idx) => (
            <MessageBubble key={`${message.role}-${idx}`} role={message.role} text={message.text} />
          ))}
          {loading ? <MessageBubble role="assistant" text="Thinking..." /> : null}
        </div>

        <div className="chat-input-row">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSend();
            }}
            placeholder="Ask about products or orders..."
            className="chat-input"
          />
          <button
            type="button"
            onClick={onSend}
            disabled={loading}
            className="chat-send-btn"
          >
            {loading ? "Sending..." : "Send"}
          </button>
        </div>
      </section>
    </section>
  );
}
