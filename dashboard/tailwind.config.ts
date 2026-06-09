import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/hooks/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Edge brand purple — replaces Tailwind's blue as the primary accent
        brand: {
          50:  "#f5eeff",
          100: "#ead6ff",
          200: "#d4acff",
          300: "#b794f4",
          400: "#9b6fe8",
          500: "#7c3aed",
          600: "#4a0f70",   // ← the main brand colour #4a0f70
          700: "#3d0c5e",
          800: "#2d0647",
          900: "#1e0330",
        },
      },
      keyframes: {
        wiggle: {
          "0%, 100%": { transform: "rotate(0deg)" },
          "15%":       { transform: "rotate(-15deg)" },
          "30%":       { transform: "rotate(12deg)" },
          "45%":       { transform: "rotate(-10deg)" },
          "60%":       { transform: "rotate(8deg)" },
          "75%":       { transform: "rotate(-5deg)" },
        },
        // Logo loader: each wavy path dissolves left→right, reappears right→left
        "dissolve-flow": {
          "0%, 100%":  { opacity: "1",    transform: "translateX(0px)" },
          "35%":       { opacity: "0.08", transform: "translateX(7px)"  },
          "36%":       { opacity: "0",    transform: "translateX(-6px)" },
          "65%":       { opacity: "0.08", transform: "translateX(-1px)" },
          "80%":       { opacity: "1",    transform: "translateX(0px)"  },
        },
        "logo-pulse": {
          "0%, 100%": { opacity: "1",   transform: "scale(1)"    },
          "50%":      { opacity: "0.85", transform: "scale(0.97)" },
        },
        "fade-in-up": {
          "0%":   { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)"    },
        },
      },
      animation: {
        wiggle:         "wiggle 0.6s ease-in-out",
        "dissolve-flow": "dissolve-flow 1.8s ease-in-out infinite",
        "logo-pulse":   "logo-pulse 2s ease-in-out infinite",
        "fade-in-up":   "fade-in-up 0.4s ease-out forwards",
      },
    },
  },
  plugins: [],
};

export default config;
