import type { Metadata } from "next";
import "./globals.css";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "corlinman admin",
  description:
    "corlinman admin UI — Rust gateway + Python AI layer + Next.js control plane.",
};

// Inline boot script. Runs before React hydrates so <html lang> matches
// the persisted i18n choice (or the browser hint) — no FOUC when the user
// previously picked English.
const LANG_BOOT = `
(function(){try{
  var k="corlinman_lang";
  var s=localStorage.getItem(k);
  var l=(s==="zh-CN"||s==="en")?s:((navigator.language||"").toLowerCase().indexOf("zh")===0?"zh-CN":"en");
  document.documentElement.setAttribute("lang",l);
}catch(e){}})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // `suppressHydrationWarning` is required by next-themes when it toggles the
  // dark/light class on <html>. Geist sans + mono are exposed as CSS vars
  // (`--font-geist-sans`, `--font-geist-mono`) consumed by tailwind.config.ts.
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${GeistSans.variable} ${GeistMono.variable} dark`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: LANG_BOOT }} />
      </head>
      <body className="min-h-dvh bg-background font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
