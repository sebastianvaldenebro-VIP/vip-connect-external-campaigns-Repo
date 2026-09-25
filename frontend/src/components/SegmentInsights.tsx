import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ReliabilityPanel } from './ReliabilityPanel';
import { useAuth } from '@/hooks/useAuth';

export function SegmentInsights({
  segmentName,
  campaign = false,
}: {
  segmentName?: string;
  campaign?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'reliability' | 'activity'>('reliability');
  const { user, groups, loading } = useAuth();
  if (loading || !user || !groups.includes('Admin')) return null;
  return (
    <details
      className="rounded-xl border border-border bg-card p-4"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm font-semibold">
        Reliability and activity
      </summary>
      {open && (
        <div className="mt-4 space-y-4">
          <div
            className="flex gap-4"
            role="group"
            aria-label="Segment insights"
          >
            <button
              type="button"
              aria-pressed={tab === 'reliability'}
              onClick={() => setTab('reliability')}
              className="text-sm text-primary"
            >
              Reliability
            </button>
            <button
              type="button"
              aria-pressed={tab === 'activity'}
              onClick={() => setTab('activity')}
              className="text-sm text-primary"
            >
              Activity
            </button>
          </div>
          <div>
            {tab === 'reliability' ? (
              <ReliabilityPanel segmentName={segmentName} campaign={campaign} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Verification and reconciliation actions use the existing audit
                log.{' '}
                <Link to="/audit" className="text-primary">
                  Open Audit
                </Link>
              </p>
            )}
          </div>
        </div>
      )}
    </details>
  );
}
