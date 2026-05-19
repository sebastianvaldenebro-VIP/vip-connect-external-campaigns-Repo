import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Badge, Button, Card, Spinner } from '@/components/ui';
import { EnableCampaignModal } from '@/components/EnableCampaignModal';
import {
  api,
  type DiagnoseResult,
  type ExtrasDetectionResult,
  type ReconcileResult,
  type VerifyResult,
} from '@/lib/api';
import { stateCodesFromSegmentGroups, stateCodesFromSegmentName } from '@/lib/stateLocationMap';
import { formatDateTime } from '@/lib/utils';

type EstimateResult = { totalCount: number | null; at: string };
type ExtrasState = ExtrasDetectionResult | 'pending' | null;

export function SegmentDetail(): ReactNode {
  const { id = '' } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [estimate, setEstimate] = useState<EstimateResult | 'pending' | null>(null);
  const [snapshotStatus, setSnapshotStatus] = useState<string | null>(null);
  const [snapshotDownloadUrls, setSnapshotDownloadUrls] = useState<string[]>([]);
  const [verification, setVerification] = useState<VerifyResult | 'pending' | null>(null);
  const [lastReconcile, setLastReconcile] = useState<ReconcileResult | null>(null);
  const [diagnose, setDiagnose] = useState<DiagnoseResult | null>(null);
  const [extras, setExtras] = useState<ExtrasState>(null);
  const [enableOpen, setEnableOpen] = useState(false);

  const detail = useQuery({
    queryKey: ['segment', id],
    queryFn: () => api.segments.get(id),
    enabled: Boolean(id),
  });

  const refresh = useMutation({
    mutationFn: async () => {
      setEstimate('pending');
      const res = await api.segments.createEstimate(id);
      const deadline = Date.now() + 90_000;
      let delay = 1_500;
      while (Date.now() < deadline) {
        const poll = await api.segments.getEstimate(id, res.estimateId);
        if (poll.status === 'SUCCEEDED')
          return { totalCount: poll.estimate?.totalCount ?? null };
        if (poll.status === 'FAILED') throw new Error(poll.message ?? 'Estimate failed');
        await new Promise((r) => setTimeout(r, delay));
        delay = Math.min(delay * 1.3, 5_000);
      }
      throw new Error('Estimate timed out');
    },
    onSuccess: (res) =>
      setEstimate({ totalCount: res.totalCount, at: new Date().toISOString() }),
    onError: () => setEstimate(null),
  });

  const snapshot = useMutation({
    mutationFn: async () => {
      setSnapshotStatus('IN_PROGRESS');
      setSnapshotDownloadUrls([]);
      const res = await api.segments.createSnapshot(id, { dataFormat: 'CSV' });
      const deadline = Date.now() + 120_000;
      let delay = 2_000;
      while (Date.now() < deadline) {
        const poll = await api.segments.getSnapshot(id, res.snapshotId);
        setSnapshotStatus(poll.status);
        if (poll.status === 'COMPLETED' || poll.status === 'FAILED') return poll;
        await new Promise((r) => setTimeout(r, delay));
        delay = Math.min(delay * 1.3, 8_000);
      }
      throw new Error('Snapshot timed out');
    },
    onSuccess: (poll) => {
      if (poll.downloadUrls?.length) setSnapshotDownloadUrls(poll.downloadUrls);
    },
    onError: () => setSnapshotStatus('FAILED'),
  });

  const verify = useMutation({
    mutationFn: async () => {
      setVerification('pending');
      return api.segments.verify(id);
    },
    onSuccess: (res) => {
      setVerification(res);
      // Now that the boundary allows snapshots, kick off extras detection
      // automatically so the operator sees BOTH missing and extras in the
      // same view without an extra click. The async pattern still applies —
      // missing populates instantly, extras stream in 1–2 min later.
      detectExtras.mutate();
    },
    onError: () => setVerification(null),
  });

  const detectExtras = useMutation({
    mutationFn: async () => {
      setExtras('pending');
      const kickoff = await api.segments.startExtrasDetection(id);
      // Snapshots take minutes — poll up to 6 min at a 5s cadence.
      const deadline = Date.now() + 6 * 60 * 1_000;
      let delay = 5_000;
      while (Date.now() < deadline) {
        const poll = await api.segments.getExtrasDetection(id, kickoff.snapshotId);
        if (poll.status === 'COMPLETED') return poll;
        if (poll.status === 'FAILED')
          throw new Error(poll.statusMessage ?? 'Snapshot failed');
        await new Promise((r) => setTimeout(r, delay));
        delay = Math.min(delay * 1.2, 10_000);
      }
      throw new Error('Extras detection timed out');
    },
    onSuccess: (res) => setExtras(res),
    onError: () => setExtras(null),
  });

  const diagnoseMutation = useMutation({
    mutationFn: () => api.segments.diagnose(id),
    onSuccess: (res) => setDiagnose(res),
  });

  const reconcile = useMutation({
    mutationFn: async () => api.segments.reconcile(id),
    onSuccess: async (res) => {
      setLastReconcile(res);
      qc.invalidateQueries({ queryKey: ['segments'] });
      qc.invalidateQueries({ queryKey: ['segment', res.newSegmentName] });
      // Seed the estimate tile for the new version — the rebuilt segment is
      // exactly the Redis set so we don't need to hit CreateSegmentEstimate.
      setEstimate({ totalCount: res.targetCount, at: res.completedAt });
      // Clear local verify state so the new detail starts fresh.
      setVerification(null);
      navigate(`/segments/${encodeURIComponent(res.newSegmentName)}`, { replace: true });
    },
  });

  const remove = useMutation({
    mutationFn: () => api.segments.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['segments'] });
      navigate('/segments');
    },
  });

  if (detail.isPending) return <p className="text-muted-foreground">Loading…</p>;
  if (detail.isError)
    return <p className="text-destructive">{(detail.error as Error).message}</p>;

  const seg = detail.data;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-start justify-between">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Segment</p>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {seg.displayName ?? seg.name}
            </h1>
            {seg.version ? (
              <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide">
                v{seg.version}
              </span>
            ) : null}
          </div>
          <p className="mt-1 font-mono text-sm text-muted-foreground">{seg.name}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={() => refresh.mutate()}>
            Refresh count
          </Button>
          <Button onClick={() => setEnableOpen(true)}>Enable Campaign</Button>
          <Button variant="outline" onClick={() => snapshot.mutate()}>
            Export snapshot
          </Button>
          <Button
            variant="destructive"
            onClick={() => {
              if (confirm(`Delete segment "${seg.name}"?`)) remove.mutate();
            }}
          >
            Delete
          </Button>
        </div>
      </header>

      <EnableCampaignModal
        open={enableOpen}
        onClose={() => setEnableOpen(false)}
        segmentName={seg.name}
        segmentArn={seg.segmentArn}
        segmentStates={
          stateCodesFromSegmentGroups(seg.segmentGroups).length > 0
            ? stateCodesFromSegmentGroups(seg.segmentGroups)
            : stateCodesFromSegmentName(seg.name)
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Member count</p>
          {estimate === 'pending' ? (
            <p className="mt-2 inline-flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner /> computing…
            </p>
          ) : estimate ? (
            <>
              <p className="mt-2 text-3xl font-semibold">
                {estimate.totalCount?.toLocaleString() ?? '—'}
              </p>
              <p className="text-xs text-muted-foreground">
                as of {formatDateTime(estimate.at)}
              </p>
            </>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground">Click refresh to compute.</p>
          )}
        </Card>
        <Card>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Created</p>
          <p className="mt-2 text-sm">{formatDateTime(seg.createdAt)}</p>
        </Card>
        <Card>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Snapshot</p>
          <div className="mt-2 text-sm">
            {snapshotStatus ? <Badge>{snapshotStatus}</Badge> : <span className="text-muted-foreground">—</span>}
            {snapshotDownloadUrls.map((url, i) => (
              <a
                key={i}
                href={url}
                download
                className="mt-2 flex items-center gap-1 text-xs text-primary underline"
              >
                Download CSV {snapshotDownloadUrls.length > 1 ? `(part ${i + 1})` : ''}
              </a>
            ))}
          </div>
        </Card>
      </div>

      <VerificationPanel
        verification={verification}
        lastReconcile={lastReconcile}
        extras={extras}
        diagnose={diagnose}
        onVerify={() => verify.mutate()}
        onReconcile={() => reconcile.mutate()}
        onDetectExtras={() => detectExtras.mutate()}
        onDiagnose={() => diagnoseMutation.mutate()}
        reconcilePending={reconcile.isPending}
        extrasPending={detectExtras.isPending}
        diagnosePending={diagnoseMutation.isPending}
      />

      <Card>
        <h2 className="text-sm font-semibold">Description</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {seg.description || 'No description.'}
        </p>
      </Card>

      <Card>
        <h2 className="text-sm font-semibold">Segment groups (raw)</h2>
        <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">
          {JSON.stringify(seg.segmentGroups, null, 2)}
        </pre>
      </Card>
    </div>
  );
}

