import type { ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { Badge, Button, Card } from '@/components/ui';
import { api } from '@/lib/api';

type LifecycleAction = 'start' | 'stop' | 'pause' | 'resume';

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

  if (detail.isPending) return <p className="text-muted-foreground">Loading…</p>;
  if (detail.isError)
    return <p className="text-destructive">{(detail.error as Error).message}</p>;

  const campaign = detail.data.campaign as Record<string, unknown>;
  const state = (detail.data.state ?? 'UNKNOWN').toUpperCase();
  const name = typeof campaign.name === 'string' ? campaign.name : id;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-start justify-between">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Campaign</p>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">{name}</h1>
            <Badge tone={state === 'RUNNING' ? 'success' : state === 'PAUSED' ? 'warning' : 'muted'}>
              {state}
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{id}</p>
        </div>
        <div className="flex items-center gap-2">
          {state !== 'RUNNING' ? (
            <Button variant="outline" onClick={() => lifecycle.mutate('start')}>
              Start
            </Button>
          ) : null}
          {state === 'RUNNING' ? (
            <Button variant="outline" onClick={() => lifecycle.mutate('pause')}>
              Pause
            </Button>
          ) : null}
          {state === 'PAUSED' ? (
            <Button variant="outline" onClick={() => lifecycle.mutate('resume')}>
              Resume
            </Button>
          ) : null}
          {state === 'RUNNING' || state === 'PAUSED' ? (
            <Button variant="outline" onClick={() => lifecycle.mutate('stop')}>
              Stop
            </Button>
          ) : null}
          <Button
            variant="destructive"
            onClick={() => {
              if (confirm(`Delete campaign "${name}"?`)) remove.mutate();
            }}
          >
            Delete
          </Button>
        </div>
      </header>

      <Card>
        <h2 className="text-sm font-semibold">Configuration</h2>
        <pre className="mt-2 max-h-[480px] overflow-auto rounded-md bg-muted p-3 text-xs">
          {JSON.stringify(campaign, null, 2)}
        </pre>
      </Card>
    </div>
  );
}
