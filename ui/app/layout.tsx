import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "corlinman 管理后台",
  description:
    "corlinman admin UI — Rust 网关 + Python AI 层 + Next.js 管理面板",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // `suppressHydrationWarning` is required by next-themes when toggling
  // between light/dark classes on <html>.
  return (
    <html lang="zh-CN" suppressHydrationWarning className="dark">
      <body className="min-h-dvh bg-background text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
