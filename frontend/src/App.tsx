import type { ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { Layout } from '@/components/Layout';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { Audit } from '@/pages/Audit';
import { Callback } from '@/pages/Callback';
import { CampaignDetail } from '@/pages/CampaignDetail';
import { CampaignNew } from '@/pages/CampaignNew';
import { Campaigns } from '@/pages/Campaigns';
import { Dashboard } from '@/pages/Dashboard';
import { Login } from '@/pages/Login';
import { Profiles } from '@/pages/Profiles';
import { SegmentDetail } from '@/pages/SegmentDetail';
import { SegmentNew } from '@/pages/SegmentNew';
import { PlanDetail } from '@/pages/PlanDetail';
import { PlanNew } from '@/pages/PlanNew';
import { PlansHistory } from '@/pages/PlansHistory';
import { PlansLayout } from '@/pages/PlansLayout';
import { PlansMonitor } from '@/pages/PlansMonitor';
import { PlansScheduler } from '@/pages/PlansScheduler';
import { PlansTemplates } from '@/pages/PlansTemplates';
import { PlansToday } from '@/pages/PlansToday';
import { PlansGuide } from '@/pages/PlansGuide';
import { Segments } from '@/pages/Segments';

export default function App(): ReactNode {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/callback" element={<Callback />} />
      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/segments" element={<Segments />} />
        <Route path="/segments/new" element={<SegmentNew />} />
        <Route path="/segments/:id" element={<SegmentDetail />} />
        <Route path="/campaigns" element={<Campaigns />} />
        <Route path="/campaigns/new" element={<CampaignNew />} />
        <Route path="/campaigns/:id" element={<CampaignDetail />} />
        <Route path="/plans/new" element={<PlanNew />} />
        <Route path="/plans/:id/edit" element={<PlanNew />} />
        <Route path="/plans/:id" element={<PlanDetail />} />
        <Route path="/plans" element={<PlansLayout />}>
          <Route index element={<Navigate to="today" replace />} />
          <Route path="today" element={<PlansToday />} />
          <Route path="monitor" element={<PlansMonitor />} />
          <Route path="history" element={<PlansHistory />} />
          <Route path="templates" element={<PlansTemplates />} />
          <Route path="scheduler" element={<PlansScheduler />} />
          <Route path="guide" element={<PlansGuide />} />
        </Route>
        <Route path="/profiles" element={<Profiles />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
