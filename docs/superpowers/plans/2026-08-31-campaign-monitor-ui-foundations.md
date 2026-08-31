# Campaign Monitor UI Foundations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the semantic design tokens and shared UI primitives (`StatusChip`, `ProgressBar`, `StepStrip`, `StatTile`, `Avatar`, `ActivityFeed`) that every redesigned Monitor page (Campaign Monitor, Live Campaigns, Agent Roster) will consume, so those pages can be built against a stable, single-source-of-truth component set instead of each reimplementing badges/bars/tables ad hoc as they do today.

**Architecture:** Extend the existing shadcn-style CSS-variable token system in `frontend/src/styles.css` (already slate-based — no palette migration needed) with 6 semantic status tones (success/info/warning/danger/neutral/acw), each as a 3-layer `bg`/`fg`/`bar` triplet matching the verified reference-design hex values. Build presentational primitives in the currently-empty `frontend/src/components/ui/` directory on top of those tones. Domain-specific status mapping (e.g. "which tone does a `time_capped` bucket get") is intentionally NOT part of this plan — each consuming page-plan defines its own mapper function from its domain enum to `StatusTone`, keeping these primitives domain-agnostic and reusable across Campaign Monitor, Live Campaigns, and Agent Roster.

**Tech Stack:** React 18 + TypeScript, Tailwind CSS (config-driven, no CSS-in-JS), Vitest (`environment: 'node'`, no jsdom/`@testing-library/react` in this repo — see Testing Note below), `clsx` + `tailwind-merge` via the existing `cn()` helper in `frontend/src/lib/utils.ts`.

**Spec:** Design catalog cross-referenced from `/mnt/c/Users/juansebastian/Downloads/Campaign Performance Dashboard (1)/{Campaign Monitor,Live Campaigns,Agent Roster}.dc.html` and `_ds/.../colors_and_type.css` (reference mockups + design system, verified against rendered screenshots) and the current-state inventory of `vip-connect-external-campaigns/frontend`. No separate written spec file exists yet — this plan's Architecture section is the spec for this subsystem.

## Global Constraints

- Do not modify `.dark {}` in `styles.css` — dark mode is not active on any page in this app today; new tokens are light-theme-only (`:root` block).
- Do not touch the existing `Badge`/`Card`/`Button`/etc. in `frontend/src/components/ui.tsx` — they stay as-is for pages that already use them. New primitives live in `frontend/src/components/ui/` (currently empty) and are additive, not a replacement of `ui.tsx`.
- `ProgressBar` must never render a bare percentage — always `value / max · pct%` text, per the reference design (`colors_and_type.css` brief note: "Progress bars: show numerator/denominator text beside the bar always").
- **Testing Note:** this repo has zero component-rendering tests (no jsdom, no `@testing-library/react` — confirmed via `vitest.config.ts`: `environment: 'node'`). Do not add that infrastructure as a side effect of this plan. Follow the existing convention exactly: extract pure logic (formatting, class-name resolution, string parsing) into plain exported functions and unit-test those with Vitest; leave JSX untested, exactly as `EnableCampaignModal.tsx`/`ui.tsx` do today.
- `npm run lint` is broken repo-wide today (ESLint 8.57.1, no `eslint.config.js` present — pre-existing, unrelated to this change). Do not attempt to fix it as part of this plan; verification steps below use `typecheck`, `test`, and `build` instead.

---

## File Structure

```
frontend/src/
  styles.css                     # MODIFY — add 21 new CSS vars (Task 1)
  ../tailwind.config.ts          # MODIFY — wire vars into `status`/`track` color keys (Task 1)
  components/ui/
    status.ts                    # NEW — StatusTone type, STATUS_TONE_CLASSES map, clampPercent, formatProgress (Task 2)
    status.test.ts                # NEW (Task 2)
    StatusChip.tsx                # NEW (Task 3)
    ProgressBar.tsx               # NEW (Task 4)
    StepStrip.tsx                 # NEW (Task 5)
    StatTile.tsx                  # NEW (Task 6)
    Avatar.tsx                    # NEW — component + initialsFromName (Task 7)
    Avatar.test.ts                 # NEW (Task 7)
    ActivityFeed.tsx              # NEW (Task 8)
    index.ts                      # NEW — barrel export (Task 9)
```

