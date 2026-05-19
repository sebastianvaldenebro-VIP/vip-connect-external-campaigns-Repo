import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Button, Card, Field, Input, Select, Spinner } from '@/components/ui';
import { EnableCampaignModal } from '@/components/EnableCampaignModal';
import { api, type ReconcileResult, type SegmentSummary, type VerifyResult } from '@/lib/api';
import { buildSegmentGroups, type Rule } from '@/lib/segmentGroups';
import {
  STATE_LOCATION_MAP,
  codesForStates,
  locationsForStates,
} from '@/lib/stateLocationMap';

type AvailableFilter = 'any' | 'yes' | 'no';

export function SegmentNew(): ReactNode {
  const navigate = useNavigate();

  const [selectedStates, setSelectedStates] = useState<string[]>([]);
  const [selectedLocations, setSelectedLocations] = useState<string[]>([]);
  const [availableFilter, setAvailableFilter] = useState<AvailableFilter>('yes');
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [selectedAttempts, setSelectedAttempts] = useState<string[]>([]);
  const [displayName, setDisplayName] = useState('');
  const [nameOverride, setNameOverride] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [justCreated, setJustCreated] = useState<SegmentSummary | null>(null);
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [reconcileResult, setReconcileResult] = useState<ReconcileResult | null>(null);
  const [enableOpen, setEnableOpen] = useState(false);

  const availableLocationOptions = useMemo(
    () => locationsForStates(selectedStates),
    [selectedStates],
  );

  // State toggle behaviour: when a state gets added, all its locations join the
  // selection; when a state goes away, its locations leave too. The operator
  // can still deselect individual locations after to narrow the filter.
  const prevStatesRef = useRef<string[]>([]);
  useEffect(() => {
    const prev = new Set(prevStatesRef.current);
    const curr = new Set(selectedStates);
    const added = selectedStates.filter((s) => !prev.has(s));
    const removed = prevStatesRef.current.filter((s) => !curr.has(s));

    if (added.length > 0) {
      const locsToAdd = locationsForStates(added);
      setSelectedLocations((prev2) => Array.from(new Set([...prev2, ...locsToAdd])));
    }
    if (removed.length > 0) {
      const locsToRemove = new Set(locationsForStates(removed));
      setSelectedLocations((prev2) => prev2.filter((l) => !locsToRemove.has(l)));
    }
    prevStatesRef.current = selectedStates;
  }, [selectedStates]);

  const attemptsQuery = useQuery({
    queryKey: ['leads', 'distinct', 'attempt'],
    queryFn: () => api.leads.distinctValues('attempt'),
  });

  const groupsQuery = useQuery({
    queryKey: ['leads', 'distinct', 'groups'],
    queryFn: () => api.leads.distinctValues('groups'),
  });

  // Auto-generated segment name — DD-MM-YYYY-STATES-GROUPS-ATTEMPTS. Operator
  // can override it by typing; nameOverride takes precedence when non-null.
  const autoName = useMemo(
    () => buildAutoName(selectedStates, selectedGroups, selectedAttempts),
    [selectedStates, selectedGroups, selectedAttempts],
  );
  // AWS Customer Profiles enforces ^[a-zA-Z0-9_-]+$ on SegmentDefinitionName.
  // Replace anything else (spaces, dots, slashes) with underscores so a typo
  // doesn't surface as a 400 from the API.
  const segmentName = sanitizeSegmentName(nameOverride ?? autoName);

  // ── Preview count ───────────────────────────────────────────────
  // Debounced 1s to avoid hammering Redis/CP while the user is still clicking.
  const [preview, setPreview] = useState<
    { status: 'idle' | 'loading' | 'error'; redis?: number; cp?: number | null; message?: string }
  >({ status: 'idle' });
  const previewTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const currentFilters = useMemo<Rule[]>(() => {
    const rules: Rule[] = [];
    const locations =
      selectedLocations.length > 0 ? selectedLocations : availableLocationOptions;
    if (locations.length > 0) {
      rules.push({ field: 'location', operator: 'INCLUSIVE', values: locations });
    }
    if (availableFilter === 'yes') {
      // Customer Profiles stores `available` as the capitalized strings "True"/"False",
      // so the AttributeDimension EQUAL match must use those exact strings.
      rules.push({ field: 'available', operator: 'INCLUSIVE', values: ['True'] });
    } else if (availableFilter === 'no') {
      rules.push({ field: 'available', operator: 'INCLUSIVE', values: ['False'] });
    }
    if (selectedGroups.length > 0) {
      rules.push({
        field: 'groups',
        operator: 'INCLUSIVE',
        values: selectedGroups,
      });
    }
    if (selectedAttempts.length > 0) {
      rules.push({
        field: 'attempt',
        operator: 'INCLUSIVE',
        values: selectedAttempts,
      });
    }
    return rules;
  }, [
    selectedLocations,
    availableLocationOptions,
    availableFilter,
    selectedGroups,
    selectedAttempts,
  ]);

  useEffect(() => {
    if (previewTimer.current) clearTimeout(previewTimer.current);
    if (currentFilters.length === 0) {
      setPreview({ status: 'idle' });
      return;
    }
    previewTimer.current = setTimeout(async () => {
      setPreview({ status: 'loading' });
      try {
        const segmentGroups = buildSegmentGroups(currentFilters, 'ALL');
        const result = await api.previewCount({ segmentGroups });
        setPreview({
          status: 'idle',
          redis: result.redisCount,
          cp: result.segmentCount,
        });
      } catch (err: unknown) {
        setPreview({
          status: 'error',
          message: err instanceof Error ? err.message : 'Preview failed',
        });
      }
    }, 1_000);
    return () => {
      if (previewTimer.current) clearTimeout(previewTimer.current);
    };
  }, [currentFilters]);

  // ── Submit ──────────────────────────────────────────────────────
  const create = useMutation({
    mutationFn: () => {
      const segmentGroups = buildSegmentGroups(currentFilters, 'ALL');
      if (segmentGroups.Groups[0].Dimensions.length === 0) {
        throw new Error(
          'Add at least one filter (state, available, groups, or attempts).',
        );
      }
      return api.segments.create({
        name: segmentName,
        displayName: displayName || segmentName,
        description: buildDescription(
          selectedStates,
          selectedLocations,
          availableFilter,
          selectedGroups,
          selectedAttempts,
        ),
        segmentGroups,
        // Manual/live distinction was retired — every new segment is
        // reconcilable. Backend defaults this to "manual" too, but the
        // type still requires it to be present.
        syncMode: 'manual',
      });
    },
    onSuccess: (created) => setJustCreated(created),
    onError: (err: Error) => setError(err.message),
  });

  const verify = useMutation({
    mutationFn: (name: string) => api.segments.verify(name),
    onSuccess: (result) => setVerifyResult(result),
  });

  const reconcile = useMutation({
    mutationFn: (name: string) => api.segments.reconcile(name),
    onSuccess: (result) => {
      setReconcileResult(result);
      setVerifyResult(null);
    },
  });

  const resetForm = () => {
    setSelectedStates([]);
    setSelectedLocations([]);
    setAvailableFilter('yes');
    setSelectedGroups([]);
    setSelectedAttempts([]);
    setDisplayName('');
    setNameOverride(null);
    setError(null);
    setJustCreated(null);
    setVerifyResult(null);
    setReconcileResult(null);
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    create.mutate();
  };

  if (justCreated) {
    // After reconcile the segment gets a new name + ARN — use those for Enable Campaign.
    const currentSegment = reconcileResult
      ? { name: reconcileResult.newSegmentName, segmentArn: reconcileResult.newSegmentArn }
      : { name: justCreated.name, segmentArn: justCreated.segmentArn };

    // Which name to run verify/reconcile against: if reconciled, use the new name.
    const activeSegmentName = currentSegment.name;

    return (
      <div className="flex flex-col gap-6">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Segment created</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            <code className="font-mono">{currentSegment.name}</code>
            {reconcileResult ? (
              <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide">
                v{reconcileResult.newVersion} reconciled
              </span>
            ) : null}
          </p>
        </header>

        <Card className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="outline"
              onClick={() => verify.mutate(activeSegmentName)}
              disabled={verify.isPending || reconcile.isPending}
            >
              {verify.isPending ? (
                <span className="inline-flex items-center gap-2"><Spinner /> Verifying…</span>
              ) : 'Verify'}
            </Button>
            <Button
              variant="outline"
              onClick={() => reconcile.mutate(activeSegmentName)}
              disabled={reconcile.isPending || verify.isPending}
            >
              {reconcile.isPending ? (
                <span className="inline-flex items-center gap-2"><Spinner /> Reconciling…</span>
              ) : 'Reconcile'}
            </Button>
            <Button onClick={() => setEnableOpen(true)} disabled={reconcile.isPending}>
              Enable Campaign
            </Button>
            <Button variant="outline" onClick={() => navigate('/segments')}>
              Back to list
            </Button>
            <Button variant="ghost" onClick={resetForm}>
              Create another
            </Button>
          </div>

          {verify.isError ? (
            <p className="text-sm text-destructive">{(verify.error as Error).message}</p>
          ) : null}
          {reconcile.isError ? (
            <p className="text-sm text-destructive">{(reconcile.error as Error).message}</p>
          ) : null}

          {verifyResult ? (
            <div className="rounded-md border border-border bg-muted/30 p-3 text-sm">
              <p className="font-semibold">Verify result</p>
              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs md:grid-cols-4">
                <div><dt className="text-muted-foreground">Redis</dt><dd className="font-mono">{verifyResult.redisCount.toLocaleString()}</dd></div>
                <div><dt className="text-muted-foreground">Segment (CP)</dt><dd className="font-mono">{verifyResult.segmentCount.toLocaleString()}</dd></div>
                <div><dt className="text-muted-foreground">Missing</dt><dd className="font-mono">{verifyResult.missingCustomerIds.length.toLocaleString()}</dd></div>
                <div><dt className="text-muted-foreground">Extras</dt><dd className="font-mono">{verifyResult.extraCustomerIds.length.toLocaleString()}</dd></div>
              </dl>
            </div>
          ) : null}

          {reconcileResult ? (
            <div className="rounded-md border border-border bg-muted/30 p-3 text-sm">
              <p className="font-semibold">Reconcile result</p>
              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs md:grid-cols-4">
                <div><dt className="text-muted-foreground">New name</dt><dd className="font-mono">{reconcileResult.newSegmentName}</dd></div>
                <div><dt className="text-muted-foreground">Version</dt><dd className="font-mono">v{reconcileResult.newVersion}</dd></div>
                <div><dt className="text-muted-foreground">Added</dt><dd className="font-mono">+{reconcileResult.added.toLocaleString()}</dd></div>
                <div><dt className="text-muted-foreground">Removed</dt><dd className="font-mono">-{reconcileResult.removed.toLocaleString()}</dd></div>
              </dl>
            </div>
          ) : null}
        </Card>

        <EnableCampaignModal
          open={enableOpen}
          onClose={() => setEnableOpen(false)}
          segmentName={currentSegment.name}
          segmentArn={currentSegment.segmentArn}
          segmentStates={codesForStates(selectedStates)}
        />
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">New segment</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Compose a segment from the structured filters. Name auto-generates
            from State + Attempts; preview count runs ~1s after the last
            change.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" onClick={() => navigate('/segments')}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create segment'}
          </Button>
        </div>
      </header>

      {error ? (
        <Card className="border-destructive/50 bg-destructive/5 text-sm text-destructive">
          {error}
        </Card>
      ) : null}

      {/* ── Identity ──────────────────────────────────────────────── */}
      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Identity</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Name (auto-generated)" htmlFor="seg-name">
            <Input
              id="seg-name"
              value={segmentName}
              onChange={(e) =>
                setNameOverride(
                  e.target.value === autoName ? null : e.target.value,
                )
              }
              placeholder="23-04-2026-New_Jersey-2"
              required
            />
          </Field>
          <Field label="Display name (optional)" htmlFor="seg-display">
            <Input
              id="seg-display"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="NJ morning — attempt 2"
            />
          </Field>
        </div>
      </Card>

      {/* ── Location ─────────────────────────────────────────────── */}
      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Location</h2>
        <p className="text-xs text-muted-foreground">
          Pick one or more states first. Locations narrow to the states you
          chose. Leave locations empty to include all locations of the
          selected states.
        </p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr,2fr]">
          <Field label="State (multi-select)">
            <MultiSelect
              options={STATE_LOCATION_MAP.map((g) => g.state)}
              selected={selectedStates}
              onChange={setSelectedStates}
              placeholder="Select states…"
            />
          </Field>
          <Field
            label={`Location (multi-select${availableLocationOptions.length ? ` · ${availableLocationOptions.length} available` : ''})`}
          >
            <MultiSelect
              options={availableLocationOptions}
              selected={selectedLocations}
              onChange={setSelectedLocations}
              placeholder={
                availableLocationOptions.length === 0
                  ? 'Pick a state first'
                  : 'Leave empty to include all locations of selected states'
              }
              disabled={availableLocationOptions.length === 0}
            />
          </Field>
        </div>
      </Card>

      {/* ── Groups ──────────────────────────────────────────────── */}
      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Groups</h2>
        <p className="text-xs text-muted-foreground">
          Populated from Redis. Independent of availability — the final filter
          is an AND between groups + attempts + location + availability.
        </p>
        <Field
          label={`Groups (multi-select${groupsQuery.data ? ` · ${groupsQuery.data.values.length} distinct values in Redis` : ''})`}
        >
          {groupsQuery.isPending ? (
            <div className="flex h-9 items-center gap-2 text-xs text-muted-foreground">
              <Spinner /> loading distinct values…
            </div>
          ) : groupsQuery.isError ? (
            <p className="text-xs text-destructive">
              {(groupsQuery.error as Error).message}
            </p>
          ) : (
            <MultiSelect
              options={groupsQuery.data?.values ?? []}
              selected={selectedGroups}
              onChange={setSelectedGroups}
              placeholder="Leave empty to not filter on groups"
            />
          )}
        </Field>
      </Card>

      {/* ── Availability & attempts ──────────────────────────────── */}
      <Card className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold">Availability & attempts</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Available status">
            <Select
              value={availableFilter}
              onChange={(e) => setAvailableFilter(e.target.value as AvailableFilter)}
            >
              <option value="yes">Available (true)</option>
              <option value="no">Not available (false)</option>
              <option value="any">Any (no filter)</option>
            </Select>
          </Field>
          <Field
            label={`Attempt (multi-select${attemptsQuery.data ? ` · ${attemptsQuery.data.values.length} distinct values in Redis` : ''})`}
          >
            {attemptsQuery.isPending ? (
              <div className="flex h-9 items-center gap-2 text-xs text-muted-foreground">
                <Spinner /> loading distinct values…
              </div>
            ) : attemptsQuery.isError ? (
              <p className="text-xs text-destructive">
                {(attemptsQuery.error as Error).message}
              </p>
            ) : (
              <MultiSelect
                options={attemptsQuery.data?.values ?? []}
                selected={selectedAttempts}
                onChange={setSelectedAttempts}
                placeholder="Leave empty to not filter on attempt"
              />
            )}
          </Field>
        </div>
      </Card>

      {/* ── Preview ─────────────────────────────────────────────── */}
      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Preview count</h2>
        <PreviewView preview={preview} />
      </Card>
    </form>
  );
}

