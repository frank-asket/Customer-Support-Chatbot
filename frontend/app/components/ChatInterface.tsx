"use client";

import { useState } from "react";
import MessageBubble from "@/app/components/MessageBubble";

type Session = {
  authenticated: boolean;
  email: string | null;
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

export default function ChatInterface() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [session, setSession] = useState<Session>({ authenticated: false, email: null });
  const [loading, setLoading] = useState(false);
  const [lastRequestId, setLastRequestId] = useState<string | null>(null);

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
          session
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

      if (data.session) setSession(data.session);
      if (data.request_id) setLastRequestId(data.request_id);
    } catch {
      setMessages((prev) => [...prev, { role: "assistant", text: "Network error. Please try again." }]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main style={{ display: "grid", placeItems: "center", padding: "24px" }}>
      <section
        style={{
          width: "min(900px, 100%)",
          border: "1px solid #334155",
          borderRadius: "12px",
          padding: "16px",
          background: "#0f172a"
        }}
      >
        <h1 style={{ marginTop: 0 }}>Meridian Electronics Support</h1>
        <p style={{ opacity: 0.8 }}>
          Session auth: {session.authenticated ? `verified (${session.email})` : "not verified"}
        </p>
        {lastRequestId ? <p style={{ opacity: 0.6, marginTop: "-6px" }}>Last request: {lastRequestId}</p> : null}

        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "10px" }}>
          {QUICK_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => setInput(prompt)}
              disabled={loading}
              style={{
                padding: "6px 10px",
                borderRadius: "999px",
                border: "1px solid #334155",
                background: "#111827",
                color: "#e2e8f0",
                cursor: "pointer"
              }}
            >
              {prompt}
            </button>
          ))}
          <button
            type="button"
            onClick={() => {
              setMessages([]);
              setLastRequestId(null);
            }}
            style={{
              padding: "6px 10px",
              borderRadius: "999px",
              border: "1px solid #334155",
              background: "#1f2937",
              color: "#e2e8f0",
              cursor: "pointer"
            }}
          >
            Clear chat
          </button>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "8px", minHeight: "300px", marginBottom: "12px" }}>
          {messages.map((message, idx) => (
            <MessageBubble key={`${message.role}-${idx}`} role={message.role} text={message.text} />
          ))}
          {loading ? <MessageBubble role="assistant" text="Thinking..." /> : null}
        </div>

        <div style={{ display: "flex", gap: "8px" }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSend();
            }}
            placeholder="Ask about products or orders..."
            style={{
              flex: 1,
              padding: "10px 12px",
              borderRadius: "8px",
              border: "1px solid #334155",
              background: "#020617",
              color: "#f8fafc"
            }}
          />
          <button
            type="button"
            onClick={onSend}
            disabled={loading}
            style={{
              padding: "10px 14px",
              borderRadius: "8px",
              border: "none",
              background: "#2563eb",
              color: "#fff",
              cursor: "pointer"
            }}
          >
            {loading ? "Sending..." : "Send"}
          </button>
        </div>
      </section>
    </main>
  );
}
