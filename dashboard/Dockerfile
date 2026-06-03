FROM node:20

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

WORKDIR /app

# Install dependencies as a separate layer (cached unless package.json changes)
COPY package.json package-lock.json* ./
RUN npm ci

# Copy source code
COPY . .

EXPOSE 3000
ENV PORT=3000 HOSTNAME="0.0.0.0"

# Build .next and start — only the build runs at container startup
CMD ["sh", "-c", "npm run build && npm start"]
