import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockConfig = vi.hoisted(() => ({
  previewMode: false,
  region: 'us-east-1',
  cognito: {
    userPoolId: 'us-east-1_fakepool',
    userPoolClientId: 'fake-client-id',
    domain: 'fake-domain.auth.us-east-1.amazoncognito.com',
    redirectSignIn: 'http://localhost:5173/callback',
    redirectSignOut: 'http://localhost:5173/',
  },
  api: { baseUrl: 'https://api.example.test' },
  session: { idleTimeoutMs: 15 * 60 * 1000 },
}));

const amplifyConfigure = vi.hoisted(() => vi.fn());
const fetchAuthSession = vi.hoisted(() => vi.fn());
const getCurrentUser = vi.hoisted(() => vi.fn());
const signInWithRedirect = vi.hoisted(() => vi.fn());
const amplifySignOut = vi.hoisted(() => vi.fn());

vi.mock('./config', () => ({ config: mockConfig }));
vi.mock('aws-amplify', () => ({ Amplify: { configure: amplifyConfigure } }));
vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession,
  getCurrentUser,
  signInWithRedirect,
  signOut: amplifySignOut,
}));

import {
  configureAuth,
  currentUser,
  getAccessToken,
  getIdToken,
  signIn,
  signOut,
} from './auth';

beforeEach(() => {
  mockConfig.previewMode = false;
  amplifyConfigure.mockClear();
  fetchAuthSession.mockReset();
  getCurrentUser.mockReset();
  signInWithRedirect.mockReset();
  amplifySignOut.mockReset();
});

describe('configureAuth', () => {
  it('does nothing in preview mode', () => {
    mockConfig.previewMode = true;
    configureAuth();
    expect(amplifyConfigure).not.toHaveBeenCalled();
  });

  it('configures Amplify Cognito with the values from config when not in preview mode', () => {
    configureAuth();
    expect(amplifyConfigure).toHaveBeenCalledTimes(1);
    const arg = amplifyConfigure.mock.calls[0][0];
    expect(arg.Auth.Cognito.userPoolId).toBe('us-east-1_fakepool');
    expect(arg.Auth.Cognito.userPoolClientId).toBe('fake-client-id');
    expect(arg.Auth.Cognito.loginWith.oauth.domain).toBe('fake-domain.auth.us-east-1.amazoncognito.com');
    expect(arg.Auth.Cognito.loginWith.oauth.redirectSignIn).toEqual(['http://localhost:5173/callback']);
    expect(arg.Auth.Cognito.loginWith.oauth.redirectSignOut).toEqual(['http://localhost:5173/']);
    expect(arg.Auth.Cognito.loginWith.oauth.responseType).toBe('code');
  });
});

describe('currentUser', () => {
  it('returns the fixed preview user in preview mode, without calling Amplify', async () => {
    mockConfig.previewMode = true;
    const result = await currentUser();
    expect(result).toEqual({ userId: 'preview-user', username: 'preview@local' });
    expect(getCurrentUser).not.toHaveBeenCalled();
  });

  it('returns the Amplify user when getCurrentUser resolves', async () => {
    getCurrentUser.mockResolvedValue({ userId: 'real-user-1', username: 'agent@vipmedical.com' });
    const result = await currentUser();
    expect(result).toEqual({ userId: 'real-user-1', username: 'agent@vipmedical.com' });
  });

  it('returns null when getCurrentUser throws (not authenticated)', async () => {
    getCurrentUser.mockRejectedValue(new Error('not authenticated'));
    const result = await currentUser();
    expect(result).toBeNull();
  });
});

describe('getAccessToken', () => {
  it('returns the fixed preview token in preview mode', async () => {
    mockConfig.previewMode = true;
    const result = await getAccessToken();
    expect(result).toBe('preview-token');
    expect(fetchAuthSession).not.toHaveBeenCalled();
  });

  it('returns the access token string from the session when present', async () => {
    fetchAuthSession.mockResolvedValue({
      tokens: { accessToken: { toString: () => 'real-access-token' } },
    });
    const result = await getAccessToken();
    expect(result).toBe('real-access-token');
  });

  it('returns null when the session has no tokens', async () => {
    fetchAuthSession.mockResolvedValue({});
    const result = await getAccessToken();
    expect(result).toBeNull();
  });
});

describe('getIdToken', () => {
  it('returns the fixed preview token in preview mode', async () => {
    mockConfig.previewMode = true;
    const result = await getIdToken();
    expect(result).toBe('preview-token');
    expect(fetchAuthSession).not.toHaveBeenCalled();
  });

  it('returns the id token string from the session when present', async () => {
    fetchAuthSession.mockResolvedValue({
      tokens: { idToken: { toString: () => 'real-id-token' } },
    });
    const result = await getIdToken();
    expect(result).toBe('real-id-token');
  });

  it('returns null when the session has no tokens', async () => {
    fetchAuthSession.mockResolvedValue({});
    const result = await getIdToken();
    expect(result).toBeNull();
  });
});

describe('signIn', () => {
  it('does nothing in preview mode', async () => {
    mockConfig.previewMode = true;
    await signIn();
    expect(signInWithRedirect).not.toHaveBeenCalled();
  });

  it('delegates to Amplify signInWithRedirect otherwise', async () => {
    signInWithRedirect.mockResolvedValue(undefined);
    await signIn();
    expect(signInWithRedirect).toHaveBeenCalledTimes(1);
  });
});

describe('signOut', () => {
  let alertSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    alertSpy = vi.fn();
    vi.stubGlobal('alert', alertSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('alerts and does not call Amplify signOut in preview mode', async () => {
    mockConfig.previewMode = true;
    await signOut();
    expect(alertSpy).toHaveBeenCalledWith('Preview mode: sign-out disabled.');
    expect(amplifySignOut).not.toHaveBeenCalled();
  });

  it('delegates to Amplify signOut otherwise', async () => {
    amplifySignOut.mockResolvedValue(undefined);
    await signOut();
    expect(amplifySignOut).toHaveBeenCalledTimes(1);
    expect(alertSpy).not.toHaveBeenCalled();
  });
});
