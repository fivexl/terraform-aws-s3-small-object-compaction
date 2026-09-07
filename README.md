[![FivexL](https://releases.fivexl.io/like-this-repo-banner.png)](https://fivexl.io/#email-subscription)

### Want practical AWS infrastructure insights?

👉 [Subscribe to our newsletter](https://fivexl.io/#email-subscription) to get:

- Real stories from real AWS projects
- No-nonsense DevOps tactics
- Cost, security & compliance patterns that actually work
- Expert guidance from engineers in the field

=========================================================================

# Amazon S3 Small Object Compaction

Terraform module to compact small S3 objects into larger files with AWS Lambda and AWS Step Functions, cutting storage costs on tiers with minimum billable object sizes (e.g. 128 KB) and speeding up Amazon Athena queries.

## Acknowledgments

This project is based on [aws-samples/s3-small-object-compaction](https://github.com/aws-samples/s3-small-object-compaction). We thank the original contributors for their work on the CDK-based solution that inspired this Terraform module.

## Architecture

The module deploys two variants of the compaction solution:

1. A **standalone Lambda function** (via [terraform-aws-modules/lambda/aws](https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws)) that iterates over a list of Amazon S3 prefixes and compacts the objects in each into a single larger file
2. An **AWS Step Functions state machine** using [Distributed Map](https://docs.aws.amazon.com/step-functions/latest/dg/use-dist-map-orchestrate-large-scale-parallel-workloads.html) to invoke a compaction Lambda in parallel for each prefix, for faster compaction at scale

Both variants can be triggered on an **EventBridge schedule** (disabled by default, matching the upstream solution).

## Usage

See [examples/basic](./examples/basic).

<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->

## License

Apache 2.0 Licensed. See [LICENSE](./LICENSE) for full details.
