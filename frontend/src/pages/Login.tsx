import { useEffect, useState, type ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

import { useAuth } from '@/hooks/useAuth';
import { signIn } from '@/lib/auth';

export function Login(): ReactNode {
  const { user, loading } = useAuth();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (user || loading) return;
    signIn().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : 'Sign-in failed');
    });
  }, [user, loading]);

  if (user) return <Navigate to="/dashboard" replace />;

  return (
    <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
      <div className="w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-sm">
        <h1 className="text-lg font-semibold">VIP Connect Admin</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Redirecting to the corporate identity provider…
        </p>
        {error ? (
          <p className="mt-4 text-sm text-destructive">{error}</p>
        ) : null}
      </div>
    </div>
  );
}
