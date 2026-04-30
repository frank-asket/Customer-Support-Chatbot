"use client";

import ChatInterface from "@/app/components/ChatInterface";
import SiteHeader from "@/app/components/SiteHeader";
import SiteFooter from "@/app/components/SiteFooter";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const SESSION_KEY = "meridian_support_session";

export default function ChatPage() {
  const router = useRouter();
  const [isAuthorized, setIsAuthorized] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) {
        router.replace("/auth");
        return;
      }
      const parsed = JSON.parse(raw) as { authenticated?: boolean; authToken?: string | null };
      if (parsed.authenticated && parsed.authToken) {
        setIsAuthorized(true);
        return;
      }
      router.replace("/auth");
    } catch {
      localStorage.removeItem(SESSION_KEY);
      router.replace("/auth");
    }
  }, [router]);

  if (!isAuthorized) return null;

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
