# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-09

### Changed

- Lambda IAM policies now grant `s3:GetObject` and `s3:ListBucket` only on the key prefix of `source_s3_uri` instead of the whole source bucket. The handlers take the source URI from the invocation event, so IAM is what bounds them to the configured prefix ([#1](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/1))
- `source_s3_uri` must now include a key prefix (`s3://<bucket>/<prefix>`). A bare bucket URI is rejected at plan time because it would widen the grant back to the whole bucket

## [0.1.0] - 2026-09-07

### Added

- Initial conversion of [aws-samples/s3-small-object-compaction](https://github.com/aws-samples/s3-small-object-compaction) from AWS CDK to a Terraform module
