import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { spawnSync } from 'node:child_process';

export interface BundlePythonCodeOptions {
  /** Directory containing the source subdir + requirements.txt (e.g. services/api-authorizer). */
  readonly assetRoot: string;
  /** Subdir of assetRoot holding the source to copy (e.g. 'src' or 'python'). Omit to copy assetRoot itself. */
  readonly srcSubdir?: string;
  /** Subdir INSIDE the output asset to place code+deps under (e.g. 'python' for layers). Omit for the asset root. */
  readonly outputSubdir?: string;
  /** requirements.txt path relative to assetRoot. Default 'requirements.txt'. */
  readonly requirementsFile?: string;
  /** Extra pip install flags — e.g. platform-forcing flags for a compiled dependency. */
  readonly extraPipArgs?: string[];
}

/**
 * Build a Lambda code asset by copying a source subdir and pip-installing its
 * requirements into the output — shared by the vip_shared layer and any
 * function bundling its own dependencies outside that layer.
 *
 * Prefers native pip when available (works on dev machines without Docker
 * Desktop) and falls back to the Lambda-runtime bundling image otherwise.
 */
export function buildBundledPythonCode(
  opts: BundlePythonCodeOptions,
): lambda.Code {
  const srcSubdir = opts.srcSubdir ?? '';
  const outputSubdir = opts.outputSubdir ?? '';
  const requirementsFile = opts.requirementsFile ?? 'requirements.txt';
  const extraPipArgs = opts.extraPipArgs ?? [];

  const srcPath = srcSubdir
    ? path.join(opts.assetRoot, srcSubdir)
    : opts.assetRoot;
  const requirementsPath = path.join(opts.assetRoot, requirementsFile);

  const dockerSrc = srcSubdir ? `/asset-input/${srcSubdir}` : '/asset-input';
  const dockerOut = outputSubdir
    ? `/asset-output/${outputSubdir}`
    : '/asset-output';
  const dockerReq = `/asset-input/${requirementsFile}`;
  const pipArgsSuffix = extraPipArgs.length ? ` ${extraPipArgs.join(' ')}` : '';

  return lambda.Code.fromAsset(opts.assetRoot, {
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      command: [
        'bash',
        '-c',
        [
          `mkdir -p ${dockerOut}`,
          `cp -r ${dockerSrc}/. ${dockerOut}/`,
          `pip install -r ${dockerReq} -t ${dockerOut} --no-cache-dir${pipArgsSuffix}`,
        ].join(' && '),
      ],
      // Using spawnSync (no shell) so paths are passed as argv, not interpolated.
      local: {
        tryBundle(outputDir: string): boolean {
          if (
            spawnSync('pip', ['--version'], { stdio: 'ignore' }).status !== 0
          ) {
            return false;
          }
          // outputDir is a temp directory CDK's own asset-bundling framework
          // creates and passes to this callback at synth/deploy time —
          // build-time tooling, not a request-handling path. No external or
          // attacker-controlled input reaches this join.
          // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
          const localOut = outputSubdir
            ? path.join(outputDir, outputSubdir)
            : outputDir;
          const steps: Array<[string, string[]]> = [
            ['mkdir', ['-p', localOut]],
            ['cp', ['-r', `${srcPath}/.`, localOut]],
            [
              'pip',
              [
                'install',
                '-r',
                requirementsPath,
                '-t',
                localOut,
                '--no-cache-dir',
                '--quiet',
                ...extraPipArgs,
              ],
            ],
          ];
          for (const [cmd, args] of steps) {
            const result = spawnSync(cmd, args, { stdio: 'inherit' });
            if (result.status !== 0) return false;
          }
          return true;
        },
      },
    },
  });
}
