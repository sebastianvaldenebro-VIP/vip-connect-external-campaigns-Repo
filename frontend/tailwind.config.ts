import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
      },
      colors: {
        border: 'hsl(var(--border))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        status: {
          success: { bg: 'var(--success-bg)', fg: 'var(--success-fg)', bar: 'var(--success-bar)' },
          info: { bg: 'var(--info-bg)', fg: 'var(--info-fg)', bar: 'var(--info-bar)' },
          warning: { bg: 'var(--warning-bg)', fg: 'var(--warning-fg)', bar: 'var(--warning-bar)' },
          danger: { bg: 'var(--danger-bg)', fg: 'var(--danger-fg)', bar: 'var(--danger-bar)' },
          neutral: { bg: 'var(--neutral-bg)', fg: 'var(--neutral-fg)', bar: 'var(--neutral-bar)' },
          acw: { bg: 'var(--acw-bg)', fg: 'var(--acw-fg)', bar: 'var(--acw-bar)' },
        },
        track: {
          1: 'var(--track1-accent)',
          '1-active': 'var(--track1-accent-active)',
          2: 'var(--track2-accent)',
        },
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
    },
  },
  plugins: [],
} satisfies Config;
