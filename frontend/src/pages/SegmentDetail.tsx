import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Badge, Spinner } from '@/components/ui';
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

  if (detail.isPending) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {(detail.error as Error).message}
      </div>
    );
  }

  const seg = detail.data;

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <p className="text-xs uppercase tracking-wide text-gray-400 mb-0.5">Segment</p>
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-xl font-semibold tracking-tight">
              {seg.displayName ?? seg.name}
            </h2>
            {seg.version ? (
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-gray-500">
                v{seg.version}
              </span>
            ) : null}
          </div>
          <p className="mt-1 font-mono text-xs text-gray-400">{seg.name}</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={() => refresh.mutate()}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Refresh count
          </button>
          <button
            type="button"
            onClick={() => setEnableOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            Enable Campaign
          </button>
          <button
            type="button"
            onClick={() => snapshot.mutate()}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Export snapshot
          </button>
          <button
            type="button"
            onClick={() => {
              if (confirm(`Delete segment "${seg.name}"?`)) remove.mutate();
            }}
            className="rounded-lg px-3 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
          >
            Delete
          </button>
        </div>
      </div>

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

      {/* Stat chips strip */}
      <div className="flex flex-wrap gap-2.5">
        <div className="flex items-center gap-2.5 rounded-xl border border-gray-200 bg-white px-3.5 py-2.5">
          {estimate === 'pending' ? (
            <span className="inline-flex items-center gap-2 text-xs text-gray-400">
              <Spinner /> computing…
            </span>
          ) : estimate ? (
            <>
              <span
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-xs font-bold font-mono"
                style={{ background: '#1d4ed822', color: '#1d4ed8' }}
              >
                {estimate.totalCount != null
                  ? estimate.totalCount >= 1000
                    ? `${Math.round(estimate.totalCount / 1000)}k`
                    : String(estimate.totalCount)
                  : '—'}
              </span>
              <div>
                <span className="text-xs font-medium text-gray-600">Member count</span>
                <div className="text-[10px] text-gray-400">as of {formatDateTime(estimate.at)}</div>
              </div>
            </>
          ) : (
            <>
              <span
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-xs font-bold font-mono"
                style={{ background: '#37415122', color: '#374151' }}
              >
                —
              </span>
              <span className="text-xs font-medium text-gray-600">Member count</span>
            </>
          )}
        </div>

        <div className="flex items-center gap-2.5 rounded-xl border border-gray-200 bg-white px-3.5 py-2.5">
          <span
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-xs font-bold font-mono"
            style={{ background: '#15803d22', color: '#15803d' }}
          >
            {seg.version ?? '—'}
          </span>
          <div>
            <span className="text-xs font-medium text-gray-600">Version</span>
            <div className="text-[10px] text-gray-400">{formatDateTime(seg.createdAt)}</div>
          </div>
        </div>

        <div className="flex items-center gap-2.5 rounded-xl border border-gray-200 bg-white px-3.5 py-2.5">
          <span
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-xs font-bold font-mono"
            style={{ background: snapshotStatus === 'COMPLETED' ? '#15803d22' : '#37415122', color: snapshotStatus === 'COMPLETED' ? '#15803d' : '#374151' }}
          >
            {snapshotStatus ? '↓' : '—'}
          </span>
          <div>
            <span className="text-xs font-medium text-gray-600">Snapshot</span>
            {snapshotStatus ? (
              <div className="text-[10px] text-gray-400">{snapshotStatus}</div>
            ) : (
              <div className="text-[10px] text-gray-400">not exported</div>
            )}
          </div>
        </div>
      </div>

      {/* Snapshot download links */}
      {snapshotDownloadUrls.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {snapshotDownloadUrls.map((url, i) => (
            <a
              key={i}
              href={url}
              download
              className="inline-flex items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-100 transition-colors"
            >
              ↓ Download CSV{snapshotDownloadUrls.length > 1 ? ` (part ${i + 1})` : ''}
            </a>
          ))}
        </div>
      ) : null}

      {/* Verification panel */}
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

      {/* Description */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-2">Description</h3>
        <p className="text-sm text-gray-500">
          {seg.description || 'No description.'}
        </p>
      </div>

      {/* Segment groups raw */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-900 mb-2">Segment groups (raw)</h3>
        <pre className="max-h-96 overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-3 text-xs text-gray-700">
          {JSON.stringify(seg.segmentGroups, null, 2)}
        </pre>
      </div>
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
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Verification</h3>
          <p className="mt-1 text-sm text-gray-500">
            Scans Redis with the segment's filters and compares counts against what
            Customer Profiles currently reports. Reconcile rebuilds a{' '}
            <code>v{'{N+1}'}</code> segment with the exact Redis-matching set and
            retargets active campaigns.
          </p>
        </div>
        <button
          type="button"
          onClick={onVerify}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Verify
        </button>
      </div>
    );
  }

  if (verification === 'pending') {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <p className="inline-flex items-center gap-2 text-sm text-gray-500">
          <Spinner /> Scanning Redis + asking CP for a fresh estimate…
        </p>
      </div>
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
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex flex-col gap-4">
      {/* Section header */}
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Verification</h3>
          <p className="text-xs text-gray-400">
            Last run {formatDateTime(verification.verifiedAt)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onDiagnose}
            disabled={diagnosePending}
            title="Sample Redis profiles and check CP membership + attributes — produces evidence of staleness for AWS support"
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            {diagnosePending ? (
              <span className="inline-flex items-center gap-2"><Spinner /> Diagnosing…</span>
            ) : 'Diagnose CP staleness'}
          </button>
          <button
            type="button"
            onClick={onVerify}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Re-verify
          </button>
        </div>
      </div>

      {/* Stat tiles */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Redis count" value={verification.redisCount.toLocaleString()} tone="default" />
        <StatTile
          label="CP segment count"
          value={verification.segmentCount.toLocaleString()}
          tone={inSync ? 'success' : 'danger'}
        />
        <StatTile
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
        <StatTile
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
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
          <span className="mt-0.5 shrink-0">⚠</span>
          <div>
            <strong className="font-semibold">Legacy filter: </strong>
            {legacyWarning}
          </div>
        </div>
      ) : null}

      <ExtrasPanel
        extras={extras}
        onDetectExtras={onDetectExtras}
        pending={extrasPending}
        disabledNote={extrasDisabledNote}
      />

      {extrasPlaceholder ? (
        <p className="inline-flex items-center gap-2 text-xs text-gray-400">
          <Spinner /> Snapshotting CP for the real missing/extras diff (~1–2 min)…
          The Missing tile shows the Redis total as a placeholder until then. Rebuild
          still works — it always rebuilds from Redis truth.
        </p>
      ) : extrasFailed ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
          <span className="mt-0.5 shrink-0">⚠</span>
          <div>
            <strong>Extras detection unavailable:</strong> the CP snapshot didn't finish
            (commonly the IAM boundary still missing the <code>Allow PassRole</code>{' '}
            statement). Missing tile shows the Redis total as a placeholder. Rebuild
            still works — it rebuilds from Redis truth without needing the snapshot.
          </div>
        </div>
      ) : null}

      {inSync ? (
        <p className="text-sm text-gray-500">
          Redis and Customer Profiles agree — no rebuild needed.
        </p>
      ) : (
        <>
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <p className="text-sm text-gray-500">
              Rebuild creates a new segment <code>v{verification.version + 1}</code> with
              exactly the {verification.redisCount.toLocaleString()} Redis-matching
              customerIds, retargets campaigns, and deletes the old segment.
            </p>
            <button
              type="button"
              onClick={onReconcile}
              disabled={reconcilePending}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              {reconcilePending ? (
                <span className="inline-flex items-center gap-2">
                  <Spinner /> rebuilding…
                </span>
              ) : (
                'Rebuild segment'
              )}
            </button>
          </div>

          {/* Diff table */}
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-100">
                    <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Customer ID</th>
                    <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Direction</th>
                    <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">Note</th>
                  </tr>
                </thead>
                <tbody>
                  {missingIds.length === 0 && extrasIds.length === 0 ? (
                    <tr>
                      <td colSpan={3} className="px-4 py-6 text-center text-sm text-gray-400">
                        No diff to display.
                      </td>
                    </tr>
                  ) : (
                    <>
                      {missingIds.slice(0, 20).map((cid) => (
                        <tr key={`miss-${cid}`} className="border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors">
                          <td className="px-4 py-3.5 font-mono text-xs text-gray-700">{cid}</td>
                          <td className="px-4 py-3.5">
                            <Badge tone="success">+ add</Badge>
                          </td>
                          <td className="px-4 py-3.5 text-xs text-gray-400">
                            (in Redis, not in CP)
                          </td>
                        </tr>
                      ))}
                      {extrasIds.slice(0, 20).map((cid) => (
                        <tr key={`extra-${cid}`} className="border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors">
                          <td className="px-4 py-3.5 font-mono text-xs text-gray-700">{cid}</td>
                          <td className="px-4 py-3.5">
                            <Badge tone="danger">− remove</Badge>
                          </td>
                          <td className="px-4 py-3.5 text-xs text-gray-400">
                            (in CP, not in Redis)
                          </td>
                        </tr>
                      ))}
                    </>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          {missing + extrasCount > Math.min(missingIds.length, 20) + Math.min(extrasIds.length, 20) ? (
            <p className="text-xs text-gray-400">
              Showing {Math.min(missingIds.length, 20)} missing + {Math.min(extrasIds.length, 20)}{' '}
              extras of {(missing + extrasCount).toLocaleString()} total.
            </p>
          ) : null}
        </>
      )}

      {diagnose ? <DiagnosePanel result={diagnose} /> : null}

      {lastReconcile ? (
        <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-600">
          Last rebuild {formatDateTime(lastReconcile.completedAt)}:{' '}
          <code>{lastReconcile.newSegmentName}</code> ·{' '}
          <span className="text-green-700">
            +{lastReconcile.added.toLocaleString()} added
          </span>
          {' · '}
          <span className="text-red-500">
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
    </div>
  );
}

function DiagnosePanel({ result }: { result: DiagnoseResult }): ReactNode {
  const hasStale = result.confirmedStaleCount > 0;
  const hasIngestionLag = (result.cpNoMatch?.length ?? 0) > 0;
  const tone = hasStale ? 'amber' : hasIngestionLag ? 'blue' : 'neutral';

  return (
    <div
      className={`rounded-xl border p-4 text-xs ${
        tone === 'amber'
          ? 'border-amber-200 bg-amber-50'
          : tone === 'blue'
          ? 'border-blue-200 bg-blue-50'
          : 'border-gray-200 bg-gray-50'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-sm">
            CP Staleness Diagnosis
            {hasStale ? (
              <span className="ml-2 text-amber-700">
                ⚠ {result.confirmedStaleCount} confirmed stale
              </span>
            ) : hasIngestionLag ? (
              <span className="ml-2 text-blue-700">
                ⚠ {result.cpNoMatch.length} ingestion lag
              </span>
            ) : (
              <span className="ml-2 text-green-700">✓ No staleness detected</span>
            )}
          </p>
          <p className="mt-0.5 text-gray-500">{result.message}</p>
          <p className="mt-0.5 text-gray-400">
            Sampled {result.sampledFromRedis} from Redis · {result.nonMembersInSample} not segment
            members · diagnosed {formatDateTime(result.diagnosedAt)}
          </p>
        </div>
        <button
          className="text-xs text-blue-600 underline hover:no-underline shrink-0"
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
        <div className="mt-3 overflow-hidden rounded-xl border border-amber-200">
          <p className="px-3 py-2 text-xs font-medium text-amber-800 bg-amber-100/60">
            Confirmed stale — CP has correct attributes but segment membership not updated
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-amber-100/40">
                  <th className="px-3 py-2 text-left font-medium text-amber-800">Customer ID</th>
                  <th className="px-3 py-2 text-left font-medium text-amber-800">CP last updated</th>
                  <th className="px-3 py-2 text-left font-medium text-amber-800">CP attributes</th>
                </tr>
              </thead>
              <tbody>
                {result.confirmedStale.slice(0, 15).map((entry) => (
                  <tr key={entry.customerId} className="border-t border-amber-200/40">
                    <td className="px-3 py-2 font-mono">{entry.customerId}</td>
                    <td className="px-3 py-2 text-gray-400">
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
          </div>
          {result.confirmedStale.length > 15 ? (
            <p className="px-3 py-2 text-gray-400">
              +{result.confirmedStale.length - 15} more — download JSON for full list
            </p>
          ) : null}
        </div>
      ) : null}

      {hasIngestionLag ? (
        <div className="mt-3 overflow-hidden rounded-xl border border-blue-200">
          <p className="px-3 py-2 text-xs font-medium text-blue-800 bg-blue-100/60">
            CP ingestion lag — Redis has updated attributes but CP has not ingested them yet
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-blue-100/40">
                  <th className="px-3 py-2 text-left font-medium text-blue-800">Customer ID</th>
                  <th className="px-3 py-2 text-left font-medium text-blue-800">CP last updated</th>
                  <th className="px-3 py-2 text-left font-medium text-blue-800">CP attributes (outdated)</th>
                </tr>
              </thead>
              <tbody>
                {result.cpNoMatch.map((entry) => (
                  <tr key={entry.customerId} className="border-t border-blue-200/40">
                    <td className="px-3 py-2 font-mono">{entry.customerId}</td>
                    <td className="px-3 py-2 text-gray-400">
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
          </div>
          <p className="px-3 py-2 text-gray-400">
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
      <div className="rounded-xl border border-dashed border-gray-200 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-900">Extras detection</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Snapshots Customer Profiles and diffs against Redis to find ids the
              segment still contains that no longer match the filter. Takes a
              minute or two.
            </p>
          </div>
          <button
            type="button"
            onClick={onDetectExtras}
            disabled={pending || snapshotBlocked}
            title={
              snapshotBlocked
                ? 'Waiting on IAM boundary amendment so CP can assume the snapshot role.'
                : undefined
            }
            className="shrink-0 rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            Detect extras
          </button>
        </div>
        {snapshotBlocked ? (
          <p className="mt-2 text-xs text-gray-400">{disabledNote}</p>
        ) : null}
      </div>
    );
  }

  if (extras === 'pending') {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
        <p className="inline-flex items-center gap-2 text-sm text-gray-500">
          <Spinner /> Snapshotting CP + diffing against Redis…
        </p>
      </div>
    );
  }

  if (extras.status === 'FAILED') {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4">
        <p className="font-semibold text-red-600 text-sm">Snapshot failed.</p>
        <p className="text-xs text-gray-500 mt-1">
          {extras.statusMessage ?? 'CP returned no status message.'}
        </p>
        <button
          type="button"
          onClick={onDetectExtras}
          disabled={pending}
          className="mt-2 rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
        >
          Retry
        </button>
      </div>
    );
  }

  const total = extras.totalExtras ?? 0;
  const shown = extras.extraCustomerIds?.length ?? 0;

  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">
            Extras detection —{' '}
            {total === 0 ? (
              <span className="text-green-700">no drift</span>
            ) : (
              <span className="text-red-500">
                {total.toLocaleString()} id{total === 1 ? '' : 's'} no longer match
              </span>
            )}
          </h3>
          {extras.computedAt ? (
            <p className="text-xs text-gray-400 mt-0.5">
              Computed {formatDateTime(extras.computedAt)} · CP {extras.cpCount} · Redis{' '}
              {extras.redisCount}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onDetectExtras}
          disabled={pending}
          className="shrink-0 rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
        >
          Re-detect
        </button>
      </div>

      {total > 0 && shown > 0 ? (
        <div className="mt-3 max-h-48 overflow-auto rounded-lg border border-gray-200 bg-white p-2">
          <ul className="divide-y divide-gray-100 text-xs">
            {extras.extraCustomerIds!.map((cid) => (
              <li key={cid} className="py-1 font-mono text-gray-700">
                {cid}
              </li>
            ))}
          </ul>
          {total > shown ? (
            <p className="mt-1 text-xs text-gray-400">
              Showing first {shown.toLocaleString()} of {total.toLocaleString()}.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function StatTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'default' | 'success' | 'danger';
}): ReactNode {
  const valueClass =
    tone === 'success'
      ? 'text-green-700'
      : tone === 'danger'
      ? 'text-red-500'
      : 'text-gray-900';
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-3">
      <p className="text-[10px] uppercase tracking-wider font-semibold text-gray-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${valueClass}`}>{value}</p>
    </div>
  );
}
