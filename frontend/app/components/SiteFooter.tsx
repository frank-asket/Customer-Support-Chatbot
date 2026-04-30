import Link from "next/link";

type FooterLink = {
  href: string;
  label: string;
};

type SiteFooterProps = {
  links: FooterLink[];
  id?: string;
};

export default function SiteFooter({ links, id }: SiteFooterProps) {
  return (
    <footer className="lp-foot" id={id}>
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
    </footer>
  );
}
