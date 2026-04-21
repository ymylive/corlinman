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
      <body className="min-h-dvh bg-background font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