// ── Subcomponents ───────────────────────────────────────────────

function MultiSelect({
  options,
  selected,
  onChange,
  placeholder,
  disabled,
}: {
  options: readonly string[];
  selected: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
}): ReactNode {
  const toggle = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    );
  };
  return (
    <div
      className={
        disabled
          ? 'flex flex-col gap-2 opacity-60'
          : 'flex flex-col gap-2'
      }
    >
      <div className="flex flex-wrap gap-1 rounded-md border border-border bg-background p-2 min-h-10">
        {selected.length === 0 ? (
          <span className="text-xs text-muted-foreground">
            {placeholder ?? 'Nothing selected'}
          </span>
        ) : (
          selected.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => toggle(v)}
              disabled={disabled}
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs text-foreground hover:bg-primary/20"
            >
              <span>{v}</span>
              <span aria-hidden>×</span>
            </button>
          ))
        )}
      </div>
      {!disabled && options.length > 0 ? (
        <div className="max-h-48 overflow-auto rounded-md border border-border bg-muted/20 p-1">
          {options.map((opt) => {
            const active = selected.includes(opt);
            return (
              <button
                key={opt}
                type="button"
                onClick={() => toggle(opt)}
                className={
                  active
                    ? 'flex w-full items-center justify-between rounded px-2 py-1 text-left text-sm bg-primary/10'
                    : 'flex w-full items-center justify-between rounded px-2 py-1 text-left text-sm hover:bg-muted'
                }
              >
                <span>{opt}</span>
                {active ? <span className="text-xs">✓</span> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function PreviewView({
  preview,
}: {
  preview: {
    status: 'idle' | 'loading' | 'error';
    redis?: number;
    cp?: number | null;
    message?: string;
  };
}): ReactNode {
  if (preview.status === 'loading') {
    return (
      <span className="inline-flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> counting Redis + CP estimate…
      </span>
    );
  }
  if (preview.status === 'error') {
    return <p className="text-sm text-destructive">{preview.message}</p>;
  }
  if (preview.redis === undefined) {
    return (
      <p className="text-sm text-muted-foreground">
        Pick at least one filter to preview the count.
      </p>
    );
  }
  const drift =
    preview.cp !== null && preview.cp !== undefined
      ? preview.redis - preview.cp
      : null;
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <Tile label="Redis count" value={preview.redis.toLocaleString()} tone="default" />
      <Tile
        label="CP estimate"
        value={preview.cp != null ? preview.cp.toLocaleString() : '—'}
        tone={drift === 0 ? 'success' : drift != null ? 'warning' : 'default'}
      />
      <Tile
        label="Drift (Redis − CP)"
        value={drift != null ? (drift > 0 ? `+${drift}` : String(drift)) : '—'}
        tone={drift === 0 ? 'success' : drift != null ? 'warning' : 'default'}
      />
    </div>
  );
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'default' | 'success' | 'warning';
}): ReactNode {
  const toneClass =
    tone === 'success'
      ? 'text-green-700'
      : tone === 'warning'
      ? 'text-amber-700'
      : 'text-foreground';
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${toneClass}`}>{value}</p>
    </div>
  );
}

// ── Description generation ─────────────────────────────────────

/**
 * Compose a human-readable summary of the segment's filter selection. Stored
 * with the segment family in DDB so reconcile can copy it forward into every
 * v{N+1} rebuild — the operator stays able to see the original intent even
 * after the segment's own SegmentGroups gets rewritten into a frozen ID list.
 */
function buildDescription(
  states: string[],
  locations: string[],
  availableFilter: AvailableFilter,
  groups: string[],
  attempts: string[],
): string {
  const parts: string[] = [];
  if (states.length > 0) parts.push(`States: ${states.join(', ')}`);
  if (locations.length > 0 && locations.length <= 6) {
    parts.push(`Locations: ${locations.join(', ')}`);
  } else if (locations.length > 6) {
    parts.push(`Locations: ${locations.length} selected`);
  }
  if (availableFilter !== 'any') {
    parts.push(`Available: ${availableFilter === 'yes' ? 'true' : 'false'}`);
  }
  if (groups.length > 0) parts.push(`Groups: ${groups.join(' / ')}`);
  if (attempts.length > 0) parts.push(`Attempts: ${attempts.join(', ')}`);
  if (parts.length === 0) return '';
  // CP's Description field caps at 1000 chars; truncate defensively.
  return parts.join(' · ').slice(0, 950);
}

// ── Name validation ────────────────────────────────────────────

/**
 * AWS rejects segment names that don't match `^[a-zA-Z0-9_-]+$`. The form
 * doesn't enforce this at the input level (operators sometimes paste in
 * names with spaces or dots), so we sanitize on the way out: any disallowed
 * character collapses to `_`, leading/trailing underscores get trimmed.
 */
function sanitizeSegmentName(raw: string): string {
  if (!raw) return '';
  return raw
    .replace(/[^a-zA-Z0-9_-]+/g, '_') // any run of bad chars → single underscore
    .replace(/^_+|_+$/g, '') // strip leading/trailing
    .slice(0, 255); // CP caps name length defensively
}

// ── Name generation ─────────────────────────────────────────────

function buildAutoName(
  states: string[],
  groups: string[],
  attempts: string[],
): string {
  const now = new Date();
  const d = String(now.getDate());
  const m = String(now.getMonth() + 1);
  const yy = String(now.getFullYear()).slice(-2);
  const datePart = `${d}-${m}-${yy}`;
  const statesPart = codesForStates(states).join('_') || 'all';
  // Groups and attempts share the same compound "Category / Nth Attempt"
  // shape in Redis, so tokenize them together and dedup across both. Groups
  // is where the operator usually picks attempt labels (the "Attempts" box
  // only holds the raw `attempt` field — often empty).
  const attemptsPart = buildAttemptsPart([...groups, ...attempts]) || 'any';
  // HHMM suffix keeps names unique when the same filter is recreated within a
  // day (CP rejects duplicate names). The operator can still override.
  const hh = String(now.getHours()).padStart(2, '0');
  const mn = String(now.getMinutes()).padStart(2, '0');
  return `${datePart}-${statesPart}-${attemptsPart}-${hh}${mn}`;
}

/**
 * Build the attempts part of a segment/campaign name using the grouped format.
 * Groups tokens by their type abbreviation, then formats as:
 *   - Single attempt: `Can-3`
 *   - Multiple attempts: `Can_2-4-6`
 * Groups joined with `_`: `Can_2-4-6_NS_6-2-4`
 */
function buildAttemptsPart(attempts: string[]): string {
  const grouped = new Map<string, string[]>();
  for (const value of attempts) {
    const parts = value.split(/\s*\/\s*/).filter(Boolean);
    const number = (value.match(/\d+/) ?? [])[0] ?? '';
    let category = parts.find((p) => !/\d/.test(p));
    if (!category) {
      category = (parts[0] ?? value).replace(/\b\w*\d+\w*\b/g, '').trim();
    }
    const abbr = abbreviate(category);
    if (!abbr) continue;
    if (!grouped.has(abbr)) grouped.set(abbr, []);
    const nums = grouped.get(abbr)!;
    if (number && !nums.includes(number)) nums.push(number);
  }
  if (grouped.size === 0) return '';
  return [...grouped.entries()]
    .map(([abbr, nums]) => {
      if (nums.length === 0) return abbr;
      if (nums.length === 1) return `${abbr}-${nums[0]}`;
      return `${abbr}_${nums.join('-')}`;
    })
    .join('_');
}


function abbreviate(text: string): string {
  const words = text.split(/[^a-zA-Z]+/).filter(Boolean);
  if (words.length >= 2) {
    // "New Lead" → "NL", "No Show Left Voicemail" → "NSLV" (cap at 4).
    return words
      .map((w) => w[0]!.toUpperCase())
      .join('')
      .slice(0, 4);
  }
  if (words.length === 1) {
    // "Cancellation" → "Can", "Attempt" → "Att".
    const w = words[0]!;
    return w[0]!.toUpperCase() + w.slice(1, 3).toLowerCase();
  }
  return 'att';
}
