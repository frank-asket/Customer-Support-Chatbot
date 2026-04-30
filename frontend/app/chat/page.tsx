import ChatInterface from "@/app/components/ChatInterface";
import SiteHeader from "@/app/components/SiteHeader";
import SiteFooter from "@/app/components/SiteFooter";

export default function ChatPage() {
  return (
    <main className="lp-wrap auth-layout">
      <section className="lp-shell auth-shell">
        <SiteHeader
          navAriaLabel="Chat navigation"
          links={[
            { href: "/", label: "Home" },
            { href: "/auth", label: "Verify account" },
            { href: "/chat", label: "Chat" }
          ]}
          ctaHref="/chat"
          ctaLabel="Open chat"
        />

        <ChatInterface />

        <SiteFooter
          id="support"
          links={[
            { href: "/", label: "Home" },
            { href: "/auth", label: "Verify account" },
            { href: "/chat", label: "Open chat" }
          ]}
        />
      </section>
    </main>
  );
}
