import { describe, expect, it } from 'vitest';

import { actionTone } from './Audit';

describe('actionTone', () => {
  it('tones existing manual actions unchanged', () => {
    expect(actionTone('create')).toBe('success');
    expect(actionTone('delete')).toBe('danger');
    expect(actionTone('pause')).toBe('warning');
  });

  it('tones bucket_started and bucket_completed as success', () => {
    expect(actionTone('bucket_started')).toBe('success');
    expect(actionTone('bucket_completed')).toBe('success');
  });

  it('tones window_closed as warning', () => {
    expect(actionTone('window_closed')).toBe('warning');
  });

  it('tones reconcile_retry as warning', () => {
    expect(actionTone('reconcile_retry')).toBe('warning');
  });

  it('tones creation_failed as danger', () => {
    expect(actionTone('creation_failed')).toBe('danger');
  });

  it('falls back to default for an unrecognized action', () => {
    expect(actionTone('something_new')).toBe('default');
  });
});
