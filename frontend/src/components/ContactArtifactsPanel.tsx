import type { ReactNode } from 'react';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api, ApiRequestError } from '@/lib/api';
import { Button } from '@/components/ui';
import { Input } from '@/components/ui';
import { cn } from '@/lib/utils';

type Props = {
  /** Pre-fill the contactId field and trigger the lookup immediately. */
  initialContactId?: string;
};

export function ContactArtifactsPanel({ initialContactId = '' }: Props): ReactNode {
  const [inputValue, setInputValue] = useState(initialContactId);
  const [contactId, setContactId] = useState(initialContactId);

  const { data, isFetching, error, dataUpdatedAt, refetch } = useQuery({
    queryKey: ['contact-artifacts', contactId],
    queryFn: () => api.contacts.getArtifacts(contactId),
    enabled: Boolean(contactId),
    retry: false,
    // Mark stale at 10 min so a re-submit after the 15 min presign TTL refetches.
    // Note: refetchOnWindowFocus is globally disabled; re-submit always calls refetch().
    staleTime: 10 * 60 * 1000,
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = inputValue.trim();
    if (!trimmed) return;
    if (trimmed === contactId) {
      // Same ID: force a fresh fetch so expired URLs are replaced.
      void refetch();
    } else {
      setContactId(trimmed);
    }
  }

  const notFound =
    error instanceof ApiRequestError && error.status === 404;

  const fetchedLabel = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString()
    : null;

  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={handleSubmit} className="flex gap-2">
        <Input
          placeholder="Contact ID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          className="font-mono text-xs"
        />
        <Button type="submit" disabled={!inputValue.trim() || isFetching}>
          {isFetching ? 'Buscando…' : 'Buscar'}
        </Button>
      </form>

      {isFetching && (
        <p className="text-sm text-muted-foreground">Buscando artefactos…</p>
      )}

      {!isFetching && notFound && (
        <p className="text-sm text-destructive">No se encontró este contacto.</p>
      )}

      {!isFetching && error && !notFound && (
        <p className="text-sm text-destructive">
          Error al obtener artefactos:{' '}
          {error instanceof Error ? error.message : 'Error desconocido'}
        </p>
      )}

      {!isFetching && data && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-2">
            <ArtifactRow
              label="Voicemail"
              icon="🎙"
              url={data.voicemail}
              filename="voicemail.wav"
            />
            <ArtifactRow
              label="Grabación"
              icon="📞"
              url={data.recording}
              filename="recording.wav"
            />
            <ArtifactRow
              label="Transcript"
              icon="💬"
              url={data.transcript}
              filename="transcript.json"
            />
          </div>
          {fetchedLabel && (
            <p className="text-xs text-muted-foreground">
              Obtenido a las {fetchedLabel} · Las URLs vencen en{' '}
              {Math.round(data.expiresInSeconds / 60)} min
            </p>
          )}
        </div>
      )}
    </div>
  );
}

type ArtifactRowProps = {
  label: string;
  icon: string;
  url: string | null;
  filename: string;
};

function ArtifactRow({ label, icon, url, filename }: ArtifactRowProps): ReactNode {
  return (
    <div
      className={cn(
        'flex items-center justify-between rounded-md border border-border px-4 py-3',
        url ? 'bg-card' : 'bg-muted/30 opacity-50',
      )}
    >
      <span className="flex items-center gap-2 text-sm font-medium">
        <span>{icon}</span>
        {label}
      </span>
      {url ? (
        // target="_blank" + ResponseContentDisposition (set server-side) are both needed
        // because the `download` attribute is silently ignored on cross-origin URLs.
        <a
          href={url}
          download={filename}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-primary underline-offset-2 hover:underline"
        >
          Descargar
        </a>
      ) : (
        <span className="text-xs text-muted-foreground">No disponible</span>
      )}
    </div>
  );
}