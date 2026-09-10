import { describe, it, expect } from 'vitest';
import {
  PRECALL_ALLOWED_PLACEHOLDERS,
  extractPlaceholders,
  renderedWorstCaseLength,
  validatePrecallSms,
  precallSmsAvailability,
} from './precallSms';

const CLINIC = 'VIP Medical Group';
const PAIN =
  "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a moment " +
  'to discuss your pain management request. Talk soon!';
const VEIN =
  "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick " +
  'call regarding your vein consultation request. Look out for our call!';
// The pre-2026-09-10 Vein draft, kept ONLY as the over-ceiling fixture below.
// It is 158 raw / 168 rendered — the exact string that forced OQ-9. Never ship it.
const VEIN_REJECTED_DRAFT =
  "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a quick " +
  'call regarding your vein consultation request. Look out for a call from ' +
  'this number!';

describe('placeholder allowlist', () => {
  it('mirrors the backend allowlist exactly', () => {
    // Drift here means the UI accepts copy the API rejects, or vice versa.
    // Backend source of truth: vip_shared.domain.services.sms_template
    // (RECIPIENT_FIELDS | CAMPAIGN_FIELDS).
    expect([...PRECALL_ALLOWED_PLACEHOLDERS].sort()).toEqual([
      'ClinicName',
      'FirstName',
    ]);
  });

  it('rejects a non-allowlisted placeholder by name', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, your {{Diagnosis}} is ready.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Diagnosis'))).toBe(true);
  });

  it('rejects {{Specialty}} — not allowlisted, the copy bakes it into the text', () => {
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, about your {{Specialty}} visit.',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('Specialty'))).toBe(true);
  });

  it('is case sensitive, like the backend regex', () => {
    expect(extractPlaceholders('Hi {{firstName}}!')).toEqual(
      new Set(['firstName']),
    );
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{firstName}}!',
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('firstName'))).toBe(true);
  });
});

describe('rendered length, not raw length', () => {
  it('measures the 20-char worst-case name, matching max_rendered_length', () => {
    expect(PAIN.length).toBe(125);
    expect(renderedWorstCaseLength(PAIN, CLINIC)).toBe(135);
  });

  it('accepts both approved templates — neither is over the ceiling', () => {
    expect(renderedWorstCaseLength(VEIN, CLINIC)).toBe(153);
    for (const tmpl of [VEIN, PAIN]) {
      const errs = validatePrecallSms({
        enabled: true,
        messageTemplate: tmpl,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      });
      expect(errs).toEqual([]);
    }
  });

  it('catches the template that is under 160 raw but over 160 rendered', () => {
    // This is why maxLength={160} on the raw textarea would be a bug. The
    // rejected Vein draft is the real example: 158 raw, 168 rendered.
    expect(VEIN_REJECTED_DRAFT.length).toBeLessThanOrEqual(160);
    expect(renderedWorstCaseLength(VEIN_REJECTED_DRAFT, CLINIC)).toBe(168);
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: VEIN_REJECTED_DRAFT,
      clinicName: CLINIC,
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('160'))).toBe(true);
  });
});

describe('required fields', () => {
  it('reports nothing when disabled, however empty', () => {
    expect(validatePrecallSms({ enabled: false })).toEqual([]);
  });

  it.each(['messageTemplate', 'originationNumberArn', 'clinicName'])(
    'requires %s when enabled',
    (field) => {
      const cfg = {
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      } as Record<string, unknown>;
      delete cfg[field];
      expect(validatePrecallSms(cfg).some((e) => e.includes(field))).toBe(true);
    },
  );

  it('accepts the approved Pain copy with every field set', () => {
    expect(
      validatePrecallSms({
        enabled: true,
        messageTemplate: PAIN,
        clinicName: CLINIC,
        originationNumberArn: 'arn:x',
      }),
    ).toEqual([]);
  });

  it('does not require clinicName when the template does not reference {{ClinicName}}', () => {
    // Mirrors the backend's conditional guard in _validate_precall_sms:
    // `"ClinicName" in extract_placeholders(tmpl) and not precall.get("clinicName")`.
    // A template that never uses {{ClinicName}} has nothing to interpolate,
    // so an operator config with no clinicName set must still be accepted —
    // the old unconditional check rejected this even though the server would
    // not have.
    const errs = validatePrecallSms({
      enabled: true,
      messageTemplate: 'Hi {{FirstName}}, quick reminder about your visit.',
      originationNumberArn: 'arn:x',
    });
    expect(errs.some((e) => e.includes('clinicName'))).toBe(false);
  });
});

describe('availability — the UI must not offer a config the API rejects', () => {
  it('is unavailable on an SMS-delivery campaign', () => {
    const a = precallSmsAvailability({ deliveryType: 'sms', dependsOn: [] });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/sms/i);
  });

  it('is unavailable when the campaign has dependsOn', () => {
    // Mirrors Task 5's server-side rejection: a campaign with dependsOn is
    // never pre-warmed (executor.py:2180, 2571-2573), so it has no segmentArn
    // at activation and the SMS would silently never fire.
    const a = precallSmsAvailability({
      deliveryType: 'campaign',
      dependsOn: ['other'],
    });
    expect(a.available).toBe(false);
    expect(a.reason).toMatch(/depend/i);
  });

  it('is available on a plain voice campaign with no dependencies', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'campaign', dependsOn: [] })
        .available,
    ).toBe(true);
  });

  it('is available on a journey campaign too', () => {
    expect(
      precallSmsAvailability({ deliveryType: 'journey', dependsOn: [] })
        .available,
    ).toBe(true);
  });
});
