import type { ReactNode } from 'react';

function Section({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <section>
      <h2 className="text-base font-semibold text-gray-900 mb-3">{title}</h2>
      <div className="text-sm text-gray-600 space-y-2">{children}</div>
    </section>
  );
}

function Pill({ color, children }: { color: string; children: ReactNode }): ReactNode {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${color}`}>
      {children}
    </span>
  );
}

export function PlansGuide(): ReactNode {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">How to use</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">Guide for creating and managing outbound campaign plans.</p>
      </div>

      <div className="max-w-3xl space-y-10">

        <Section title="What is a Plan?">
          <p>
            A <strong>Plan</strong> is an automated workflow that runs one or more outbound
            campaign groups (called <em>buckets</em>) in sequence. Each bucket contains one or more
            Connect campaigns that execute in DAG order — campaigns can run in parallel and depend on
            each other.
          </p>
          <p>
            When a plan runs, it creates all Connect campaigns, builds the CP segments, and manages
            their lifecycle automatically. Everything stops at <strong>7 PM EST</strong> daily.
          </p>
        </Section>

        <Section title="Buckets">
          <p>
            A bucket is one wave of campaigns. Buckets normally run one after another (sequentially).
            Each bucket has two modes:
          </p>
          <ul className="list-disc list-inside space-y-1.5 ml-2">
            <li>
              <strong>Time-based</strong> — runs for a fixed number of minutes, then stops regardless
              of whether campaigns finished. Enable <em>Pre-start next</em> to warm up the next
              bucket's Connect campaigns 5 minutes before the current one expires — so they start
              instantly when the next bucket activates.
            </li>
            <li>
              <strong>Status-based</strong> — runs until all campaigns reach a terminal state
              (completed, cancelled, error, expired). No time limit.
            </li>
          </ul>
          <p className="mt-1">Each bucket also has these settings:</p>
          <ul className="list-disc list-inside space-y-1 ml-2 text-xs">
            <li>
              <strong>Campaign Config</strong>: the Connect queue, contact flow, phone number, dialer
              type, and AMD settings shared by all campaigns in that bucket.
              <ul className="list-disc list-inside ml-5 mt-0.5 space-y-0.5">
                <li><strong>Progressive</strong> — agent-assisted dialing (agent must be available).</li>
                <li><strong>Agentless</strong> — fully automated, no agent required.</li>
                <li><strong>AMD</strong> (Answering Machine Detection) — detects voicemails and drops or leaves a message depending on your flow.</li>
              </ul>
            </li>
            <li>
              <strong>Cleanup</strong> — when checked (default), Connect campaigns and CP segments
              created for this bucket are automatically deleted after the bucket finishes. Uncheck
              only if you need to inspect them afterwards.
            </li>
            <li>
              <strong>Run in parallel with previous bucket</strong> — when checked, this bucket starts
              at the same time as the previous bucket instead of waiting for it to finish. Useful for
              running different state groups simultaneously.
            </li>
          </ul>
        </Section>

        <Section title="Campaigns inside a bucket">
          <p>
            Each campaign targets a specific combination of states, lead groups, and attempt numbers.
          </p>
          <ul className="list-disc list-inside space-y-1.5 ml-2">
            <li>
              <strong>Run type — Until 7 PM EST</strong>: campaign end time is hard-capped at 7 PM Eastern time.
            </li>
            <li>
              <strong>Run type — Custom (N min)</strong>: campaign runs for N minutes from when it starts.
            </li>
            <li>
              <strong>Depends on</strong>: list of other campaigns (same or earlier buckets) that must
              complete before this campaign starts. Leave empty to start immediately when its bucket
              activates. If any parent is cancelled or errors, this campaign is cascade-cancelled.
            </li>
          </ul>
          <p className="mt-1">
            Campaigns without dependencies run as <strong>stage 1</strong> (start immediately when
            the bucket activates). Campaigns with cross-bucket dependencies start as soon as all
            parents complete — even if the parent is in a previous bucket.
          </p>
        </Section>

        <Section title="Campaign statuses">
          <div className="flex flex-wrap gap-2 mb-3">
            <Pill color="bg-gray-100 text-gray-600 border-gray-200">waiting</Pill>
            <Pill color="bg-yellow-50 text-yellow-800 border-yellow-300">warming</Pill>
            <Pill color="bg-blue-50 text-blue-800 border-blue-300">running</Pill>
            <Pill color="bg-green-50 text-green-800 border-green-300">done</Pill>
            <Pill color="bg-gray-100 text-gray-400 border-gray-200">cancelled</Pill>
            <Pill color="bg-red-50 text-red-700 border-red-200">error</Pill>
            <Pill color="bg-orange-50 text-orange-700 border-orange-200">expired</Pill>
          </div>
          <ul className="list-disc list-inside space-y-1 ml-2">
            <li><strong>waiting</strong> — queued, waiting for dependencies or bucket to activate</li>
            <li><strong>warming</strong> — Connect campaign created but not yet started (pre-start window)</li>
            <li><strong>running</strong> — actively dialing</li>
            <li><strong>done</strong> — campaign reached end state normally</li>
            <li><strong>cancelled</strong> — skipped (empty segment, parent cancelled, bucket expired, or manually cancelled)</li>
            <li><strong>error</strong> — segment or campaign creation failed</li>
            <li><strong>expired</strong> — was running when the time-based bucket expired; stopped mid-run</li>
          </ul>
        </Section>

        <Section title="Triggers">
          <ul className="list-disc list-inside space-y-1.5 ml-2">
            <li>
              <strong>▶ Manual</strong> — only starts when you click "Run now". No automatic trigger.
            </li>
            <li>
              <strong>⏰ Time (COT)</strong> — starts automatically every day at the specified time
              in Colombia time (UTC-5, no daylight saving). Example: <code className="bg-gray-100 px-1 rounded">07:55</code> starts at 7:55 AM COT.
            </li>
            <li>
              <strong>⛓ On plan complete</strong> — starts automatically when another plan finishes
              its last bucket. Used to chain plans back-to-back.
              <ul className="list-disc list-inside ml-6 mt-1 space-y-1 text-xs">
                <li>
                  <strong>Repeat on</strong> — fires every time the upstream plan completes. The
                  trigger stays active permanently. Use this for daily chained workflows.
                </li>
                <li>
                  <strong>Repeat off</strong> — fires only once the next time the upstream plan
                  completes, then the trigger resets to Manual automatically. Use for one-off
                  follow-up runs.
                </li>
              </ul>
            </li>
          </ul>
          <p className="mt-1 text-xs text-gray-400">
            Templates cannot have a time or on-plan-complete trigger — they are only for cloning.
          </p>
        </Section>

        <Section title="Loop — repeat a plan continuously">
          <p>
            The <strong>Loop</strong> option (in the trigger section of the edit form) makes a plan
            restart automatically after each run completes, without needing another trigger. This is
            independent of the trigger type — a manual plan can still loop.
          </p>
          <ul className="list-disc list-inside space-y-1 ml-2">
            <li>
              Enable loop → the plan restarts immediately every time its last bucket finishes.
            </li>
            <li>
              <strong>Stop looping after (COT)</strong> — optional cutoff time. Once that time passes,
              the loop stops and the plan does not restart again. Leave blank to loop indefinitely
              until you edit the plan or it hits the 7 PM EST hard stop.
            </li>
          </ul>
          <p className="text-xs text-gray-400 mt-1">
            Typical use: a plan that keeps cycling through fresh leads all morning until the cutoff.
          </p>
        </Section>

        <Section title="How to create a plan">
          <ol className="list-decimal list-inside space-y-2 ml-2">
            <li>Click <strong>New plan</strong> in the sidebar.</li>
            <li>Enter a name and optional description.</li>
            <li>Choose a <strong>trigger</strong> (manual, time, or on plan complete).</li>
            <li>
              Add one or more <strong>buckets</strong>. For each bucket:
              <ul className="list-disc list-inside ml-6 mt-1 space-y-1 text-xs">
                <li>Set run mode (time-based or status-based) and duration if time-based.</li>
                <li>Configure Campaign Config: queue, flow, phone number, dialer type, AMD.</li>
                <li>Add campaigns — select states, group, attempts, run type, and dependencies.</li>
                <li>Optionally enable parallel (starts alongside previous bucket).</li>
              </ul>
            </li>
            <li>
              <strong>Overlap warning</strong> (amber banner) — appears if two campaigns target the
              same state AND the same attempt/group combination. This means the same customers would
              be dialed twice in the same run. This is usually a mistake but can be intentional
              (e.g., two different flows for the same group). You must check "I acknowledge" to save.
            </li>
            <li>Click <strong>Save</strong>.</li>
          </ol>
          <p className="mt-1 text-xs text-gray-400">
            To duplicate an existing plan: click the <strong>⋯</strong> menu on any plan card and
            choose Duplicate. A copy opens in edit mode with all buckets and campaigns pre-filled.
          </p>
        </Section>

        <Section title="How to use a Template">
          <p>
            Templates are pre-configured plans that cannot be run directly. They are starting points
            for creating new plans quickly without building from scratch.
          </p>
          <ol className="list-decimal list-inside space-y-1.5 ml-2">
            <li>Go to <strong>Templates</strong> in the sidebar.</li>
            <li>Find the template you want and click <strong>Use</strong>.</li>
            <li>Give the new plan a name — a copy is created and opened in edit mode.</li>
            <li>Adjust buckets, campaigns, trigger, and config as needed, then save.</li>
          </ol>
          <p className="mt-1 text-xs text-gray-400">
            To create a template: create a plan normally, then check "Mark as template" in the edit form.
            Templates appear under Templates, not under Today's plan.
          </p>
        </Section>

        <Section title="Live monitor controls">
          <p>
            Click on any plan card to open its <strong>Live monitor</strong>. While a run is active:
          </p>
          <ul className="list-disc list-inside space-y-1.5 ml-2">
            <li><strong>Abort</strong> — stops all campaigns and marks the run aborted. Chained plans do NOT fire. Loop does NOT restart.</li>
            <li><strong>Force Finish</strong> — stops all campaigns and marks the run completed. Chained plans WILL fire. Loop WILL restart.</li>
            <li><strong>▶ Start now</strong> (bucket header) — activates a queued or warming bucket immediately, bypassing timing and dependency checks.</li>
            <li><strong>■ Stop bucket</strong> (bucket header) — expires the current bucket now and advances to the next one.</li>
            <li><strong>▶</strong> (campaign card) — force-starts a single queued or cancelled campaign, bypassing its dependencies. Useful when a campaign got stuck or was incorrectly cancelled.</li>
            <li><strong>■</strong> (campaign card) — force-stops a single running campaign immediately. If it was the last running campaign in the bucket, the bucket advances automatically.</li>
          </ul>
          <p className="mt-1">
            <strong>When a run completes normally:</strong> all chained plans fire (any plan with trigger
            "after this plan"), and if Loop is enabled the plan restarts immediately.
          </p>
        </Section>

        <Section title="Run history">
          <p>
            In the Live monitor page, scroll down to find the <strong>Run history</strong> section
            (collapsed by default when a run is active). It lists every past run with status,
            triggered-by, start time, and duration. Click any row to view the full bucket and
            campaign state for that run — the monitor replays the run exactly as it was captured
            at trigger time (plan snapshot).
          </p>
        </Section>

        <Section title="Daily cutoff — 7 PM EST">
          <p>
            All running plans are automatically force-finished at <strong>7 PM Eastern time (EST/EDT, America/New_York)</strong>.
            Campaign end times are also capped at 7 PM EST. This applies to both the automatic
            tick and to any campaign configured as "Until 7 PM EST".
          </p>
          <p className="text-xs text-gray-400">
            Plan start times (triggers) use Colombia time (COT = UTC-5). The daily cutoff uses Eastern time.
          </p>
        </Section>

        <Section title="Automatic scheduler">
          <p>
            Plans with a <strong>Time trigger</strong> fire automatically via EventBridge. The
            schedule is created when you save the plan and updates when you edit the trigger time.
          </p>
          <p>
            The <strong>Scheduler</strong> page (legacy) shows the old per-plan schedule settings.
            For new plans, set the trigger directly on the plan using the "⏰ Time" trigger type —
            no need to use the Scheduler page.
          </p>
        </Section>

      </div>
    </div>
  );
}
