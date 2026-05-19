import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { spawnSync } from 'node:child_process';

/**
 * Build a per-stack copy of the shared vip_shared layer.
 *
 * Originally the layer was defined once in ApiSegmentsStack and imported by
 * the other API stacks. CloudFormation blocks updates to exports while they
 * are in use, so any change to shared code made ``cdk deploy`` fail with:
 *   "Cannot update export ... as it is in use by ..."
 *
 * The cheapest fix is to give each stack its own copy. Four ~5 MB layer
 * versions is inconsequential and completely eliminates the cross-stack
 * reference — each stack can update independently.
 */
export function buildSharedLayer(scope: Construct, id = 'SharedLayer'): lambda.LayerVersion {
  const sharedRoot = path.join(__dirname, '../../../services/shared');
  return new lambda.LayerVersion(scope, id, {
    layerVersionName: undefined, // let CFN generate per-stack unique names
    compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    description: 'vip_shared domain + infrastructure + deps',
    code: lambda.Code.fromAsset(sharedRoot, {
      bundling: {
        image: lambda.Runtime.PYTHON_3_12.bundlingImage,
        command: [
          'bash',
          '-c',
          [
            'mkdir -p /asset-output/python',
            'cp -r /asset-input/python/. /asset-output/python/',
            'pip install -r /asset-input/requirements.txt -t /asset-output/python --no-cache-dir',
          ].join(' && '),
        ],
        // Prefer native pip when available — avoids the Docker round-trip and
        // works on dev machines without Docker Desktop. Falls back to the
        // image above if pip isn't on PATH. Using spawnSync (no shell) so
        // paths are passed as argv, not interpolated.
        local: {
          tryBundle(outputDir: string): boolean {
            if (spawnSync('pip', ['--version'], { stdio: 'ignore' }).status !== 0) {
              return false;
            }
            const outputPython = path.join(outputDir, 'python');
            const inputPython = path.join(sharedRoot, 'python');
            const requirements = path.join(sharedRoot, 'requirements.txt');
            const steps: Array<[string, string[]]> = [
              ['mkdir', ['-p', outputPython]],
              ['cp', ['-r', `${inputPython}/.`, outputPython]],
              [
                'pip',
                [
                  'install',
                  '-r',
                  requirements,
                  '-t',
                  outputPython,
                  '--no-cache-dir',
                  '--quiet',
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
    }),
  });
}