---

### Task 1: Design tokens — status tones + track accents

**Files:**
- Modify: `frontend/src/styles.css:6-20` (inside existing `:root` block)
- Modify: `frontend/tailwind.config.ts:11-31` (inside existing `theme.extend.colors`)

**Interfaces:**
- Produces: CSS custom properties `--success-bg`, `--success-fg`, `--success-bar`, `--info-bg`, `--info-fg`, `--info-bar`, `--warning-bg`, `--warning-fg`, `--warning-bar`, `--danger-bg`, `--danger-fg`, `--danger-bar`, `--neutral-bg`, `--neutral-fg`, `--neutral-bar`, `--acw-bg`, `--acw-fg`, `--acw-bar`, `--track1-accent`, `--track1-accent-active`, `--track2-accent`. Produces Tailwind utility classes `bg-status-{tone}-bg`, `text-status-{tone}-fg`, `bg-status-{tone}-bar` for `tone ∈ {success,info,warning,danger,neutral,acw}`, plus `bg-track-1`, `bg-track-1-active`, `bg-track-2` / `border-track-1` / `border-track-2` (any Tailwind property, since these resolve to plain color values). Task 2 consumes these class names.

- [ ] **Step 1: Add the CSS variables**

Edit `frontend/src/styles.css`, inside the existing `:root { ... }` block (after `--radius: 0.5rem;`, before the closing `}`):

```css
    /* ---------- Status tones (3-layer: bg tint / fg text / bar fill) ---------- */
    --success-bg:  #dcfce7;
    --success-fg:  #15803d;
    --success-bar: #16a34a;

    --info-bg:  #dbeafe;
    --info-fg:  #1d4ed8;
    --info-bar: #2563eb;

    --warning-bg:  #fef3c7;
    --warning-fg:  #b45309;
    --warning-bar: #d97706;

    --danger-bg:  #fee2e2;
    --danger-fg:  #b91c1c;
    --danger-bar: #b91c1c;

    --neutral-bg:  #f1f5f9;
    --neutral-fg:  #64748b;
    --neutral-bar: #cbd5e1;

    --acw-bg:  #ede9fe;
    --acw-fg:  #6d28d9;
    --acw-bar: #8b5cf6;

    /* ---------- Track accents (Campaign Monitor timeline/step-strip) ---------- */
    --track1-accent:        #0d9488;
    --track1-accent-active: #14b8a6;
    --track2-accent:        #7c3aed;
```

- [ ] **Step 2: Wire the variables into Tailwind**

Edit `frontend/tailwind.config.ts`, inside `theme.extend.colors` (after the existing `destructive` entry, before the closing `},` of `colors`):

```ts
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
```

- [ ] **Step 3: Verify the config parses and Tailwind picks up the classes**

Run: `cd frontend && npx tsc --noEmit tailwind.config.ts 2>&1 | head -20 && echo '<div class="bg-status-success-bg text-status-success-fg bg-track-1"></div>' > /tmp/tw-probe.html && npx tailwindcss -i ./src/styles.css -o /tmp/tw-probe-out.css --content /tmp/tw-probe.html 2>&1 | tail -20 && grep -c "status-success-bg" /tmp/tw-probe-out.css`

