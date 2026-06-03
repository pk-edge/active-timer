/** @type {import('next').NextConfig} */
const nextConfig = {
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "*.amazonaws.com" },
      { protocol: "http",  hostname: "minio" },
      { protocol: "http",  hostname: "localhost" },
    ],
  },
};

export default nextConfig;
