import type { SpawnSyncReturns } from 'node:child_process';
import { spawnSync } from 'node:child_process';

jest.mock('node:child_process');

/**
 * `buildBundledPythonCode()`'s `local.tryBundle` callback is only ever
 * invoked by CDK's own asset-staging pipeline (real Docker/pip round-trip at
 * synth time) — not reachable as a plain function through the public CDK
 * API. Driving it for real here would shell out to a real `pip`/`docker` on
 * whatever machine runs `jest`, which is neither deterministic nor
 * appropriate for a unit test.
 *
 * Instead, `aws-cdk-lib/aws-lambda`'s `Code.fromAsset` is partially mocked to
 * capture the exact `bundling` options object this function builds
 * (including the `local.tryBundle` closure) without running real asset
 * staging, then `tryBundle` is invoked directly with `node:child_process`'s
 * `spawnSync` mocked — same pattern as `shared-layer.test.ts`, which tests
 * `buildSharedLayer()`'s one fixed combination of these options. This file
 * exercises every optional-field combination directly, since
 * `buildBundledPythonCode()` is the shared factory both `buildSharedLayer()`
 * and `ApiAuthorizerStack` call with different subsets of them.
 */
let capturedBundling: cdk.BundlingOptions | undefined;

jest.mock('aws-cdk-lib/aws-lambda', () => {
  const actual = jest.requireActual('aws-cdk-lib/aws-lambda');
  return {
    ...actual,
    Code: {
      ...actual.Code,
      fromAsset: (
        _assetPath: string,
        options?: { bundling?: unknown },
      ) => {
        capturedBundling = options?.bundling as cdk.BundlingOptions | undefined;
        // Point the "asset" at this very test directory instead of the real
        // assetRoot, and WITHOUT the bundling option, so callers get a real,
        // already-existing directory to stage — no bundling (Docker or
        // local) is ever attempted by the real CDK asset pipeline.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        return actual.Code.fromAsset(__dirname) as any;
      },
    },
  };
});

import * as cdk from 'aws-cdk-lib';
import { buildBundledPythonCode } from './python-bundling';

const mockedSpawnSync = spawnSync as jest.MockedFunction<typeof spawnSync>;

function statusResult(status: number): SpawnSyncReturns<Buffer> {
  return { status, signal: null, output: [], pid: 1, stdout: Buffer.from(''), stderr: Buffer.from('') };
}

function getTryBundle(
  opts: Parameters<typeof buildBundledPythonCode>[0],
): (outputDir: string) => boolean {
  buildBundledPythonCode(opts);
  const local = capturedBundling?.local;
  if (!local?.tryBundle) {
    throw new Error('bundling.local.tryBundle was not captured');
  }
  return (outputDir: string) => local.tryBundle(outputDir, capturedBundling as cdk.BundlingOptions);
}