Expected: no TypeScript errors from the config file, and the grep prints a number ≥ 1 (the utility class was generated). If `npx tailwindcss` isn't available as a standalone CLI in this project (Tailwind v4 changed the CLI story), skip the standalone probe and instead confirm via Task 9's full `npm run build`, which will fail loudly if the config is malformed.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles.css frontend/tailwind.config.ts
git commit -m "feat(frontend): add status-tone and track-accent design tokens"
```

---

### Task 2: `status.ts` — StatusTone type, tone→class map, progress formatting

**Files:**
- Create: `frontend/src/components/ui/status.ts`
- Test: `frontend/src/components/ui/status.test.ts`

**Interfaces:**
- Consumes: Tailwind classes produced by Task 1 (`bg-status-*-bg`, `text-status-*-fg`, `bg-status-*-bar`).
- Produces: `type StatusTone = 'success' | 'info' | 'warning' | 'danger' | 'neutral' | 'acw'`; `STATUS_TONE_CLASSES: Record<StatusTone, { bg: string; fg: string; bar: string; dot: string }>`; `clampPercent(value: number, max: number): number`; `formatProgress(value: number, max: number): string`. Tasks 3, 4, 5, 6, 7, 8 all consume `StatusTone` and `STATUS_TONE_CLASSES`; Task 4 also consumes `clampPercent` and `formatProgress`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/components/ui/status.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import { clampPercent, formatProgress } from './status';

describe('clampPercent', () => {
  it('computes a normal percentage', () => {
    expect(clampPercent(112, 240)).toBeCloseTo(46.666, 2);
  });

  it('returns 0 when max is 0', () => {
    expect(clampPercent(5, 0)).toBe(0);
  });

  it('caps at 100 when value exceeds max', () => {
    expect(clampPercent(300, 240)).toBe(100);
  });

  it('returns 0 for a value of 0', () => {
    expect(clampPercent(0, 240)).toBe(0);
  });
});

describe('formatProgress', () => {
  it('formats a normal ratio with rounded percent', () => {
    expect(formatProgress(1240, 2980)).toBe('1,240 / 2,980 · 42%');
  });

  it('rounds up at the .5 boundary', () => {
    expect(formatProgress(112, 240)).toBe('112 / 240 · 47%');
  });

  it('marks over-100% (redials) explicitly instead of capping silently', () => {
    expect(formatProgress(255, 240)).toBe('255 / 240 · 100%+');
  });

  it('handles a zero denominator without throwing', () => {
    expect(formatProgress(0, 0)).toBe('0 / 0 · 0%');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/components/ui/status.test.ts`
