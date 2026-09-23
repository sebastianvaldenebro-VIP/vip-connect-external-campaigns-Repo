import { useState } from 'react';
import { usePreferences } from '@/components/UserPreferencesProvider';
export function Preferences() {
  const { preferences, save } = usePreferences();
  const [freshness, setFreshness] = useState(
    String(preferences.freshnessMinutes),
  );
  const [message, setMessage] = useState('');
  return (
    <section className="max-w-2xl rounded-xl border border-border bg-card p-6 shadow-sm">
      <h2 className="text-xl font-semibold">Preferences</h2>
      <p className="mt-2 text-sm text-muted-foreground">
        Saved for your account in this browser. These display settings do not
        change campaign thresholds, schedules, or dialing.
      </p>
      <form
        className="mt-6 flex max-w-sm flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          const minutes = Number(freshness);
          if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
            setMessage('Enter a whole number from 1 to 1440 minutes.');
            return;
          }
          setMessage(
            save({ freshnessMinutes: minutes })
              ? 'Preferences saved in this browser.'
              : 'Preferences could not be saved. Browser storage may be unavailable.',
          );
        }}
      >
        <label className="text-sm">
          Mark observations stale after (minutes)
          <input
            className="mt-2 block w-full rounded border border-border bg-background p-2"
            type="number"
            min="1"
            max="1440"
            step="1"
            value={freshness}
            onChange={(event) => setFreshness(event.target.value)}
            required
          />
        </label>
        <button
          className="rounded bg-primary px-4 py-2 text-sm text-primary-foreground"
          type="submit"
        >
          Save preferences
        </button>
        {message && (
          <p role="status" className="text-sm">
            {message}
          </p>
        )}
      </form>
    </section>
  );
}