describe('buildBundledPythonCode', () => {
  beforeEach(() => {
    capturedBundling = undefined;
    mockedSpawnSync.mockReset();
  });

  it('builds the docker command with defaults when srcSubdir/outputSubdir/requirementsFile/extraPipArgs are all omitted', () => {
    buildBundledPythonCode({ assetRoot: '/asset/root' });
    const command = capturedBundling?.command as string[];
    const script = command[2];
    expect(script).toContain('mkdir -p /asset-output');
    expect(script).toContain('cp -r /asset-input/. /asset-output/');
    expect(script).toContain('pip install -r /asset-input/requirements.txt -t /asset-output --no-cache-dir');
    // No trailing space from an empty extraPipArgs suffix.
    expect(script.endsWith('--no-cache-dir')).toBe(true);
  });

  it('builds the docker command with srcSubdir/outputSubdir/requirementsFile/extraPipArgs all provided', () => {
    buildBundledPythonCode({
      assetRoot: '/asset/root',
      srcSubdir: 'src',
      outputSubdir: 'python',
      requirementsFile: 'reqs-custom.txt',
      extraPipArgs: ['--platform', 'manylinux2014_x86_64'],
    });
    const command = capturedBundling?.command as string[];
    const script = command[2];
    expect(script).toContain('mkdir -p /asset-output/python');
    expect(script).toContain('cp -r /asset-input/src/. /asset-output/python/');
    expect(script).toContain(
      'pip install -r /asset-input/reqs-custom.txt -t /asset-output/python --no-cache-dir --platform manylinux2014_x86_64',
    );
  });

  describe('local.tryBundle', () => {
    it('returns false when the `pip --version` availability check fails', () => {
      mockedSpawnSync.mockReturnValueOnce(statusResult(1));
      const tryBundle = getTryBundle({ assetRoot: '/asset/root' });

      expect(tryBundle('/tmp/does-not-matter')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(1);
      expect(mockedSpawnSync).toHaveBeenNthCalledWith(1, 'pip', ['--version'], { stdio: 'ignore' });
    });

    it('returns false when the mkdir step fails', () => {
      mockedSpawnSync
        .mockReturnValueOnce(statusResult(0)) // pip --version
        .mockReturnValueOnce(statusResult(1)); // mkdir
      const tryBundle = getTryBundle({ assetRoot: '/asset/root' });

      expect(tryBundle('/tmp/out')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(2);
      expect(mockedSpawnSync.mock.calls[1][0]).toBe('mkdir');
    });

    it('returns false when the cp step fails', () => {
      mockedSpawnSync
        .mockReturnValueOnce(statusResult(0)) // pip --version
        .mockReturnValueOnce(statusResult(0)) // mkdir
        .mockReturnValueOnce(statusResult(1)); // cp
      const tryBundle = getTryBundle({ assetRoot: '/asset/root' });

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
      const tryBundle = getTryBundle({ assetRoot: '/asset/root' });

      expect(tryBundle('/tmp/out')).toBe(false);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(4);
      expect(mockedSpawnSync.mock.calls[3][0]).toBe('pip');
      expect(mockedSpawnSync.mock.calls[3][1]).toEqual(
        expect.arrayContaining(['install', '--no-cache-dir', '--quiet']),
      );
    });

    it('succeeds with outputSubdir omitted — localOut is the bare outputDir', () => {
      mockedSpawnSync.mockReturnValue(statusResult(0));
      const tryBundle = getTryBundle({ assetRoot: '/asset/root' });

      expect(tryBundle('/tmp/out')).toBe(true);
      expect(mockedSpawnSync.mock.calls[1][1]).toEqual(['-p', '/tmp/out']); // mkdir -p localOut
    });

    it('succeeds with outputSubdir provided — localOut is joined under outputDir', () => {
      mockedSpawnSync.mockReturnValue(statusResult(0));
      const tryBundle = getTryBundle({ assetRoot: '/asset/root', srcSubdir: 'src', outputSubdir: 'python' });

      expect(tryBundle('/tmp/out')).toBe(true);
      expect(mockedSpawnSync).toHaveBeenCalledTimes(4);
      const mkdirArgs = mockedSpawnSync.mock.calls[1][1] as string[];
      expect(mkdirArgs[1]).toBe(require('path').join('/tmp/out', 'python'));
      for (const call of mockedSpawnSync.mock.calls.slice(1)) {
        expect(call[2]).toEqual({ stdio: 'inherit' });
      }
    });

    it('passes extraPipArgs through to the pip install step', () => {
      mockedSpawnSync.mockReturnValue(statusResult(0));
      const tryBundle = getTryBundle({
        assetRoot: '/asset/root',
        extraPipArgs: ['--platform', 'manylinux2014_x86_64'],
      });

      expect(tryBundle('/tmp/out')).toBe(true);
      expect(mockedSpawnSync.mock.calls[3][1]).toEqual(
        expect.arrayContaining(['--platform', 'manylinux2014_x86_64']),
      );
    });
  });
});