Expected: FAIL — `Cannot find module './status'` (file doesn't exist yet).

- [ ] **Step 3: Write the implementation**

Create `frontend/src/components/ui/status.ts`:

```ts
export type StatusTone = 'success' | 'info' | 'warning' | 'danger' | 'neutral' | 'acw';

type ToneClasses = { bg: string; fg: string; bar: string; dot: string };

export const STATUS_TONE_CLASSES: Record<StatusTone, ToneClasses> = {
  success: {
    bg: 'bg-status-success-bg',
    fg: 'text-status-success-fg',
    bar: 'bg-status-success-bar',
    dot: 'bg-status-success-bar',
  },
  info: {
    bg: 'bg-status-info-bg',
    fg: 'text-status-info-fg',
    bar: 'bg-status-info-bar',
    dot: 'bg-status-info-bar',
  },
  warning: {
    bg: 'bg-status-warning-bg',
    fg: 'text-status-warning-fg',
    bar: 'bg-status-warning-bar',
    dot: 'bg-status-warning-bar',
  },
  danger: {
    bg: 'bg-status-danger-bg',
    fg: 'text-status-danger-fg',
    bar: 'bg-status-danger-bar',
    dot: 'bg-status-danger-bar',
  },
  neutral: {
    bg: 'bg-status-neutral-bg',
    fg: 'text-status-neutral-fg',
    bar: 'bg-status-neutral-bar',
    dot: 'bg-status-neutral-bar',
  },
  acw: {
    bg: 'bg-status-acw-bg',
    fg: 'text-status-acw-fg',
    bar: 'bg-status-acw-bar',
    dot: 'bg-status-acw-bar',
  },
};

/** Percentage of `value` out of `max`, clamped to [0, 100]. `max <= 0` returns 0. */
export function clampPercent(value: number, max: number): number {
  if (max <= 0) return 0;
  return Math.min(100, Math.max(0, (value / max) * 100));
}

/**
 * "value / max · pct%" — the reference design requires the numerator/denominator
 * text beside every progress bar, never a bare percentage. When `value` exceeds
 * `max` (redials past the segment size), shows "100%+" instead of silently
 * capping the displayed number, since that's a materially different situation
 * from "exactly done".
 */
export function formatProgress(value: number, max: number): string {
  const suffix = value > max && max > 0 ? '100%+' : `${Math.round(clampPercent(value, max))}%`;
  return `${value.toLocaleString()} / ${max.toLocaleString()} · ${suffix}`;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/ui/status.test.ts`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ui/status.ts frontend/src/components/ui/status.test.ts
git commit -m "feat(frontend): add StatusTone map and progress-formatting helpers"
```

---

### Task 3: `StatusChip.tsx`

**Files:**
- Create: `frontend/src/components/ui/StatusChip.tsx`

**Interfaces:**
- Consumes: `StatusTone`, `STATUS_TONE_CLASSES` from `./status` (Task 2); `cn` from `@/lib/utils`.
- Produces: `StatusChip` component with props `{ tone: StatusTone; label: string; pulse?: boolean; mono?: boolean; className?: string }`. Consumed by every page-plan (Campaign Monitor bucket/state chips, Live Campaigns campaign status chips, Agent Roster status pills).

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ui/StatusChip.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export function StatusChip({
  tone,
  label,
  pulse = false,
  mono = false,
  className,
}: {
  tone: StatusTone;
  label: string;
  pulse?: boolean;
  mono?: boolean;
  className?: string;
}): ReactNode {
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
        toneClasses.bg,
        toneClasses.fg,
        mono && 'font-mono',
        className,
      )}
    >
      {pulse ? (
        <span aria-hidden className={cn('h-1.5 w-1.5 rounded-full animate-pulse', toneClasses.dot)} />
      ) : null}
      {label}
    </span>
  );
}
```

No dedicated test file — this is pure presentational JSX with no branching logic beyond the tone lookup already covered by Task 2's tests, matching the existing convention (`Badge`, `Spinner` in `ui.tsx` have no tests either).

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/StatusChip.tsx
git commit -m "feat(frontend): add StatusChip primitive"
```

---

### Task 4: `ProgressBar.tsx`

**Files:**
- Create: `frontend/src/components/ui/ProgressBar.tsx`

**Interfaces:**
- Consumes: `StatusTone`, `STATUS_TONE_CLASSES`, `clampPercent`, `formatProgress` from `./status` (Task 2).
- Produces: `ProgressBar` component with props `{ value: number; max: number; tone?: StatusTone; label?: string; className?: string }` (default `tone='info'`). Consumed by Campaign Monitor (bucket contact/time progress), Live Campaigns (campaign dial progress).

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ui/ProgressBar.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { clampPercent, formatProgress, STATUS_TONE_CLASSES, type StatusTone } from './status';

export function ProgressBar({
  value,
  max,
  tone = 'info',
  label,
  className,
}: {
  value: number;
  max: number;
  tone?: StatusTone;
  label?: string;
  className?: string;
}): ReactNode {
  const pct = clampPercent(value, max);
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <div className="flex items-center justify-between text-xs">
        {label ? <span className="text-muted-foreground">{label}</span> : <span />}
        <span className="font-mono text-foreground">{formatProgress(value, max)}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-[width] duration-500', toneClasses.bar)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/ProgressBar.tsx
git commit -m "feat(frontend): add ProgressBar primitive with numerator/denominator text"
```

---

### Task 5: `StepStrip.tsx`

**Files:**
- Create: `frontend/src/components/ui/StepStrip.tsx`

**Interfaces:**
- Consumes: `STATUS_TONE_CLASSES`, `StatusTone` from `./status` (Task 2).
- Produces: `StepStrip` component with props `{ steps: Array<{ tone: StatusTone; pulse?: boolean }>; className?: string }`. **Generalized to N steps** — the reference mockup hardcodes exactly 9 (one per state campaign: NY·1, NY·2, CA·1, CA·2, NJ, MD, CT, PA, TX), but per the "generalizar" decision, this component takes an arbitrary-length array; the Campaign Monitor page-plan is responsible for mapping a bucket's actual `campaigns.length` to a `steps` array, not this primitive.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ui/StepStrip.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export type StepStripStep = {
  tone: StatusTone;
  pulse?: boolean;
};

export function StepStrip({
  steps,
  className,
}: {
  steps: StepStripStep[];
  className?: string;
}): ReactNode {
  return (
    <div className={cn('flex items-center gap-1', className)} role="list">
      {steps.map((step, i) => (
        <span
          key={i}
          role="listitem"
          className={cn('h-2 flex-1 rounded-full', STATUS_TONE_CLASSES[step.tone].bar, step.pulse && 'animate-pulse')}
        />
      ))}
    </div>
  );
}
```

No dedicated test file — pure presentational mapping, same rationale as Task 3.

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/StepStrip.tsx
git commit -m "feat(frontend): add StepStrip primitive (N-step sequence, not fixed to 9)"
```

