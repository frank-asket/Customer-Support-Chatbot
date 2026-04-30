import Link from "next/link";
import SiteHeader from "@/app/components/SiteHeader";
import SiteFooter from "@/app/components/SiteFooter";

export default function HomePage() {
  return (
    <main className="lp-wrap">
      <section className="lp-shell" aria-labelledby="hero-title">
        <SiteHeader
          navAriaLabel="Main"
          links={[
            { href: "#features", label: "Features" },
            { href: "#orders", label: "Order Help" },
            { href: "#security", label: "Security" },
            { href: "#support", label: "Support" }
          ]}
          ctaHref="/auth"
          ctaLabel="Verify account"
        />

        <div className="lp-grid">
          <article className="lp-left">
            <h1 id="hero-title">
              Get fast answers for your
              <br />
              <span>Meridian orders and products</span>
            </h1>
            <p>
              Need help with availability, returns, or tracking? Chat with Meridian Support anytime and verify your
              account securely before order-specific actions.
            </p>
            <div className="lp-actions">
              <Link className="btn btn-primary" href="/auth">
                Verify account
              </Link>
              <Link href="/chat" className="btn btn-link">
                Ask a general question
              </Link>
              <a href="#features" className="btn btn-link">
                View features
              </a>
            </div>
            <div className="lp-trust">
              <span>Live chat support</span>
              <span>Secure email + PIN verification</span>
              <span>Order help after verification</span>
            </div>
          </article>

          <aside className="lp-right" id="features">
            <div className="lp-panel lp-chat-mockup">
              <div className="lp-chat-top">
                <div>
                  <h2>ChatInterface Mockup</h2>
                  <p>Meridian Electronics Support</p>
                </div>
                <span className="lp-chat-status">Connected</span>
              </div>

              <div className="lp-chat-body" aria-label="Meridian chat preview">
                <div className="lp-bubble lp-bubble-user">Track my order A100</div>
                <div className="lp-bubble lp-bubble-assistant">
                  Please provide your email and 4-digit PIN before I access order details.
                </div>
                <div className="lp-bubble lp-bubble-user">donaldgarcia@example.net / 7912</div>
                <div className="lp-bubble lp-bubble-assistant">
                  Verified. Order A100 is in transit and expected tomorrow by 5 PM.
                </div>
              </div>

              <div className="lp-chat-input">
                <span>Ask about products or orders...</span>
                <button type="button">Send</button>
              </div>
            </div>

            <div className="lp-float lp-float-top">Verify first for order access</div>
            <div className="lp-float lp-float-bottom">Friendly support, faster answers</div>
            <div className="lp-arrow" aria-hidden="true">
              ↷
            </div>
          </aside>
        </div>

        <SiteFooter
          id="support"
          links={[
            { href: "#features", label: "Features" },
            { href: "/auth", label: "Verify account" },
            { href: "/chat", label: "Open chat" }
          ]}
        />
      </section>
    </main>
  );
}
