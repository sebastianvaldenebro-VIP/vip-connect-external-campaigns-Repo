import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { Spinner } from '@/components/ui';
import { api, type PlanSummaryV2 } from '@/lib/api';

// ── Template card ─────────────────────────────────────────────────────────────

function TemplateCard({
  plan,
  isDuplicating,
  onEdit,
  onDuplicate,
  onDelete,
  onClick,
}: {
  plan: PlanSummaryV2;
  isDuplicating: boolean;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onClick: () => void;
}) {
  const totalCampaigns = plan.buckets.reduce((s, b) => s + (b.campaigns?.length ?? 0), 0);

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
      <div
        className="grid items-center gap-3 px-4 py-3.5"
        style={{ gridTemplateColumns: '1fr auto' }}
      >
        {/* Info */}
        <button
          type="button"
          className="min-w-0 text-left"
          onClick={onClick}
        >
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-gray-900">{plan.name}</span>
            <span className="inline-flex items-center rounded border border-purple-200 bg-purple-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-purple-600">
              Template
            </span>
          </div>
          <div className="flex items-center gap-2 mt-1 text-xs text-gray-400 flex-wrap">
            <span>
              {plan.buckets.length} bucket{plan.buckets.length !== 1 ? 's' : ''} · {totalCampaigns}{' '}
              campaign{totalCampaigns !== 1 ? 's' : ''}
            </span>
            {plan.description && (
              <>
                <span>·</span>
                <span className="truncate max-w-xs">{plan.description}</span>
              </>
            )}
          </div>
        </button>

        {/* Actions */}
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={onEdit}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 transition-colors"
          >
            Edit
          </button>
          <button
            type="button"
            onClick={onDuplicate}
            disabled={isDuplicating}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 text-white px-3.5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {isDuplicating ? <Spinner /> : 'Use template'}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="rounded-lg px-2 py-1.5 text-xs text-red-400 hover:text-red-600 hover:bg-red-50 transition-colors"
            title="Delete template"
          >
            ✕
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

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

  const handleDuplicate = (plan: PlanSummaryV2) => {
    const name = prompt(`New plan name (copying "${plan.name}"):`, `${plan.name} (copy)`);
    if (!name?.trim()) return;
    setDuplicatingId(plan.planId);
    duplicate.mutate({ id: plan.planId, name: name.trim() });
  };

  if (list.isPending) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner />
      </div>
    );
  }
  if (list.isError) {
    return <p className="text-sm text-destructive">{(list.error as Error).message}</p>;
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Templates</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Reusable configurations — duplicate to create a runnable plan.
        </p>
      </div>

      {templates.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
          No templates yet. Create a plan and mark it as template.
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {templates.map((plan) => (
            <TemplateCard
              key={plan.planId}
              plan={plan}
              isDuplicating={duplicatingId === plan.planId}
              onEdit={() => navigate(`/plans/${encodeURIComponent(plan.planId)}/edit`)}
              onDuplicate={() => handleDuplicate(plan)}
              onDelete={() => {
                if (confirm(`Delete template "${plan.name}"?`)) remove.mutate(plan.planId);
              }}
              onClick={() => navigate(`/plans/${encodeURIComponent(plan.planId)}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
