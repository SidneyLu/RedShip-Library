/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#f8f6f3",
        ink: "#1c1917",
        muted: "#78716c",
        crimson: {
          50: "#fff1f2",
          200: "#fecdd3",
          400: "#fb7185",
          500: "#f43f5e",
          700: "#be123c",
          800: "#9f1239",
        },
        border: "#e7e5e4",
        card: "#ffffff",
      },
    },
  },
  plugins: [],
};
