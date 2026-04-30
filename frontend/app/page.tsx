import Link from "next/link";

export default function HomePage() {
  return (
    <main className="landing">
      <section className="hero" aria-labelledby="hero-title">
        <p className="hero-kicker">Meridian Electronics</p>
        <h1 id="hero-title">Customer support that answers instantly and acts securely.</h1>
        <p className="hero-copy">
          Give customers real-time answers for products, returns, and order questions while enforcing PIN verification
          before sensitive actions.
        </p>
        <div className="hero-actions">
          <Link className="btn btn-primary" href="/chat">
            Open live assistant
          </Link>
          <a className="btn btn-ghost" href="#how-it-works">
            See how it works
          </a>
        </div>
      </section>

      <section className="feature-band" aria-label="Core capabilities">
        <article>
          <h2>Inventory and order help</h2>
          <p>Resolve the most common requests without long queues or manual lookup.</p>
        </article>
        <article>
          <h2>Built-in auth guardrails</h2>
          <p>Order tools only unlock after verified email and PIN checks.</p>
        </article>
        <article>
          <h2>Production-ready routing</h2>
          <p>Model routing with default, fallback, and escalation control from env vars.</p>
        </article>
      </section>

      <section id="how-it-works" className="detail" aria-labelledby="detail-title">
        <h2 id="detail-title">Designed for support teams that need speed and trust</h2>
        <p>
          The assistant runs a tool-calling loop through your MCP server, applies authentication policy before
          protected order workflows, and returns answers in a clean chat experience your team can ship now.
        </p>
      </section>

      <section className="final-cta" aria-label="Call to action">
        <h2>Ready to test the Meridian AI support flow?</h2>
        <Link className="btn btn-primary" href="/chat">
          Start a conversation
        </Link>
      </section>
    </main>
  );
}
