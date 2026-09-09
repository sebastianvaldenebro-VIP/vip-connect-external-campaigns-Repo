import type { SpawnSyncReturns } from 'node:child_process';
import { spawnSync } from 'node:child_process';

jest.mock('node:child_process');

/**
 * `buildSharedLayer()`'s `local.tryBundle` callback is only ever invoked by
 * CDK's own asset-staging pipeline (real Docker/pip round-trip at synth
 * time) — not reachable as a plain function through the public CDK API.
 * Driving it for real here would shell out to a real `pip`/`docker` on
 * whatever machine runs `jest`, which is neither deterministic nor
 * appropriate for a unit test.
 *
 * Instead, `aws-cdk-lib/aws-lambda`'s `Code.fromAsset` is partially mocked
 * to capture the exact `bundling` options object `buildSharedLayer()` builds
 * (including the `local.tryBundle` closure) without running real asset
 * staging, then `tryBundle` is invoked directly with `node:child_process`'s
 * `spawnSync` mocked — giving full, deterministic control over each branch
 * (pip-missing fallback, each step failing, full success) with no real
 * filesystem/network/Docker side effects.
 */
let capturedBundling: cdk.BundlingOptions | undefined;

jest.mock('aws-cdk-lib/aws-lambda', () => {
  const actual = jest.requireActual('aws-cdk-lib/aws-lambda');
  return {
    ...actual,
    Code: {
      ...actual.Code,
      fromAsset: (
        assetPath: string,
        options?: { bundling?: unknown },
      ) => {
        capturedBundling = options?.bundling as cdk.BundlingOptions | undefined;
        // Point the "asset" at this very test directory instead of the real
        // sharedRoot, and WITHOUT the bundling option, so LayerVersion gets a
        // real, valid, already-existing directory to stage — no bundling
        // (Docker or local) is ever attempted by the real CDK asset pipeline.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        return actual.Code.fromAsset(__dirname) as any;
      },
    },
  };
});

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { buildSharedLayer } from './shared-layer';

const mockedSpawnSync = spawnSync as jest.MockedFunction<typeof spawnSync>;

function statusResult(status: number): SpawnSyncReturns<Buffer> {
  return { status, signal: null, output: [], pid: 1, stdout: Buffer.from(''), stderr: Buffer.from('') };
}

function buildStackWithLayer(id?: string) {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'TestStack', {
    env: { account: '165505826690', region: 'us-east-1' },
  });
  const layer = id === undefined ? buildSharedLayer(stack) : buildSharedLayer(stack, id);
  return { stack, layer };
}

describe('buildSharedLayer', () => {
  beforeEach(() => {
    capturedBundling = undefined;
    mockedSpawnSync.mockReset();
  });

  it('builds a LayerVersion with the expected compatible runtime and description', () => {
    const { stack } = buildStackWithLayer('TestLayer');
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: ['python3.12'],
      Description: 'vip_shared domain + infrastructure + deps',
    });
  });

  it('defaults the construct id to "SharedLayer" when none is given', () => {
    const { stack } = buildStackWithLayer();
    expect(stack.node.tryFindChild('SharedLayer')).toBeDefined();
  });

  it('uses a custom construct id when one is given', () => {
    const { stack } = buildStackWithLayer('CustomLayerId');
    expect(stack.node.tryFindChild('CustomLayerId')).toBeDefined();
    expect(stack.node.tryFindChild('SharedLayer')).toBeUndefined();
  });

  describe('local.tryBundle', () => {
    function getTryBundle(): (outputDir: string) => boolean {
      buildStackWithLayer('TestLayer');
      const local = capturedBundling?.local;
      if (!local?.tryBundle) {
        throw new Error('bundling.local.tryBundle was not captured');
      }
      return (outputDir: string) => local.tryBundle(outputDir, capturedBundling as cdk.BundlingOptions);
    }

    it('returns false when the `pip --version` availability check fails', () => {
      mockedSpawnSync.mockReturnValueOnce(statusResult(1));
      const tryBundle = getTryBundle();

      expect(tryBundle('/tmp/does-not-matter')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(1);
      expect(mockedSpawnSync).toHaveBeenNthCalledWith(1, 'pip', ['--version'], { stdio: 'ignore' });
    });

    it('returns false when the mkdir step fails', () => {
      mockedSpawnSync
        .mockReturnValueOnce(statusResult(0)) // pip --version
        .mockReturnValueOnce(statusResult(1)); // mkdir
      const tryBundle = getTryBundle();

      expect(tryBundle('/tmp/out')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(2);
      expect(mockedSpawnSync.mock.calls[1][0]).toBe('mkdir');
    });

    it('returns false when the cp step fails', () => {
      mockedSpawnSync
        .mockReturnValueOnce(statusResult(0)) // pip --version
        .mockReturnValueOnce(statusResult(0)) // mkdir
        .mockReturnValueOnce(statusResult(1)); // cp
      const tryBundle = getTryBundle();

      expect(tryBundle('/tmp/out')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(3);
      expect(mockedSpawnSync.mock.calls[2][0]).toBe('cp');
    });

    it('returns false when the pip install step fails', () => {
      mockedSpawnSync
        .mockReturnValueOnce(statusResult(0)) // pip --version
        .mockReturnValueOnce(statusResult(0)) // mkdir
        .mockReturnValueOnce(statusResult(0)) // cp
        .mockReturnValueOnce(statusResult(1)); // pip install
      const tryBundle = getTryBundle();

      expect(tryBundle('/tmp/out')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(4);
      expect(mockedSpawnSync.mock.calls[3][0]).toBe('pip');
      expect(mockedSpawnSync.mock.calls[3][1]).toEqual(
        expect.arrayContaining(['install', '--no-cache-dir', '--quiet']),
      );
    });

    it('returns true when pip is available and every step succeeds', () => {
      mockedSpawnSync.mockReturnValue(statusResult(0));
      const tryBundle = getTryBundle();

      expect(tryBundle('/tmp/out')).toBe(true);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(4);
      for (const call of mockedSpawnSync.mock.calls.slice(1)) {
        expect(call[2]).toEqual({ stdio: 'inherit' });
      }
    });
  });
});
