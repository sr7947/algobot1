/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      colors: {
        navy: { 900: '#0a0e1a', 800: '#111827', 700: '#1f2937' },
        accent: { DEFAULT: '#3b82f6', light: '#60a5fa' },
        profit: '#10b981',
        loss: '#ef4444',
        warn: '#f59e0b',
      },
      animation: {
        'fade-in': 'fadeIn 0.3s ease-out',
        'slide-in': 'slideIn 0.25s ease-out',
        'pulse-green': 'pulseGreen 2s ease-in-out infinite',
        'pulse-red': 'pulseRed 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        slideIn: { '0%': { transform: 'translateY(10px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
        pulseGreen: { '0%, 100%': { boxShadow: '0 0 0 0 rgba(16,185,129,0.4)' }, '50%': { boxShadow: '0 0 0 8px rgba(16,185,129,0)' } },
        pulseRed: { '0%, 100%': { boxShadow: '0 0 0 0 rgba(239,68,68,0.4)' }, '50%': { boxShadow: '0 0 0 8px rgba(239,68,68,0)' } },
      },
    },
  },
  plugins: [],
};
