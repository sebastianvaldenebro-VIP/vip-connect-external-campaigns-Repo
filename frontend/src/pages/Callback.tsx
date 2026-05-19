import { useEffect, type ReactNode } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';

import { useAuth } from '@/hooks/useAuth';

export function Callback(): ReactNode {
  const { user, loading } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!loading && user) {
      navigate('/dashboard', { replace: true });
    }
  }, [loading, user, navigate]);

  if (!loading && !user) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="flex min-h-screen items-center justify-center text-muted-foreground">
      Completing sign-in…
    </div>
  );
}
