import type { ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Badge, Spinner } from '@/components/ui';
import { api } from '@/lib/api';

type LifecycleAction = 'start' | 'stop' | 'pause' | 'resume';

function stateTone(state: string): 'success' | 'warning' | 'muted' | 'danger' | 'default' {
  switch (state) {
    case 'RUNNING':
      return 'success';
    case 'PAUSED':
      return 'warning';
    case 'STOPPED':
    case 'INITIALIZED':
      return 'muted';
    case 'FAILED':
      return 'danger';
    default:
      return 'default';
  }
}

function statusBorderClass(state: string): string {
  switch (state) {
    case 'RUNNING':
      return 'border-l-4 border-l-blue-400';
    case 'PAUSED':
      return 'border-l-4 border-l-amber-400';
    case 'STOPPED':
    case 'INITIALIZED':
      return 'border-l-4 border-l-gray-200';
    case 'FAILED':
      return 'border-l-4 border-l-red-500';
    default:
      return 'border-l-4 border-l-gray-200';
  }
}

export function CampaignDetail(): ReactNode {
  const { id = '' } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const detail = useQuery({
    queryKey: ['campaign', id],
    queryFn: () => api.campaigns.get(id),
    enabled: Boolean(id),
  });

  const lifecycle = useMutation({
    mutationFn: (action: LifecycleAction) => api.campaigns[action](id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaign', id] }),
  });

  const remove = useMutation({
    mutationFn: () => api.campaigns.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['campaigns'] });
      navigate('/campaigns');
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
      <div className="rounded-xl border border-dashed border-red-200 p-8 text-center text-sm text-red-400">
        {(detail.error as Error).message}
      </div>
    );
  }

  const campaign = detail.data.campaign as Record<string, unknown>;
  const state = (detail.data.state ?? 'UNKNOWN').toUpperCase();
  const name = typeof campaign.name === 'string' ? campaign.name : id;

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground mb-0.5">Campaign</p>
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-xl font-semibold tracking-tight">{name}</h2>
            <Badge tone={stateTone(state)}>{state}</Badge>
          </div>
          <p className="mt-1 font-mono text-xs text-gray-400">{id}</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {state !== 'RUNNING' ? (
            <button
              type="button"
              onClick={() => lifecycle.mutate('start')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Start
            </button>
          ) : null}
          {state === 'RUNNING' ? (
            <button
              type="button"
              onClick={() => lifecycle.mutate('pause')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Pause
            </button>
          ) : null}
          {state === 'PAUSED' ? (
            <button
              type="button"
              onClick={() => lifecycle.mutate('resume')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Resume
            </button>
          ) : null}
          {state === 'RUNNING' || state === 'PAUSED' ? (
            <button
              type="button"
              onClick={() => lifecycle.mutate('stop')}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              Stop
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => {
              if (confirm(`Delete campaign "${name}"?`)) remove.mutate();
            }}
            className="rounded-lg px-2 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
          >
            Delete
          </button>
        </div>
      </div>

      {/* Configuration card with status-based left border */}
      <div
        className={[
          'bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm',
          statusBorderClass(state),
        ].join(' ')}
      >
        <div className="px-5 py-4">
          <h3 className="text-sm font-semibold text-gray-900 mb-3">Configuration</h3>
          <pre className="max-h-[480px] overflow-auto rounded-lg bg-gray-50 border border-gray-100 p-4 text-xs font-mono text-gray-700 leading-relaxed">
            {JSON.stringify(campaign, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  );
}
