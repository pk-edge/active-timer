/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a self-contained server bundle (.next/standalone) so the runtime
  // image can ship without node_modules or the full toolchain.
  output: "standalone",

  // Gzip what the Next server sends (HTML/JSON/JS). Default-on; set explicitly
  // so it can never be accidentally turned off.
  compress: true,

  // Barrel-file optimization: pull ONLY the symbols actually used from these
  // large packages into each route chunk instead of the whole barrel. Smaller
  // bundles and faster compiles for icon + chart imports.
  experimental: {
    optimizePackageImports: ["recharts", "lucide-react", "date-fns"],
  },

  images: {
    remotePatterns: [
      { protocol: "https", hostname: "*.amazonaws.com" },
      { protocol: "http",  hostname: "minio" },
      { protocol: "http",  hostname: "localhost" },
    ],
  },
};

export default nextConfig;
