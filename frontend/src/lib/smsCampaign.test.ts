import { describe, expect, it } from 'vitest';
import { DEFAULT_SMS_CAMPAIGN_TEMPLATE, SMS_CAMPAIGN_BOOKING_URL, previewSmsCampaign, smsMessageStats, smsCampaignMessageStats, validateSmsCampaignTemplate } from './smsCampaign';
import { validateBulkSms } from './precallSms';

describe('booking SMS campaign contract', () => {
  it('keeps the requested default and personalizes only the first name', () => {
    expect(previewSmsCampaign(DEFAULT_SMS_CAMPAIGN_TEMPLATE, 'José')).toBe(
      "Hey José! It's VIP Medical Group and we noticed that booking your consult might still be on your to-do list. Let's cross it off! Book your own appointment in 30 seconds - no phone calls needed: https://luma-link.com/ly8aKJQzsjm",
    );
    expect(DEFAULT_SMS_CAMPAIGN_TEMPLATE).toContain('{{FirstName}}');
    expect(validateSmsCampaignTemplate(DEFAULT_SMS_CAMPAIGN_TEMPLATE)).toEqual([]);
    expect(smsCampaignMessageStats(DEFAULT_SMS_CAMPAIGN_TEMPLATE).parts).toBe(2);
  });

  it('uses a neutral fallback and keeps editable text without a link', () => {
    expect(previewSmsCampaign('Hello {{ FirstName }}!', '12345')).toBe('Hello there!');
    expect(validateSmsCampaignTemplate('Hello {{FirstName}}! VIP Medical Group here.')).toEqual([]);
  });

  it.each(['{{LastName}}', '{{ClinicName}}', '{{{FirstName}}}', '{{ FirstName', '${FirstName}'])('rejects unsupported personalization %s', (text) => {
    expect(validateSmsCampaignTemplate(text).length).toBeGreaterThan(0);
  });

  it.each(['?patient=Alex', '#patient', '/another', '.evil.test', '@evil.test', '"', '.'])('rejects a suffix on the approved URL: %s', (suffix) => {
    expect(validateSmsCampaignTemplate(SMS_CAMPAIGN_BOOKING_URL + suffix).length).toBeGreaterThan(0);
  });

  it.each(['https://example.com', 'http://luma-link.com/ly8aKJQzsjm', 'ftp://example.com', 'www.example.com',
    'example.com', '//example.com', 'luma-link.com/other', '192.0.2.1', 'médico.com', '例子.公司',
    `x${SMS_CAMPAIGN_BOOKING_URL}`])('rejects an unsupported link %s', (text) => {
    expect(validateSmsCampaignTemplate(text).length).toBeGreaterThan(0);
  });

  it('counts GSM extensions, Unicode and surrogate pairs', () => {
    expect(smsMessageStats('^'.repeat(81))).toMatchObject({ units: 162, parts: 2, unicode: false });
    expect(smsMessageStats('é'.repeat(160))).toMatchObject({ parts: 1, unicode: false });
    expect(smsMessageStats('’'.repeat(71))).toMatchObject({ units: 71, parts: 2, unicode: true });
    expect(smsMessageStats('𠀀'.repeat(36))).toMatchObject({ characters: 36, units: 72, parts: 2 });
  });

  it('checks actual provider limits and a long Unicode name before saving', () => {
    expect(validateSmsCampaignTemplate('A'.repeat(1530))).toEqual([]);
    expect(validateSmsCampaignTemplate('A'.repeat(1531))).not.toEqual([]);
    expect(validateSmsCampaignTemplate('’'.repeat(630))).toEqual([]);
    expect(validateSmsCampaignTemplate('’'.repeat(631))).not.toEqual([]);
    expect(validateSmsCampaignTemplate('A'.repeat(591) + '{{FirstName}}')).not.toEqual([]);
  });

  it('preserves legacy limits and rejects unknown versions', () => {
    expect(validateBulkSms({ smsTemplateVersion: 'campaign-v1', smsMessageTemplate: DEFAULT_SMS_CAMPAIGN_TEMPLATE })).toEqual([]);
    expect(validateBulkSms({ smsMessageTemplate: DEFAULT_SMS_CAMPAIGN_TEMPLATE })).not.toEqual([]);
    expect(validateBulkSms({ smsTemplateVersion: null, smsMessageTemplate: 'Hi' })).not.toEqual([]);
  });
});
