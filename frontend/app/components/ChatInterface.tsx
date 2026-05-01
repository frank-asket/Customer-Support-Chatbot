"use client";

import { useEffect, useState } from "react";
import MessageBubble from "@/app/components/MessageBubble";

type Session = {
  authenticated: boolean;
  email: string | null;
  authToken?: string | null;
  /** Client-side expiry (ms since epoch); used to refresh before TTL. */
  authTokenExpiresAt?: number | null;
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

const SESSION_KEY = "meridian_support_session";

const REFRESH_MARGIN_MS = 5 * 60 * 1000;

function revokeRemoteSession(token: string | null | undefined) {
  if (!token) return;
  void fetch("/api/auth/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auth_token: token })
  });
}

async function refreshSessionIfStale(sess: Session): Promise<Session> {
  if (!sess.authenticated || !sess.authToken) return sess;
  if (sess.authTokenExpiresAt && Date.now() < sess.authTokenExpiresAt - REFRESH_MARGIN_MS) return sess;
  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auth_token: sess.authToken })
    });
    const data = (await res.json()) as {
      authenticated?: boolean;
      auth_token?: string | null;
      auth_token_expires_in?: number | null;
      email?: string | null;
    };
    if (!res.ok || !data.authenticated || !data.auth_token) {
      return {
        authenticated: false,
        email: null,
        authToken: null,
        authTokenExpiresAt: null,
        customerContext: null
      };
    }
    const ttlSec = typeof data.auth_token_expires_in === "number" ? data.auth_token_expires_in : 3600;
    return {
      ...sess,
      authToken: data.auth_token,
      email: data.email ?? sess.email,
      authTokenExpiresAt: Date.now() + ttlSec * 1000
    };
  } catch {
    return sess;
  }
}

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
  const [session, setSession] = useState<Session>({
    authenticated: false,
    email: null,
    authToken: null,
    authTokenExpiresAt: null
  });
  const [sessionReady, setSessionReady] = useState(false);
  const [capabilityPrompts, setCapabilityPrompts] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [lastRequestId, setLastRequestId] = useState<string | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) {
        setSessionReady(true);
        return;
      }
      const parsed = JSON.parse(raw) as Session;
      if (typeof parsed.authenticated === "boolean") {
        const nextSession = {
          authenticated: parsed.authenticated,
          email: parsed.email ?? null,
          authToken: parsed.authToken ?? null,
          authTokenExpiresAt: parsed.authTokenExpiresAt ?? null,
          customerContext: parsed.customerContext ?? null
        };
        setSession(nextSession);
        if (nextSession.authenticated) {
          setMessages([{ role: "assistant", text: buildWelcomeMessage(nextSession) }]);
        }
      }
    } catch {
      localStorage.removeItem(SESSION_KEY);
    } finally {
      setSessionReady(true);
    }
  }, []);

  useEffect(() => {
    if (!sessionReady) return;
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  }, [session, sessionReady]);

  useEffect(() => {
    let active = true;
    async function loadCapabilities() {
      try {
        const response = await fetch("/api/capabilities");
        const data = (await response.json()) as {
          helper_message?: string;
          suggested_prompts?: string[];
        };
        if (!active || !response.ok) return;
        if (Array.isArray(data.suggested_prompts)) {
          setCapabilityPrompts(data.suggested_prompts.slice(0, 5));
        }
        const helperMessage = data.helper_message;
        if (helperMessage) {
          setMessages((prev) => {
            const alreadyAdded = prev.some((msg) => msg.role === "assistant" && msg.text === helperMessage);
            return alreadyAdded ? prev : [...prev, { role: "assistant", text: helperMessage }];
          });
        }
      } catch {
        // Best effort only; chat remains usable without capabilities metadata.
      }
    }
    loadCapabilities();
    return () => {
      active = false;
    };
  }, []);

  async function onSend() {
    const nextInput = input.trim();
    if (!nextInput || loading || !sessionReady) return;

    setMessages((prev) => [...prev, { role: "user", text: nextInput }]);
    setInput("");
    setLoading(true);

    try {
      let workingSession = session;
      if (session.authenticated && session.authToken) {
        workingSession = await refreshSessionIfStale(session);
        if (
          workingSession.authToken !== session.authToken ||
          workingSession.authTokenExpiresAt !== session.authTokenExpiresAt ||
          workingSession.authenticated !== session.authenticated
        ) {
          setSession(workingSession);
        }
      }
      if (workingSession.authenticated && !workingSession.authToken) {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", text: "Your session expired. Please verify again from the auth page." }
        ]);
        setLoading(false);
        return;
      }

      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: nextInput,
          session: { authenticated: workingSession.authenticated, email: workingSession.email },
          auth_token: workingSession.authToken ?? null
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
          authToken: data.session?.authenticated ? prev.authToken ?? null : null,
          authTokenExpiresAt: data.session?.authenticated ? prev.authTokenExpiresAt ?? null : null,
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
                revokeRemoteSession(session.authToken);
                setSession({ authenticated: false, email: null, authToken: null, authTokenExpiresAt: null });
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
          {capabilityPrompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => setInput(prompt)}
              disabled={loading || !sessionReady}
              className="chat-chip-btn"
            >
              {prompt}
            </button>
          ))}
          <button
            type="button"
            onClick={() => {
              revokeRemoteSession(session.authToken);
              setMessages([]);
              setLastRequestId(null);
              setSession({ authenticated: false, email: null, authToken: null, authTokenExpiresAt: null });
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
            placeholder={sessionReady ? "Ask about products or orders..." : "Preparing your session..."}
            className="chat-input"
            disabled={!sessionReady}
          />
          <button
            type="button"
            onClick={onSend}
            disabled={loading || !sessionReady}
            className="chat-send-btn"
          >
            {!sessionReady ? "Loading..." : loading ? "Sending..." : "Send"}
          </button>
        </div>
      </section>
    </section>
  );
}
