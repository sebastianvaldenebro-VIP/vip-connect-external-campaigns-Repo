import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { buildBundledPythonCode } from './python-bundling';

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
    code: buildBundledPythonCode({
      assetRoot: sharedRoot,
      srcSubdir: 'python',
      outputSubdir: 'python',
    }),
  });
}