---

### Task 6: `StatTile.tsx`

**Files:**
- Create: `frontend/src/components/ui/StatTile.tsx`

**Interfaces:**
- Consumes: `cn` from `@/lib/utils` only (no status-tone dependency — stat values use plain foreground/muted text by default, with an optional override for cases like "Available: 0 agents" needing to render red).
- Produces: `StatTile` component with props `{ label: string; value: ReactNode; valueClassName?: string; className?: string }`. Consumed by Campaign Monitor (Track 1 stat grid: Cycle today, Time in cycle, etc.) and Agent Roster (workforce summary tiles).

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ui/StatTile.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export function StatTile({
  label,
  value,
  valueClassName,
  className,
}: {
  label: string;
  value: ReactNode;
  valueClassName?: string;
  className?: string;
}): ReactNode {
  return (
    <div className={cn('rounded-lg border border-border bg-muted/40 p-3', className)}>
      <div className={cn('font-mono text-lg font-semibold tabular-nums text-foreground', valueClassName)}>
        {value}
      </div>
      <div className="mt-0.5 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/StatTile.tsx
git commit -m "feat(frontend): add StatTile primitive"
```

---

### Task 7: `Avatar.tsx` + `initialsFromName`

**Files:**
- Create: `frontend/src/components/ui/Avatar.tsx`
- Test: `frontend/src/components/ui/Avatar.test.ts`

**Interfaces:**
- Consumes: `STATUS_TONE_CLASSES`, `StatusTone` from `./status` (Task 2).
- Produces: `initialsFromName(name: string): string`; `Avatar` component with props `{ name: string; tone?: StatusTone; className?: string }` (default `tone='neutral'`). Consumed by Agent Roster (agent list rows) and Campaign Monitor's Agent availability panel if it ever lists individuals.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/components/ui/Avatar.test.ts`:

```ts
import { describe, expect, it } from 'vitest';

import { initialsFromName } from './Avatar';

describe('initialsFromName', () => {
  it('takes first letter of first and last name', () => {
    expect(initialsFromName('Diego Santos')).toBe('DS');
  });

  it('handles a single name with one initial', () => {
    expect(initialsFromName('Ana')).toBe('A');
  });

  it('collapses extra internal whitespace', () => {
    expect(initialsFromName('  Tom   Fisher  ')).toBe('TF');
  });

  it('uses first and last of a three-plus-word name', () => {
    expect(initialsFromName('Maria De La Cruz')).toBe('MC');
  });

  it('falls back to "?" for an empty string', () => {
    expect(initialsFromName('')).toBe('?');
  });

  it('uppercases lowercase input', () => {
    expect(initialsFromName('jack ryan')).toBe('JR');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/components/ui/Avatar.test.ts`
Expected: FAIL — `Cannot find module './Avatar'`.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/components/ui/Avatar.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

/** "Diego Santos" → "DS"; single word → 1 letter; empty → "?". */
export function initialsFromName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0]!.slice(0, 1).toUpperCase();
  return `${parts[0]!.slice(0, 1)}${parts[parts.length - 1]!.slice(0, 1)}`.toUpperCase();
}

export function Avatar({
  name,
  tone = 'neutral',
  className,
}: {
  name: string;
  tone?: StatusTone;
  className?: string;
}): ReactNode {
  const toneClasses = STATUS_TONE_CLASSES[tone];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold',
        toneClasses.bg,
        toneClasses.fg,
        className,
      )}
    >
      {initialsFromName(name)}
    </span>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/ui/Avatar.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ui/Avatar.tsx frontend/src/components/ui/Avatar.test.ts
