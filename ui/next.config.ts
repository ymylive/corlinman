import type { NextConfig } from "next";

// corlinman ui — Next.js config
//
// NOTE: `output: "export"` produces a fully static bundle so the Docker
// `ui-builder` stage can copy `out/` into the runtime image (see plan §10).
// TODO(M6): Switch to SSR (`output: undefined` / default) if admin pages
// require request-time data from the gateway that cannot be fetched from
// the client. In that case the Dockerfile needs to change to ship `.next/`
// and run `next start` instead of serving a static export.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "export",
  // next-intl plugin is wired in lib/i18n (see lib/i18n/*); keep here explicit.
  images: {
    // Static export cannot use the Next image optimizer.
    unoptimized: true,
  },
  experimental: {
    typedRoutes: true,
  },
};

export default nextConfig;
