import type { CampaignState } from '@/lib/api';
import type { StatusTone } from '@/components/ui/status';

type Reconcile = CampaignState['reconcile'];

export function reconcileTone(reconcile: Reconcile): StatusTone {
  if (!reconcile) return 'neutral';
  if (reconcile.retries > 0) return 'warning';
  return reconcile.actual === reconcile.expected ? 'success' : 'warning';
}

export function formatReconcile(reconcile: Reconcile): string {
  if (!reconcile) return '';
  const retrySuffix =
    reconcile.retries === 0 ? 'clean' : `${reconcile.retries} ${reconcile.retries === 1 ? 'retry' : 'retries'}`;
  return `reconcile: ${reconcile.expected} → ${reconcile.actual} · ${retrySuffix}`;
}
