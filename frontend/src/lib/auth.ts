import { Amplify } from 'aws-amplify';
import {
  fetchAuthSession,
  getCurrentUser,
  signInWithRedirect,
  signOut as amplifySignOut,
} from 'aws-amplify/auth';

import { config } from './config';

const PREVIEW_USER = {
  userId: 'preview-user',
  username: 'preview@local',
};

export function configureAuth(): void {
  if (config.previewMode) return;

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.cognito.userPoolId,
        userPoolClientId: config.cognito.userPoolClientId,
        loginWith: {
          oauth: {
            domain: config.cognito.domain,
            scopes: ['openid', 'email', 'profile'],
            redirectSignIn: [config.cognito.redirectSignIn],
            redirectSignOut: [config.cognito.redirectSignOut],
            responseType: 'code',
          },
        },
      },
    },
  });
}

export async function currentUser() {
  if (config.previewMode) return PREVIEW_USER;
  try {
    return await getCurrentUser();
  } catch {
    return null;
  }
}

export async function getAccessToken(): Promise<string | null> {
  if (config.previewMode) return 'preview-token';
  const session = await fetchAuthSession();
  return session.tokens?.accessToken?.toString() ?? null;
}

export async function getIdToken(): Promise<string | null> {
  if (config.previewMode) return 'preview-token';
  const session = await fetchAuthSession();
  return session.tokens?.idToken?.toString() ?? null;
}

/** Cognito groups from the ID token's `cognito:groups` claim — decoded
 * client-side purely for UI nav gating (hide items an Agent can't reach).
 * The real security boundary is server-side (the Lambda authorizer);
 * nothing here should be treated as an authorization decision. */
export async function getUserGroups(): Promise<string[]> {
  if (config.previewMode) return ['Admin'];
  const session = await fetchAuthSession();
  const groups = session.tokens?.idToken?.payload?.['cognito:groups'];
  return Array.isArray(groups) ? groups.filter((g): g is string => typeof g === 'string') : [];
}

export async function signIn(): Promise<void> {
  if (config.previewMode) return;
  await signInWithRedirect();
}

export async function signOut(): Promise<void> {
  if (config.previewMode) {
    alert('Preview mode: sign-out disabled.');
    return;
  }
  await amplifySignOut();
}
