/** Mirrors the versioned sms_campaign.py contract; the server is authoritative. */
import type { SmsOriginationNumber } from './api';

export const SMS_CAMPAIGN_TEMPLATE_VERSION = 'campaign-v1' as const;
export const NO_PROMOTIONAL_SMS_NUMBER = 'No promotional SMS sending number is available. Ask your administrator to configure an active promotional number with SMS capability.';

/** Local to campaign-v1; precall and legacy selectors keep their existing behavior. */
export function isPromotionalSmsNumber(number: SmsOriginationNumber): boolean {
  return number.status === 'ACTIVE' && number.messageType === 'PROMOTIONAL'
    && Array.isArray(number.numberCapabilities) && number.numberCapabilities.includes('SMS');
}

export const SMS_CAMPAIGN_BOOKING_URL = 'https://luma-link.com/ly8aKJQzsjm';
export const DEFAULT_SMS_CAMPAIGN_TEMPLATE =
  "Hey {{FirstName}}! It's VIP Medical Group and we noticed that booking your consult might still be on your to-do list. Let's cross it off! Book your own appointment in 30 seconds - no phone calls needed: " + SMS_CAMPAIGN_BOOKING_URL;

const TOKEN = /\{\{\s*(\w+)\s*\}\}/g;
const GSM_BASIC = new Set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà');
const GSM_EXTENSION = new Set('\f^{}\\[~]|€');

/** A fictional example only; names for delivery come from Customer Profiles. */
export function previewSmsCampaign(template: string, firstName = 'Alex'): string {
  const clean = firstName.normalize('NFC').trim();
  const name = clean && /^[\p{L}\p{M}'’\- ]+$/u.test(clean)
    ? [...clean].slice(0, 20).join('') : 'there';
  return template.replace(TOKEN, (token, field: string) => field === 'FirstName' ? name : token);
}

export function smsMessageStats(body: string): {
  characters: number; parts: number; unicode: boolean; units: number;
} {
  const characters = [...body].length;
  const unicode = [...body].some((char) => !GSM_BASIC.has(char) && !GSM_EXTENSION.has(char));
  const units = unicode ? body.length
    : [...body].reduce((total, char) => total + (GSM_EXTENSION.has(char) ? 2 : 1), 0);
  const single = unicode ? 70 : 160;
  return { characters, unicode, units, parts: units === 0 ? 0 : units <= single ? 1 : Math.ceil(units / (unicode ? 67 : 153)) };
}

/** Counter reserves the same 20-character first-name budget as the server. */
export function smsCampaignMessageStats(template: string) {
  return smsMessageStats(previewSmsCampaign(template, 'A'.repeat(20)));
}

export function validateSmsCampaignTemplate(template: string): string[] {
  const errors: string[] = [];
  if (typeof template !== 'string') return ['SMS: message template must be text'];
  if (!template.trim()) return ['SMS: message template is required'];
  if ([...template].length > 1600) errors.push('SMS: template exceeds 1600 characters');
  const matches = [...template.matchAll(TOKEN)];
  if (matches.some((m) => m[1] !== 'FirstName'))
    errors.push('SMS: only {{FirstName}} can be personalized in this campaign');
  if (matches.some((m) => template[m.index! - 1] === '{' || template[m.index! + m[0].length] === '}')
      || /\{\{|\}\}/.test(template.replace(TOKEN, '')))
    errors.push('SMS: malformed placeholder; use {{FirstName}}');
  const links = [...template.matchAll(/(?:[a-z][a-z0-9+.-]*:\/\/|www\.|\/\/)\S*|(?:[\p{L}\p{N}_-]+\.)+\p{L}{2,}(?::\d+)?(?:[/?#]\S*)?|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:[/?#]\S*)?/giu)];
  if (links.some((match) => match[0] !== SMS_CAMPAIGN_BOOKING_URL
      || (match.index! > 0 && !/\s/.test(template[match.index! - 1]))))
    errors.push('SMS: use the provided Luma booking link without extra parameters');
  if (template.includes('${')) errors.push('SMS: use {{FirstName}} for personalization');
  const stats = smsCampaignMessageStats(template);
  if (stats.characters > 1600 || stats.units > (stats.unicode ? 630 : 1530))
    errors.push(`SMS: message exceeds the ${stats.unicode ? '630 Unicode units' : '1530 GSM units'} limit after personalization`);
  if (matches.some((m) => m[1] === 'FirstName')
      && smsMessageStats(previewSmsCampaign(template, '𠀀'.repeat(20))).units > 630)
    errors.push('SMS: shorten the message so it fits with any patient first name (630 Unicode units)');
  return errors;
}