function VerificationPanel({
  verification,
  lastReconcile,
  extras,
  diagnose,
  onVerify,
  onReconcile,
  onDetectExtras,
  onDiagnose,
  reconcilePending,
  extrasPending,
  diagnosePending,
}: {
  verification: VerifyResult | 'pending' | null;
  lastReconcile: ReconcileResult | null;
  extras: ExtrasState;
  diagnose: DiagnoseResult | null;
  onVerify: () => void;
  onReconcile: () => void;
  onDetectExtras: () => void;
  onDiagnose: () => void;
  reconcilePending: boolean;
  extrasPending: boolean;
  diagnosePending: boolean;
}): ReactNode {
  if (!verification) {
    return (
      <Card className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">Verification</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Scans Redis with the segment's filters and compares counts against what
            Customer Profiles currently reports. Reconcile rebuilds a{' '}
            <code>v{'{N+1}'}</code> segment with the exact Redis-matching set and
            retargets active campaigns.
          </p>
        </div>
        <Button onClick={onVerify}>Verify</Button>
      </Card>
    );
  }

  if (verification === 'pending') {
    return (
      <Card>
        <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Scanning Redis + asking CP for a fresh estimate…
        </p>
      </Card>
    );
  }

  const legacyWarning = verification.notes?.legacyFilter;
  const extrasDisabledNote = verification.notes?.extrasDetectionDisabled;
  // Extras come from the async detection (started automatically on Verify
  // success). While the snapshot runs we render the tile as "computing"; once
  // it completes we know the full diff. The Verify response itself no longer
  // carries extras — that path was retired.
  const extrasResult =
    extras && extras !== 'pending' && extras.status === 'COMPLETED' ? extras : null;
  const extrasFailed =
    extras && extras !== 'pending' && extras.status === 'FAILED';
  const extrasCount = extrasResult?.totalExtras ?? 0;
  const extrasIds = extrasResult?.extraCustomerIds ?? [];
  const extrasComputing = extras === 'pending' || extrasPending;
  // Verify returns redis_ids as "missing" placeholder (it can't compute the
  // real diff without a snapshot). When extras detection completes we get
  // the *actual* Redis−CP missing count + ids, which we prefer over the
  // verify placeholder.
  const missing = extrasResult?.totalMissing ?? verification.missingCustomerIds.length;
  const missingIds =
    extrasResult?.missingCustomerIds ?? verification.missingCustomerIds;
  // Only declare "in sync" once we actually know the extras count.
  const inSync = extrasResult !== null && missing === 0 && extrasCount === 0;
  // Soft "we're still computing the precise diff" state — informational only;
  // it must not gate the Rebuild button. The rebuild flow rebuilds from Redis
  // truth and works correctly without the CP-side diff.
  const extrasPlaceholder =
    !extrasResult && !extrasFailed && extrasComputing;

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold">Verification</h2>
          <p className="text-xs text-muted-foreground">
            Last run {formatDateTime(verification.verifiedAt)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onDiagnose}
            disabled={diagnosePending}
            title="Sample Redis profiles and check CP membership + attributes — produces evidence of staleness for AWS support"
          >
            {diagnosePending ? (
              <span className="inline-flex items-center gap-2"><Spinner /> Diagnosing…</span>
            ) : 'Diagnose CP staleness'}
          </Button>
          <Button variant="outline" size="sm" onClick={onVerify}>
            Re-verify
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <Tile label="Redis count" value={verification.redisCount.toLocaleString()} tone="default" />
        <Tile
          label="CP segment count"
          value={verification.segmentCount.toLocaleString()}
          tone={inSync ? 'success' : 'danger'}
        />
        <Tile
          label="Missing (to add)"
          value={
            extrasResult
              ? missing.toLocaleString()
              : extrasComputing
              ? 'computing…'
              : missing.toLocaleString()
          }
          tone={extrasResult ? (missing > 0 ? 'danger' : 'success') : 'default'}
        />
        <Tile
          label="Extras (to remove)"
          value={
            extrasResult
              ? extrasCount.toLocaleString()
              : extrasComputing
              ? 'computing…'
              : '—'
          }
          tone={extrasResult ? (extrasCount > 0 ? 'danger' : 'success') : 'default'}
        />
      </div>

      {legacyWarning ? (
        <div className="rounded-md border border-amber-400/50 bg-amber-50 p-3 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <strong className="font-semibold">Legacy filter: </strong>
          {legacyWarning}
        </div>
      ) : null}

      <ExtrasPanel
        extras={extras}
        onDetectExtras={onDetectExtras}
        pending={extrasPending}
        disabledNote={extrasDisabledNote}
      />

      {extrasPlaceholder ? (
        <p className="inline-flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner /> Snapshotting CP for the real missing/extras diff (~1–2 min)…
          The Missing tile shows the Redis total as a placeholder until then. Rebuild
          still works — it always rebuilds from Redis truth.
        </p>
      ) : extrasFailed ? (
        <div className="rounded-md border border-amber-400/50 bg-amber-50 p-3 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <strong>Extras detection unavailable:</strong> the CP snapshot didn't finish
          (commonly the IAM boundary still missing the <code>Allow PassRole</code>{' '}
          statement). Missing tile shows the Redis total as a placeholder. Rebuild
          still works — it rebuilds from Redis truth without needing the snapshot.
        </div>
      ) : null}

      {inSync ? (
        <p className="text-sm text-muted-foreground">
          Redis and Customer Profiles agree — no rebuild needed.
        </p>
      ) : (
        <>
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              Rebuild creates a new segment <code>v{verification.version + 1}</code> with
              exactly the {verification.redisCount.toLocaleString()} Redis-matching
              customerIds, retargets campaigns, and deletes the old segment.
            </p>
            <Button onClick={onReconcile} disabled={reconcilePending}>
              {reconcilePending ? (
                <span className="inline-flex items-center gap-2">
                  <Spinner /> rebuilding…
                </span>
              ) : (
                'Rebuild segment'
              )}
            </Button>
          </div>

          <div className="overflow-hidden rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Customer ID</th>
                  <th className="px-3 py-2 text-left font-medium">Direction</th>
                  <th className="px-3 py-2 text-left font-medium">Note</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {missingIds.length === 0 && extrasIds.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="px-3 py-4 text-center text-muted-foreground">
                      No diff to display.
                    </td>
                  </tr>
                ) : (
                  <>
                    {missingIds.slice(0, 20).map((cid) => (
                      <tr key={`miss-${cid}`}>
                        <td className="px-3 py-2 font-mono text-xs">{cid}</td>
                        <td className="px-3 py-2">
                          <Badge tone="success">+ add</Badge>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          (in Redis, not in CP)
                        </td>
                      </tr>
                    ))}
                    {extrasIds.slice(0, 20).map((cid) => (
                      <tr key={`extra-${cid}`}>
                        <td className="px-3 py-2 font-mono text-xs">{cid}</td>
                        <td className="px-3 py-2">
                          <Badge tone="danger">− remove</Badge>
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          (in CP, not in Redis)
                        </td>
                      </tr>
                    ))}
                  </>
                )}
              </tbody>
            </table>
          </div>
          {missing + extrasCount > Math.min(missingIds.length, 20) + Math.min(extrasIds.length, 20) ? (
            <p className="text-xs text-muted-foreground">
              Showing {Math.min(missingIds.length, 20)} missing + {Math.min(extrasIds.length, 20)}{' '}
              extras of {(missing + extrasCount).toLocaleString()} total.
            </p>
          ) : null}
        </>
      )}

      {diagnose ? <DiagnosePanel result={diagnose} /> : null}

      {lastReconcile ? (
        <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
          Last rebuild {formatDateTime(lastReconcile.completedAt)}:{' '}
          <code>{lastReconcile.newSegmentName}</code> ·{' '}
          <span className="text-green-700">
            +{lastReconcile.added.toLocaleString()} added
          </span>
          {' · '}
          <span className="text-destructive">
            −{lastReconcile.removed.toLocaleString()} removed
          </span>
          {lastReconcile.campaignsUpdated.length > 0 ? (
            <>
              {' · '}
              {lastReconcile.campaignsUpdated.length} campaign(s) retargeted
            </>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function DiagnosePanel({ result }: { result: DiagnoseResult }): ReactNode {
  const hasStale = result.confirmedStaleCount > 0;
  const hasIngestionLag = (result.cpNoMatch?.length ?? 0) > 0;
  const tone = hasStale ? 'amber' : hasIngestionLag ? 'blue' : 'neutral';

  return (
    <div
      className={`rounded-md border p-3 text-xs ${
        tone === 'amber'
          ? 'border-amber-400/50 bg-amber-50 dark:bg-amber-950/30'
          : tone === 'blue'
          ? 'border-blue-400/50 bg-blue-50 dark:bg-blue-950/30'
          : 'border-border bg-muted/30'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-sm">
            CP Staleness Diagnosis
            {hasStale ? (
              <span className="ml-2 text-amber-700 dark:text-amber-300">
                ⚠ {result.confirmedStaleCount} confirmed stale
              </span>
            ) : hasIngestionLag ? (
              <span className="ml-2 text-blue-700 dark:text-blue-300">
                ⚠ {result.cpNoMatch.length} ingestion lag
              </span>
            ) : (
              <span className="ml-2 text-green-700 dark:text-green-400">✓ No staleness detected</span>
            )}
          </p>
          <p className="mt-0.5 text-muted-foreground">{result.message}</p>
          <p className="mt-0.5 text-muted-foreground">
            Sampled {result.sampledFromRedis} from Redis · {result.nonMembersInSample} not segment
            members · diagnosed {formatDateTime(result.diagnosedAt)}
          </p>
        </div>
        <button
          className="text-xs text-primary underline hover:no-underline"
          onClick={() => {
            const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `staleness-${result.segmentName}-${result.diagnosedAt}.json`;
            a.click();
            URL.revokeObjectURL(url);
          }}
        >
          Download JSON
        </button>
      </div>

      {result.confirmedStale.length > 0 ? (
        <div className="mt-3 overflow-hidden rounded-md border border-amber-300/50">
          <p className="px-3 py-2 text-xs font-medium text-amber-800 dark:text-amber-200 bg-amber-100/60 dark:bg-amber-900/30">
            Confirmed stale — CP has correct attributes but segment membership not updated
          </p>
          <table className="w-full text-xs">
            <thead className="bg-amber-100/40 dark:bg-amber-900/20">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Customer ID</th>
                <th className="px-3 py-2 text-left font-medium">CP last updated</th>
                <th className="px-3 py-2 text-left font-medium">CP attributes</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-amber-200/40">
              {result.confirmedStale.slice(0, 15).map((entry) => (
                <tr key={entry.customerId}>
                  <td className="px-3 py-2 font-mono">{entry.customerId}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {entry.cpLastUpdatedAt ? formatDateTime(entry.cpLastUpdatedAt) : '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[10px] leading-relaxed">
                    {Object.entries(entry.cpAttributes)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' · ')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.confirmedStale.length > 15 ? (
            <p className="px-3 py-2 text-muted-foreground">
              +{result.confirmedStale.length - 15} more — download JSON for full list
            </p>
          ) : null}
        </div>
      ) : null}

      {hasIngestionLag ? (
        <div className="mt-3 overflow-hidden rounded-md border border-blue-300/50">
          <p className="px-3 py-2 text-xs font-medium text-blue-800 dark:text-blue-200 bg-blue-100/60 dark:bg-blue-900/30">
            CP ingestion lag — Redis has updated attributes but CP has not ingested them yet
          </p>
          <table className="w-full text-xs">
            <thead className="bg-blue-100/40 dark:bg-blue-900/20">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Customer ID</th>
                <th className="px-3 py-2 text-left font-medium">CP last updated</th>
                <th className="px-3 py-2 text-left font-medium">CP attributes (outdated)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-blue-200/40">
              {result.cpNoMatch.map((entry) => (
                <tr key={entry.customerId}>
                  <td className="px-3 py-2 font-mono">{entry.customerId}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {entry.cpLastUpdatedAt ? formatDateTime(entry.cpLastUpdatedAt) : '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[10px] leading-relaxed">
                    {Object.entries(entry.cpAttributes)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' · ')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="px-3 py-2 text-muted-foreground">
            Showing up to 5 samples — download JSON for full context
          </p>
        </div>
      ) : null}
    </div>
  );
}

function ExtrasPanel({
  extras,
  onDetectExtras,
  pending,
  disabledNote,
}: {
  extras: ExtrasState;
  onDetectExtras: () => void;
  pending: boolean;
  disabledNote?: string;
}): ReactNode {
  // Feature-flagged off while the snapshot boundary fix lands. The backend
  // returns an `extrasDetectionDisabled` note until that policy change ships.
  const snapshotBlocked = Boolean(disabledNote);

  if (!extras) {
    return (
      <div className="rounded-md border border-dashed border-border p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">Extras detection</h3>
            <p className="text-xs text-muted-foreground">
              Snapshots Customer Profiles and diffs against Redis to find ids the
              segment still contains that no longer match the filter. Takes a
              minute or two.
            </p>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={onDetectExtras}
            disabled={pending || snapshotBlocked}
            title={
              snapshotBlocked
                ? 'Waiting on IAM boundary amendment so CP can assume the snapshot role.'
                : undefined
            }
          >
            Detect extras
          </Button>
        </div>
        {snapshotBlocked ? (
          <p className="mt-2 text-xs text-muted-foreground">{disabledNote}</p>
        ) : null}
      </div>
    );
  }

  if (extras === 'pending') {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3">
        <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Snapshotting CP + diffing against Redis…
        </p>
      </div>
    );
  }

  if (extras.status === 'FAILED') {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
        <p className="font-semibold text-destructive">Snapshot failed.</p>
        <p className="text-xs text-muted-foreground">
          {extras.statusMessage ?? 'CP returned no status message.'}
        </p>
        <Button
          size="sm"
          variant="outline"
          className="mt-2"
          onClick={onDetectExtras}
          disabled={pending}
        >
          Retry
        </Button>
      </div>
    );
  }

  const total = extras.totalExtras ?? 0;
  const shown = extras.extraCustomerIds?.length ?? 0;

  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">
            Extras detection —{' '}
            {total === 0 ? (
              <span className="text-green-700">no drift</span>
            ) : (
              <span className="text-destructive">
                {total.toLocaleString()} id{total === 1 ? '' : 's'} no longer match
              </span>
            )}
          </h3>
          {extras.computedAt ? (
            <p className="text-xs text-muted-foreground">
              Computed {formatDateTime(extras.computedAt)} · CP {extras.cpCount} · Redis{' '}
              {extras.redisCount}
            </p>
          ) : null}
        </div>
        <Button size="sm" variant="outline" onClick={onDetectExtras} disabled={pending}>
          Re-detect
        </Button>
      </div>

      {total > 0 && shown > 0 ? (
        <div className="mt-3 max-h-48 overflow-auto rounded-md border border-border bg-background p-2">
          <ul className="divide-y divide-border text-xs">
            {extras.extraCustomerIds!.map((cid) => (
              <li key={cid} className="py-1 font-mono">
                {cid}
              </li>
            ))}
          </ul>
          {total > shown ? (
            <p className="mt-1 text-xs text-muted-foreground">
              Showing first {shown.toLocaleString()} of {total.toLocaleString()}.
            </p>
          ) : null}
        </div>
      ) : null}
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
  tone: 'default' | 'success' | 'danger';
}): ReactNode {
  const toneClass =
    tone === 'success'
      ? 'text-green-700'
      : tone === 'danger'
      ? 'text-destructive'
      : 'text-foreground';
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${toneClass}`}>{value}</p>
    </div>
  );
}
