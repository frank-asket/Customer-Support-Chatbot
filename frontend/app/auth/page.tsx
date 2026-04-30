"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import SiteHeader from "@/app/components/SiteHeader";
import SiteFooter from "@/app/components/SiteFooter";

const SESSION_KEY = "meridian_support_session";
type CustomerContext = {
  first_name: string;
  last_order_id: string;
  last_order_status: string;
  primary_request: string;
};

export default function AuthPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [pin, setPin] = useState("");
  const [showPin, setShowPin] = useState(false);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canSubmit = email.trim().length > 3 && /^\d{4}$/.test(pin);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;
    setLoading(true);
    setMessage(null);
    setError(null);

    try {
      const response = await fetch("/api/auth/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, pin })
      });
      const data = (await response.json()) as {
        authenticated?: boolean;
        email?: string | null;
        message?: string;
        customer_context?: CustomerContext | null;
        auth_token?: string | null;
        error?: string;
        details?: string;
      };

      if (!response.ok) {
        setError(`${data.error ?? "Verification request failed"}${data.details ? ` (${data.details})` : ""}`);
        return;
      }

      if (data.authenticated) {
        localStorage.setItem(
          SESSION_KEY,
          JSON.stringify({
            authenticated: true,
            email: data.email ?? email.trim().toLowerCase(),
            customerContext: data.customer_context ?? null,
            authToken: data.auth_token ?? null
          })
        );
        setMessage(data.message ?? "Verification successful.");
        router.push("/chat");
        return;
      }

      localStorage.removeItem(SESSION_KEY);
      setError(data.message ?? "Verification failed. Please check your details.");
    } catch {
      setError("Network error while verifying. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="lp-wrap auth-layout">
      <section className="lp-shell auth-shell">
        <SiteHeader
          navAriaLabel="Auth navigation"
          links={[
            { href: "/", label: "Home" },
            { href: "/chat", label: "Chat" }
          ]}
          ctaHref="/chat"
          ctaLabel="Open chat"
        />

        <div className="auth-content">
          <article className="auth-intro">
            <p className="auth-kicker">Secure Access</p>
            <h1>Identity verification for protected order access</h1>
            <p>
              Verify your email and 4-digit PIN to access shipment status, order history, and account-specific support
              actions.
            </p>
            <div className="lp-trust auth-trust">
              <span>Credential-gated support actions</span>
              <span>Immediate access to verified workflows</span>
              <span>Session reset available anytime</span>
            </div>
            <div className="auth-assurance">
              <h3>Why verification is required</h3>
              <ul>
                <li>Prevents unauthorized access to order history and shipment addresses.</li>
                <li>Enables precise responses using your current account and order context.</li>
                <li>Completes in seconds using your registered email and 4-digit PIN.</li>
              </ul>
            </div>
          </article>

          <section className="auth-card">
            <div className="auth-card-head">
              <h2>Verify credentials</h2>
              <span className="auth-pill">Step 1 of 1</span>
            </div>
            <p className="auth-card-subtitle">Required before any account-level order operations can be executed.</p>
            <form onSubmit={onSubmit} className="auth-form">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onBlur={() => setEmail((prev) => prev.trim().toLowerCase())}
                placeholder="you@example.com"
                autoComplete="email"
                required
              />

              <label htmlFor="pin">PIN</label>
              <div className="auth-pin-row">
                <input
                  id="pin"
                  type={showPin ? "text" : "password"}
                  value={pin}
                  onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 4))}
                  placeholder="4-digit PIN"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  required
                  minLength={4}
                  maxLength={4}
                  pattern="[0-9]{4}"
                />
                <button
                  type="button"
                  className="auth-pin-toggle"
                  onClick={() => setShowPin((prev) => !prev)}
                  aria-label={showPin ? "Hide PIN" : "Show PIN"}
                >
                  {showPin ? "Hide" : "Show"}
                </button>
              </div>

              <ul className="auth-checklist" aria-label="Verification requirements">
                <li>Use the same email associated with your Meridian order.</li>
                <li>PIN must be exactly 4 digits.</li>
                <li>Order-specific tools unlock automatically after successful verification.</li>
              </ul>

              <button className="btn btn-primary auth-submit" type="submit" disabled={loading || !canSubmit}>
                {loading ? "Verifying..." : "Verify and continue to support"}
              </button>
            </form>
            <div aria-live="polite">
              {message ? <p className="auth-success">{message}</p> : null}
              {error ? <p className="auth-error">{error}</p> : null}
            </div>
            <p className="auth-back">
              Need general guidance first? <Link href="/chat">Open chat</Link>
            </p>
          </section>
        </div>

        <SiteFooter
          links={[
            { href: "/", label: "Home" },
            { href: "/chat", label: "Open chat" },
            { href: "/auth", label: "Verify account" }
          ]}
        />
      </section>
    </main>
  );
}
