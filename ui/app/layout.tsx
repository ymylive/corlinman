import type { Metadata } from "next";
import "./globals.css";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Instrument_Serif } from "next/font/google";
import { Providers } from "@/components/providers";

// Tidepool display serif (Phase 0). Used only where explicitly opted in
// via `font-serif` (hero greeting, uptime streak card, italic emphasis).
// Single weight + italic keeps the payload under ~30KB total.
const instrumentSerif = Instrument_Serif({
  weight: "400",
  style: ["normal", "italic"],
  subsets: ["latin"],
  display: "swap",
  variable: "--font-instrument-serif",
});

export const metadata: Metadata = {
  title: "corlinman admin",
  description:
    "corlinman admin UI — Rust gateway + Python AI layer + Next.js control plane.",
};

// Inline boot script. Runs before React hydrates so <html lang> matches
// the persisted i18n choice (or the browser hint) — no FOUC when the user
// previously picked English. Also hydrates the Tidepool theme (light/dark)
// from localStorage so theme-sensitive surfaces (aurora, glass, palette
// outline) paint in the correct mode on first paint, not after React.
const BOOT = `
(function(){try{
  var el = document.documentElement;
  // Language
  var k="corlinman_lang";
  var s=localStorage.getItem(k);
  var l=(s==="zh-CN"||s==="en")?s:((navigator.language||"").toLowerCase().indexOf("zh")===0?"zh-CN":"en");
  el.setAttribute("lang",l);
  // Theme (Tidepool). URL ?theme=light|dark wins over storage (handy for
  // demos / screenshot testing) — and is persisted to localStorage so that
  // next-themes (initialised later inside React) sees the same value and
  // doesn't override our choice. Otherwise falls back to stored value,
  // then the legacy next-themes key, then dark as the default.
  var tk="corlinman-theme";
  var qs=(location.search||"").match(/[?&]theme=(light|dark)/);
  var t = qs ? qs[1] : localStorage.getItem(tk);
  if (!t) { var ts=localStorage.getItem("theme"); if (ts==="light"||ts==="dark") t=ts; }
  if (t!=="light" && t!=="dark") t="dark";
  if (qs) { try { localStorage.setItem(tk, t); } catch(_){} }
  el.setAttribute("data-theme", t);
  if (t==="dark") el.classList.add("dark"); else el.classList.remove("dark");
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
      className={`${GeistSans.variable} ${GeistMono.variable} ${instrumentSerif.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT }} />
      </head>
      {/* Body does NOT paint a background. Admin routes mount their own
          <AuroraBackground />, login provides a dot-grid layer. Route groups
          that need a solid color set it on their own wrapper. This lets the
          aurora actually show through — otherwise bg-background sits on top
          of the fixed -z-10 aurora layer. */}
      <body className="min-h-dvh font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
