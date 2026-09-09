/** @type {import('jest').Config} */
module.exports = {
  rootDir: '..',
  testMatch: ['<rootDir>/infra/**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': ['ts-jest', { tsconfig: '<rootDir>/tsconfig.json' }],
  },
  testEnvironment: 'node',
  collectCoverage: true,
  collectCoverageFrom: [
    'infra/lib/**/*.ts',
    'infra/bin/**/*.ts',
    '!infra/**/*.test.ts',
    '!infra/**/*.d.ts',
    // Dead/orphaned code, confirmed unused by infra/bin/app.ts or any other
    // stack (2026-09-09): monitoring-stack.ts is an intentionally-parked
    // reference implementation (real monitoring is created via
    // infra/scripts/create-alarms.sh, not CDK); feeder-stack.ts describes
    // infrastructure already decommissioned in production. Neither is
    // wired up anywhere — excluded rather than tested, since testing dead
    // code would be theater. Flagged to the repo owner for a delete/keep
    // decision, not fixed here.
    '!infra/lib/stacks/feeder-stack.ts',
    '!infra/lib/stacks/monitoring-stack.ts',
  ],
  coverageDirectory: '<rootDir>/infra/coverage',
  coverageThreshold: {
    global: {
      branches: 100,
      functions: 100,
      lines: 100,
      statements: 100,
    },
  },
};
