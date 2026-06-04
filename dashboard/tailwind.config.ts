import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/hooks/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      keyframes: {
        wiggle: {
          "0%, 100%": { transform: "rotate(0deg)" },
          "15%":       { transform: "rotate(-15deg)" },
          "30%":       { transform: "rotate(12deg)" },
          "45%":       { transform: "rotate(-10deg)" },
          "60%":       { transform: "rotate(8deg)" },
          "75%":       { transform: "rotate(-5deg)" },
        },
      },
      animation: {
        wiggle: "wiggle 0.6s ease-in-out",
      },
    },
  },
  plugins: [],
};

export default config;
