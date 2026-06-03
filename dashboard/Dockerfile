FROM node:20

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

WORKDIR /app

# Copy package files first so npm install runs on container start
COPY package.json package-lock.json* ./

# Copy all source code
COPY . .

EXPOSE 3000
ENV PORT=3000 HOSTNAME="0.0.0.0"

# Install node_modules, build .next, then start — all at container runtime
CMD ["sh", "-c", "npm install && npm run build && npm start"]
