# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
