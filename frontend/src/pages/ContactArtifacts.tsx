import type { ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';

import { ContactArtifactsPanel } from '@/components/ContactArtifactsPanel';

export function ContactArtifacts(): ReactNode {
  const [params] = useSearchParams();
  const contactId = params.get('contactId') ?? '';

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Artefactos de Contacto</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Busca voicemails, grabaciones y transcripts de Amazon Connect por Contact ID.
        </p>
      </div>
      <div className="max-w-xl">
        <ContactArtifactsPanel initialContactId={contactId} />
      </div>
    </div>
  );
}