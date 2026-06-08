/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a self-contained server bundle (.next/standalone) so the runtime
  // image can ship without node_modules or the full toolchain.
  output: "standalone",
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "*.amazonaws.com" },
      { protocol: "http",  hostname: "minio" },
      { protocol: "http",  hostname: "localhost" },
    ],
  },
};

export default nextConfig;
