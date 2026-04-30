import Link from "next/link";

type HeaderLink = {
  href: string;
  label: string;
};

type SiteHeaderProps = {
  navAriaLabel: string;
  links: HeaderLink[];
  ctaHref: string;
  ctaLabel: string;
  ctaClassName?: string;
};

export default function SiteHeader({
  navAriaLabel,
  links,
  ctaHref,
  ctaLabel,
  ctaClassName = "btn btn-primary"
}: SiteHeaderProps) {
  return (
    <header className="lp-nav">
      <div className="lp-brand" aria-label="Meridian Electronics">
        <span>MERIDIAN</span>
        <span>AI SUPPORT</span>
      </div>
      <nav className="lp-links" aria-label={navAriaLabel}>
        {links.map((link) =>
          link.href.startsWith("#") ? (
            <a key={`${link.href}-${link.label}`} href={link.href}>
              {link.label}
            </a>
          ) : (
            <Link key={`${link.href}-${link.label}`} href={link.href}>
              {link.label}
            </Link>
          )
        )}
      </nav>
      <Link className={ctaClassName} href={ctaHref}>
        {ctaLabel}
      </Link>
    </header>
  );
}
