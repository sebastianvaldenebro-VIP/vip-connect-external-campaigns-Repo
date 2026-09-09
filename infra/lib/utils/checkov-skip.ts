import { CfnResource } from 'aws-cdk-lib';
import { IConstruct } from 'constructs';

/**
 * Suppress specific Checkov findings on a single resource via CloudFormation
 * Metadata, instead of a global `--skip-check` (which would hide the check
 * for every resource, including ones that genuinely need it).
 *
 * Checkov reads `Metadata.checkov.skip` on the synthesized resource itself:
 * https://www.checkov.io/2.Basics/Suppressing%20and%20Skipping%20Policies.html
 */
export function skipCheckovChecks(
  construct: IConstruct,
  skips: Array<{ id: string; comment: string }>,
): void {
  const cfnResource = construct.node.defaultChild as CfnResource;
  const existing = (cfnResource.cfnOptions.metadata?.checkov?.skip as unknown[]) ?? [];
  cfnResource.cfnOptions.metadata = {
    ...cfnResource.cfnOptions.metadata,
    checkov: { skip: [...existing, ...skips] },
  };
}
