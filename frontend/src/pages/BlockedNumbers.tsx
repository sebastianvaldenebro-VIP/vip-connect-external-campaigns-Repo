import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge, Button, Card, Field, Input, Spinner } from '@/components/ui';
import { api, type BlockedNumber } from '@/lib/api';
import { formatDateTime } from '@/lib/utils';

/** Client-side mirror of the backend's normalize_phone — used only for
 * immediate form feedback. The backend re-normalizes and is the source of
 * truth for what actually gets written as the DynamoDB key. */
export function normalizePhoneInput(raw: string): string | null {
  const digits = raw.replace(/\D/g, '');
  const withCountry = digits.length === 10 ? `1${digits}` : digits;
  if (withCountry.length !== 11 || !withCountry.startsWith('1')) return null;
  return `+${withCountry}`;
}

export function BlockedNumbers(): ReactNode {
  const queryClient = useQueryClient();
  const [phoneInput, setPhoneInput] = useState('');
  const [reason, setReason] = useState('');
  const [feedback, setFeedback] = useState<{
    tone: 'success' | 'danger';
    text: string;
  } | null>(null);

  const list = useQuery({
    queryKey: ['deny-list'],
    queryFn: () => api.denyList.list(),
  });

  const add = useMutation({
    mutationFn: (body: { phoneNumber: string; reason?: string }) =>
      api.denyList.add(body),
    onSuccess: (result) => {
      setFeedback({
        tone: 'success',
        text: result.alreadyBlocked
          ? `${result.phoneNumber} was already blocked — updated.`
          : `${result.phoneNumber} is now blocked.`,
      });
      setPhoneInput('');
      setReason('');
      void queryClient.invalidateQueries({ queryKey: ['deny-list'] });
    },
    onError: (error: Error) => {
      setFeedback({ tone: 'danger', text: error.message });
    },
  });

  const normalized = normalizePhoneInput(phoneInput);
  const showFormatHint = phoneInput.trim().length > 0 && !normalized;

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setFeedback(null);
    if (!normalized) {
      setFeedback({
        tone: 'danger',
        text: 'Enter a valid 10-digit US phone number.',
      });
      return;
    }
    add.mutate({ phoneNumber: normalized, reason: reason.trim() || undefined });
  };

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">
          Blocked numbers
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Use this when you can&apos;t transfer a live call to the &quot;Block
          Number&quot; Quick Connect. The number is blocked on the next inbound
          call.
        </p>
      </div>

      <Card className="max-w-xl">
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field
            label="Phone number"
            hint={
              showFormatHint
                ? '10-digit US number, e.g. (914) 555-1234'
                : undefined
            }
            hintTone="danger"
          >
            <Input
              value={phoneInput}
              onChange={(e) => setPhoneInput(e.target.value)}
              placeholder="(914) 555-1234"
              inputMode="tel"
              autoComplete="off"
            />
          </Field>
          <Field label="Reason (optional)">
            <Input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. harassment, repeated spam"
              maxLength={500}
            />
          </Field>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={add.isPending || !normalized}>
              {add.isPending ? <Spinner /> : 'Block number'}
            </Button>
            {feedback ? (
              <span
                className={
                  feedback.tone === 'success'
                    ? 'text-sm text-green-700'
                    : 'text-sm text-destructive'
                }
              >
                {feedback.text}
              </span>
            ) : null}
          </div>
        </form>
      </Card>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-100">
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                  Phone number
                </th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                  Added
                </th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                  Added by
                </th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                  Reason
                </th>
                <th className="px-4 py-2.5 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wider whitespace-nowrap">
                  Source
                </th>
              </tr>
            </thead>
            <tbody>
              {list.isPending ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center">
                    <div className="flex items-center justify-center py-12">
                      <Spinner />
                    </div>
                  </td>
                </tr>
              ) : list.isError ? (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-8 text-center text-destructive text-sm"
                  >
                    {(list.error as Error).message}
                  </td>
                </tr>
              ) : list.data.blockedNumbers.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8">
                    <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-gray-400">
                      No numbers blocked yet.
                    </div>
                  </td>
                </tr>
              ) : (
                list.data.blockedNumbers.map((row: BlockedNumber) => (
                  <tr
                    key={row.phoneNumber}
                    className="border-b border-gray-100 last:border-0 hover:bg-gray-50/50 transition-colors"
                  >
                    <td className="px-4 py-3.5 align-top font-mono text-sm text-gray-700 whitespace-nowrap">
                      {row.phoneNumber}
                    </td>
                    <td className="px-4 py-3.5 align-top font-mono text-xs text-gray-400 whitespace-nowrap">
                      {formatDateTime(row.addedAt)}
                    </td>
                    <td className="px-4 py-3.5 align-top text-sm text-gray-700">
                      {row.addedBy ?? '—'}
                    </td>
                    <td className="px-4 py-3.5 align-top text-sm text-gray-600">
                      {row.reason ?? '—'}
                    </td>
                    <td className="px-4 py-3.5 align-top">
                      <Badge
                        tone={row.source === 'manual-ui' ? 'default' : 'muted'}
                      >
                        {row.source === 'manual-ui'
                          ? 'Portal'
                          : row.source === 'quick-connect'
                            ? 'Quick Connect'
                            : 'Legacy'}
                      </Badge>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
