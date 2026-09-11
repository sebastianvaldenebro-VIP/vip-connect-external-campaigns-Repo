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
 *
 * Most callers should use the default `requirements.txt` (the shared base
 * every stack gets). Only pass `requirementsFile` when a caller needs a
 * dependency that is NOT shared by every stack — e.g. `api-sms` needs
 * `phonenumbers` (~48 MB of geocoding/timezone data) for TCPA quiet-hours
 * resolution, but the other 6 stacks (Campaigns, Segments, Profiles, Plans,
 * Metrics, ProgressiveDialer) never import anything that uses it. Adding it
 * to the shared base would bloat all 7 stacks' layer copies; instead
 * `api-sms-stack.ts` passes its own superset file (`requirements-sms.txt`)
 * so only its layer copy carries the extra weight.
 */
export function buildSharedLayer(
  scope: Construct,
  id = 'SharedLayer',
  requirementsFile = 'requirements.txt',
): lambda.LayerVersion {
  const sharedRoot = path.join(__dirname, '../../../services/shared');
  return new lambda.LayerVersion(scope, id, {
    layerVersionName: undefined, // let CFN generate per-stack unique names
    compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    description: 'vip_shared domain + infrastructure + deps',
    code: buildBundledPythonCode({
      assetRoot: sharedRoot,
      srcSubdir: 'python',
      outputSubdir: 'python',
      requirementsFile,
    }),
  });
}
