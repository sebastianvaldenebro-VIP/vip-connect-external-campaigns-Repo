/**
 * Runtime config from Vite env vars.
 * Defined in frontend/.env.local (gitignored) or injected by Amplify build.
 * See .env.example at the repo root for the required keys.
 */

export const config = {
  previewMode: import.meta.env.VITE_PREVIEW_MODE === 'true',
  region: import.meta.env.VITE_AWS_REGION ?? 'us-east-1',
  cognito: {
    userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID ?? '',
    userPoolClientId: import.meta.env.VITE_COGNITO_CLIENT_ID ?? '',
    domain: import.meta.env.VITE_COGNITO_DOMAIN ?? '',
    redirectSignIn:
      import.meta.env.VITE_COGNITO_REDIRECT_SIGNIN ?? 'http://localhost:5173/callback',
    redirectSignOut:
      import.meta.env.VITE_COGNITO_REDIRECT_SIGNOUT ?? 'http://localhost:5173/',
  },
  api: {
    baseUrl: import.meta.env.VITE_API_BASE_URL ?? '',
  },
  session: {
    idleTimeoutMs: 15 * 60 * 1000, // 15 min HIPAA
  },
} as const;
