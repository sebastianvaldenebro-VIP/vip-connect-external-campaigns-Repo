import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Card, Spinner } from '@/components/ui';
import { api } from '@/lib/api';
import { PlanCard } from '@/pages/Plans';

export function PlansTemplates(): ReactNode {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const list = useQuery({ queryKey: ['plans'], queryFn: () => api.plans.listV2() });

  const remove = useMutation({
    mutationFn: (id: string) => api.plans.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plans'] }),
  });

  const [duplicatingId, setDuplicatingId] = useState<string | null>(null);
  const duplicate = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.plans.duplicate(id, name),
    onSuccess: (plan) => {
      setDuplicatingId(null);
      qc.invalidateQueries({ queryKey: ['plans'] });
      navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`);
    },
    onError: () => setDuplicatingId(null),
  });

  const templates = (list.data?.plans ?? []).filter((p) => p.isTemplate || p.is_template);

  const handleDuplicate = (plan: { planId: string; name: string }) => {
    const name = prompt(`New plan name (copying "${plan.name}"):`, `${plan.name} (copy)`);
    if (!name?.trim()) return;
    setDuplicatingId(plan.planId);
    duplicate.mutate({ id: plan.planId, name: name.trim() });
  };

  if (list.isPending) {
    return <p className="inline-flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Loading…</p>;
  }
  if (list.isError) {
    return <p className="text-sm text-destructive">{(list.error as Error).message}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">Templates</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">Reusable configurations — duplicate to create a runnable plan.</p>
      </div>

      {templates.length === 0 ? (
        <Card className="p-8 text-center text-sm text-muted-foreground">
          No templates yet. Create a plan and mark it as template.
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {templates.map((plan) => (
            <PlanCard
              key={plan.planId}
              plan={plan}
              isTriggering={false}
              isDuplicating={duplicatingId === plan.planId}
              isTemplate
              onRun={() => {}}
              onEdit={() => navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`)}
              onDuplicate={() => handleDuplicate(plan)}
              onDelete={() => { if (confirm(`Delete template "${plan.name}"?`)) remove.mutate(plan.planId); }}
              onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
