# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Distributed Map child executions could not assume the state-machine role, which made the Step Functions variant of v0.3.0 unusable: every execution failed at the `ForEachS3Prefix` map with `States.ExceedToleratedFailureThreshold` and the compact Lambda never ran. The `aws:SourceArn` trust-policy condition added in v0.3.0 allowed only the `stateMachine:` ARN, but Step Functions starts the children of a Distributed Map with the Map Run ARN, `mapRun:<state machine name>/<map label>:<uuid>`. The condition now allows both, still scoped to this state machine and account. Trust-policy update only, no resource replacement ([#10](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/10))

## [0.3.0] - 2026-09-14

### Changed

- The Step Functions state machine and its execution log group are now created by [`terraform-aws-modules/step-functions/aws`](https://registry.terraform.io/modules/terraform-aws-modules/step-functions/aws) pinned to `5.1.1`, instead of hand-rolled `aws_sfn_state_machine` / `aws_cloudwatch_log_group` resources ([#5](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/5)). Section 5 of the FivexL module standardisation guide requires a verified registry module where one exists, and the Lambda functions in this module already follow that rule. The state-machine IAM role and its policy stay in this module and are handed to the step-functions module with `use_existing_role`: the module builds its own trust policy and cannot express the `aws:SourceAccount` / `aws:SourceArn` conditions added for [#7](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/7). No IAM resource is created, destroyed or changed
- **`aws` provider floor raised from `>= 6.0` to `>= 6.28`**, required by the step-functions module

### Upgrade notes

`moved` blocks carry the state machine and its log group into the module, so
neither is replaced, and IAM is untouched. Expect a plan with one in-place
update, a `Name` tag the module adds to the state machine, and no other drift.

### Fixed

- Compaction is now idempotent. Both compact handlers wrote their merged output to a fixed path under `/tmp` in append mode and never removed it. Lambda reuses warm execution environments with `/tmp` intact, so re-running a date appended the day's data onto the previous run's file and uploaded an object with the content doubled, a retry after a timeout appended onto the partial file, and a long backlog filled the disk. The output is now a fresh temp file per run, truncated on open and removed afterwards, so re-running a date overwrites instead of appending ([#7](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/7))

### Security

- The state-machine role trust policy now requires `aws:SourceAccount` to be this account and `aws:SourceArn` to be the compaction state machine. The role carries `"*"`-scoped CloudWatch Logs delivery grants that AWS does not allow narrowing, so without the conditions any principal with `iam:PassRole` on the role and `states:CreateStateMachine` could attach it to their own state machine ([#7](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/7))

## [0.2.1] - 2026-09-09

### Changed

- Pin the nested `terraform-aws-modules/lambda/aws` module to exactly `8.8.0` instead of `~> 8.0`. Module sources are not covered by `.terraform.lock.hcl`, so a floating constraint re-resolved to the newest 8.x on every `terraform init`, and that module runs `package.py` at plan time. Bumps are now deliberate one-line diffs. No resource changes

## [0.2.0] - 2026-09-09

### Changed

- Lambda IAM policies now grant `s3:GetObject` and `s3:ListBucket` only on the key prefix of `source_s3_uri` instead of the whole source bucket. The handlers take the source URI from the invocation event, so IAM is what bounds them to the configured prefix ([#1](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/1))
- `source_s3_uri` must now include a key prefix (`s3://<bucket>/<prefix>`). A bare bucket URI is rejected at plan time because it would widen the grant back to the whole bucket

## [0.1.0] - 2026-09-07

### Added

- Initial conversion of [aws-samples/s3-small-object-compaction](https://github.com/aws-samples/s3-small-object-compaction) from AWS CDK to a Terraform module
