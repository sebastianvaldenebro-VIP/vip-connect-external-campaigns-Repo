/**
 * Client-side mirror of the pre-call SMS rules enforced by
 * services/api-plans/src/handlers/plans.py (_validate_precall_sms).
 *
 * SCOPE, deliberately narrow: this duplicates only the cheap, stable rules —
 * required fields, the placeholder allowlist, the rendered-length ceiling, and
 * the dependsOn/deliveryType conflicts. It does NOT reimplement the ten PHI
 * regexes in _PHI_PATTERNS. Duplicating those in TypeScript would create two
 * sources of truth for a compliance rule and they would drift. The server stays
 * authoritative; the UI's job is to catch the common mistakes early and to
 * surface whatever the server says verbatim when it says no.
 */

export const PRECALL_ALLOWED_PLACEHOLDERS = new Set(['FirstName', 'ClinicName']);

/** Matches vip_shared…sms_template._PLACEHOLDER_RE. Case sensitive on purpose. */
const PLACEHOLDER_RE = /\{\{\s*(\w+)\s*\}\}/g;

/** Matches _NAME_MAX_LEN in the Python renderer, which truncates there. */
const NAME_BUDGET = 20;

/** Matches _MAX_SMS_CHARS in handlers/plans.py. One GSM-7 segment. */
export const MAX_SMS_CHARS = 160;

export function extractPlaceholders(template: string): Set<string> {
  return new Set(
    [...(template ?? '').matchAll(PLACEHOLDER_RE)].map((m) => m[1]),
  );
}

/** Worst-case rendered length: the longest name the server would ever render. */
export function renderedWorstCaseLength(
  template: string,
  clinicName: string,
): number {
  return (template ?? '')
    .replace(/\{\{\s*FirstName\s*\}\}/g, 'A'.repeat(NAME_BUDGET))
    .replace(/\{\{\s*ClinicName\s*\}\}/g, clinicName ?? '').length;
}

/**
 * Mirrors the backend's ALLOWLIST exactly (_VOICE_DELIVERY_TYPES in
 * handlers/plans.py) rather than a denylist of just 'sms'. A denylist of one
 * value happens to produce the same result as this allowlist for today's 4
 * known deliveryType values, but a future non-voice type would silently pass
 * a denylist and only get caught server-side, after the operator has already
 * filled out the whole precall panel. Keep in sync with _VOICE_DELIVERY_TYPES.
 */
const VOICE_DELIVERY_TYPES = new Set(['campaign', 'branded', 'journey']);

export function precallSmsAvailability(campaign: {
  deliveryType?: string;
  dependsOn?: string[];
}): { available: boolean; reason?: string } {
  const deliveryType = campaign.deliveryType ?? 'campaign';
  if (!VOICE_DELIVERY_TYPES.has(deliveryType)) {
    return {
      available: false,
      reason:
        `Pre-call SMS applies to voice campaigns — a '${deliveryType}' campaign has no dial to precede.`,
    };
  }
  if ((campaign.dependsOn ?? []).length > 0) {
    return {
      available: false,
      reason:
        'Pre-call SMS is unavailable while this campaign waits on another: a dependent campaign is not pre-warmed, so it has no lead segment when the bucket activates and the text would never be sent. Remove the dependency to enable it.',
    };
  }
  return { available: true };
}

export function validatePrecallSms(
  cfg: Record<string, unknown> | undefined,
): string[] {
  if (!cfg?.enabled) return [];
  const errors: string[] = [];
  const template = String(cfg.messageTemplate ?? '');
  const clinicName = String(cfg.clinicName ?? '');

  if (!template.trim()) errors.push('Pre-call SMS: messageTemplate is required');
  if (!String(cfg.originationNumberArn ?? '').trim())
    errors.push('Pre-call SMS: originationNumberArn is required');
  const placeholders = extractPlaceholders(template);
  // Mirrors the backend's conditional guard exactly (_validate_precall_sms):
  // `"ClinicName" in extract_placeholders(tmpl) and not precall.get("clinicName")`.
  // Requiring clinicName unconditionally — whenever enabled — was stricter
  // than the server and rejected valid configs whose template never
  // references {{ClinicName}} at all.
  if (placeholders.has('ClinicName') && !clinicName.trim())
    errors.push('Pre-call SMS: clinicName is required (it is interpolated into the message)');

  const unknown = [...placeholders].filter(
    (f) => !PRECALL_ALLOWED_PLACEHOLDERS.has(f),
  );
  if (unknown.length)
    errors.push(
      `Pre-call SMS: placeholder(s) not allowed: ${unknown.sort().join(', ')}. ` +
        `Only {{FirstName}} and {{ClinicName}} may be used.`,
    );

  const rendered = renderedWorstCaseLength(template, clinicName);
  if (rendered > MAX_SMS_CHARS)
    errors.push(
      `Pre-call SMS: renders to ${rendered} characters with a long first name, over the ${MAX_SMS_CHARS} limit`,
    );

  return errors;
}

/**
 * Client-side mirror of the bulk-SMS rules enforced by
 * services/api-plans/src/handlers/plans.py (_validate_sms_campaign), which
 * shares _screen_sms_template_content with _validate_precall_sms above.
 *
 * SCOPE, deliberately narrow — same principle as validatePrecallSms: this
 * duplicates only the cheap, stable rules (required template, the placeholder
 * allowlist, the rendered-length ceiling, and the conditional clinicName
 * requirement). It does NOT reimplement the PHI regexes in _PHI_PATTERNS —
 * the server stays the single source of truth for those.
 */
export function validateBulkSms(
  cfg: Record<string, unknown> | undefined,
): string[] {
  const errors: string[] = [];
  const template = String(cfg?.smsMessageTemplate ?? '');
  const clinicName = String(cfg?.clinicName ?? '');

  if (!template.trim()) errors.push('SMS: message template is required');

  const placeholders = extractPlaceholders(template);
  // Mirrors the backend's conditional guard exactly (_validate_sms_campaign):
  // requires clinicName only if the template actually references
  // {{ClinicName}} — not unconditionally, same fix already applied to
  // validatePrecallSms's own clinicName check for the identical reason.
  if (placeholders.has('ClinicName') && !clinicName.trim())
    errors.push('SMS: clinicName is required (it is interpolated into the message)');

  const unknown = [...placeholders].filter(
    (f) => !PRECALL_ALLOWED_PLACEHOLDERS.has(f),
  );
  if (unknown.length)
    errors.push(
      `SMS: placeholder(s) not allowed: ${unknown.sort().join(', ')}. ` +
        `Only {{FirstName}} and {{ClinicName}} may be used.`,
    );

  const rendered = renderedWorstCaseLength(template, clinicName);
  if (rendered > MAX_SMS_CHARS)
    errors.push(
      `SMS: renders to ${rendered} characters with a long first name, over the ${MAX_SMS_CHARS} limit`,
    );

  return errors;
}
