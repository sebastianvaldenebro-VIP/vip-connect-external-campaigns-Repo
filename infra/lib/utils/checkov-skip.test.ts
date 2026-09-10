import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { CfnResource } from 'aws-cdk-lib';
import { skipCheckovChecks } from './checkov-skip';

function buildBucket() {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'TestStack', {
    env: { account: '165505826690', region: 'us-east-1' },
  });
  const bucket = new s3.Bucket(stack, 'TestBucket');
  return bucket;
}

describe('skipCheckovChecks', () => {
  it('sets Metadata.checkov.skip with the given id/comment on a construct with no prior skips', () => {
    const bucket = buildBucket();
    skipCheckovChecks(bucket, [{ id: 'CKV_AWS_18', comment: 'no access logging needed here' }]);

    const cfnResource = bucket.node.defaultChild as CfnResource;
    expect(cfnResource.cfnOptions.metadata).toEqual({
      checkov: {
        skip: [{ id: 'CKV_AWS_18', comment: 'no access logging needed here' }],
      },
    });
  });

  it('accumulates skip entries across two separate calls instead of overwriting the first', () => {
    const bucket = buildBucket();

    skipCheckovChecks(bucket, [{ id: 'CKV_AWS_18', comment: 'first skip' }]);
    skipCheckovChecks(bucket, [{ id: 'CKV_AWS_21', comment: 'second skip' }]);

    const cfnResource = bucket.node.defaultChild as CfnResource;
    expect(cfnResource.cfnOptions.metadata).toEqual({
      checkov: {
        skip: [
          { id: 'CKV_AWS_18', comment: 'first skip' },
          { id: 'CKV_AWS_21', comment: 'second skip' },
        ],
      },
    });
  });

  it('accumulates multiple entries passed in a single call together with pre-existing ones', () => {
    const bucket = buildBucket();

    skipCheckovChecks(bucket, [{ id: 'CKV_AWS_18', comment: 'first skip' }]);
    skipCheckovChecks(bucket, [
      { id: 'CKV_AWS_21', comment: 'second skip' },
      { id: 'CKV_AWS_144', comment: 'third skip' },
    ]);

    const cfnResource = bucket.node.defaultChild as CfnResource;
    expect((cfnResource.cfnOptions.metadata as { checkov: { skip: unknown[] } }).checkov.skip).toEqual([
      { id: 'CKV_AWS_18', comment: 'first skip' },
      { id: 'CKV_AWS_21', comment: 'second skip' },
      { id: 'CKV_AWS_144', comment: 'third skip' },
    ]);
  });

  it('preserves other existing cfnOptions.metadata keys when adding the checkov skip', () => {
    const bucket = buildBucket();
    const cfnResource = bucket.node.defaultChild as CfnResource;
    cfnResource.cfnOptions.metadata = { 'aws:cdk:path': 'TestStack/TestBucket/Resource' };

    skipCheckovChecks(bucket, [{ id: 'CKV_AWS_18', comment: 'no access logging needed here' }]);

    expect(cfnResource.cfnOptions.metadata).toEqual({
      'aws:cdk:path': 'TestStack/TestBucket/Resource',
      checkov: { skip: [{ id: 'CKV_AWS_18', comment: 'no access logging needed here' }] },
    });
  });
});