git commit -m "feat(frontend): add Avatar primitive with initialsFromName helper"
```

---

### Task 8: `ActivityFeed.tsx`

**Files:**
- Create: `frontend/src/components/ui/ActivityFeed.tsx`

**Interfaces:**
- Consumes: `STATUS_TONE_CLASSES`, `StatusTone` from `./status` (Task 2).
- Produces: `ActivityFeedItem` type `{ id: string; timestampLabel: string; text: string; tone: StatusTone }`; `ActivityFeed` component with props `{ items: ActivityFeedItem[]; className?: string; emptyLabel?: string }`. Consumed by the Campaign Monitor page-plan's "Day activity feed" panel — that plan's backend task is responsible for producing `timestampLabel` already formatted in ET (this component does no timezone conversion itself, matching the brief's explicit ET-labeling requirement being a data-layer concern, not a presentation concern).

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ui/ActivityFeed.tsx`:

```tsx
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { STATUS_TONE_CLASSES, type StatusTone } from './status';

export type ActivityFeedItem = {
  id: string;
  timestampLabel: string;
  text: string;
  tone: StatusTone;
};

export function ActivityFeed({
  items,
  className,
  emptyLabel = 'No activity yet.',
}: {
  items: ActivityFeedItem[];
  className?: string;
  emptyLabel?: string;
}): ReactNode {
  if (items.length === 0) {
    return <p className={cn('text-sm text-muted-foreground', className)}>{emptyLabel}</p>;
  }
  return (
    <ol className={cn('flex max-h-[360px] flex-col gap-2 overflow-y-auto', className)}>
      {items.map((item) => (
        <li key={item.id} className="flex items-start gap-2 text-xs">
          <span className="w-[52px] shrink-0 pt-0.5 text-right font-mono text-muted-foreground">
            {item.timestampLabel}
          </span>
          <span
            aria-hidden
            className={cn('mt-1 h-1.5 w-1.5 shrink-0 rounded-full', STATUS_TONE_CLASSES[item.tone].bar)}
          />
          <span className="text-foreground/80">{item.text}</span>
        </li>
      ))}
    </ol>
  );
}
```

No dedicated test file — pure presentational; formatting of `timestampLabel` is the caller's concern and will be tested where that formatting function is written (Campaign Monitor page-plan).

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/ActivityFeed.tsx
git commit -m "feat(frontend): add ActivityFeed primitive"
```

---

### Task 9: Barrel export + full verification

**Files:**
- Create: `frontend/src/components/ui/index.ts`

**Interfaces:**
- Consumes: everything from Tasks 2–8.
- Produces: a single import surface `import { StatusChip, ProgressBar, StepStrip, StatTile, Avatar, ActivityFeed, type StatusTone, STATUS_TONE_CLASSES, clampPercent, formatProgress, initialsFromName } from '@/components/ui'` for every future page-plan (Campaign Monitor, Live Campaigns, Agent Roster) to consume.

- [ ] **Step 1: Write the barrel**

Create `frontend/src/components/ui/index.ts`:

```ts
export * from './status';
export * from './StatusChip';
export * from './ProgressBar';
export * from './StepStrip';
export * from './StatTile';
export * from './Avatar';
export * from './ActivityFeed';
```

- [ ] **Step 2: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: all existing tests still pass, plus the new `status.test.ts` (8 tests) and `Avatar.test.ts` (6 tests).

- [ ] **Step 3: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both succeed with no new errors. (Skip `npm run lint` — pre-existing broken baseline, see Global Constraints.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ui/index.ts
git commit -m "feat(frontend): barrel-export the new Monitor UI primitives"
```

---

## Self-Review Notes

- **Spec coverage:** every primitive identified as reusable-across-pages in the design catalog (status chip, progress bar w/ numerator-denominator, N-step strip, stat tile, avatar w/ initials, activity feed) has a task. Domain-specific mapping (bucket lifecycle states, agent Connect statuses, routing-profile risk levels) is explicitly deferred to the page-level plans that follow this one (Campaign Monitor, Live Campaigns, Agent Roster), since those require the Fase 2 backend work to land first per the "fundaciones primero" ordering.
- **Placeholder scan:** none — every task has complete, runnable code and exact file paths.
- **Type consistency:** `StatusTone` is defined once in Task 2 and imported (never redeclared) by every subsequent task; `STATUS_TONE_CLASSES` keys are used identically (`bg`/`fg`/`bar`/`dot`) everywhere it's consumed.
