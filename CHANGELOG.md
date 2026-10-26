# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-10-26

### Fixed

- Parquet, ORC and Avro sources were merged by byte concatenation and written out as one object no reader can open, while the run reported success. In the default mode such objects are now detected by their magic bytes and the prefix fails with an error, and nothing is uploaded ([#21](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/21))

### Added

- `file_format` (`concat` | `parquet`, default `concat`): `parquet` merges a prefix by rewriting the row groups of its objects into one Parquet file, keeping the source schema and codec, and refuses objects whose schema differs. pyarrow comes from a Lambda layer the module builds at apply time from the PyPI wheel, created and attached to the compaction functions only in this mode. The plan fails when the compaction functions have less than 512 MB of memory ([#21](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/21))

### Changed

- `lambda_runtime` defaults to `python3.14` instead of `python3.12` and is validated to `python3.12`, `python3.13` or `python3.14`. The floor is 3.12 because the compaction handlers now stage their output in a `NamedTemporaryFile(delete_on_close=False)` instead of `mkstemp` with a manual remove, which also removes the parquet-only python3.12 check; the cap is 3.14 because the pyarrow pinned for the parquet layer publishes no wheel for newer runtimes, so the layer build would fail there
- The nested `terraform-aws-modules/lambda/aws` module moves from `8.8.0` to `8.12.0`. The releases in between only add optional inputs and outputs, all unset here, and make `package.py` show pip's stderr when a build fails. The `aws` provider floor stays at `>= 6.28`

### Security

- The parquet mode does not use the AWS SDK for pandas managed layer. Its latest release, 3.17.1, bundles certifi 2026.7.22, whose CA store still trusts the `emSign ECC Root CA - G3` root behind eight compromised eMudhra intermediates ([GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2), critical), and the layer can only be patched by a new aws-sdk-pandas release. The module's own layer holds pyarrow alone, which has no dependencies, so it carries no certifi; boto3 falls back to the CA bundle of botocore in the Lambda runtime

### Upgrade notes

With the default `file_format = "concat"` and `lambda_runtime` unset, the plan shows an in-place update of all three functions, `python3.12` to `python3.14`, plus the code update of the two compaction functions; no environment variable or layer is added. A consumer that pins `lambda_runtime` to `python3.12` or `python3.13` sees only the code update.

With `file_format = "parquet"` the machine that runs `terraform apply` needs `python3`, `pip` and access to PyPI to build the layer, a 35 MB zip.

## [0.3.4] - 2026-10-13

### Added

- `target_sse_algorithm` (`AES256` | null, default null): the functions sent no `x-amz-server-side-encryption` header on the compacted output or the Distributed Map manifest, so a target bucket whose policy denies unencrypted uploads rejected every write even with default encryption on, because the bucket default encrypts the object without satisfying the header condition. When set, every write carries the header; null keeps the previous behaviour

## [0.3.3] - 2026-09-17

### Fixed

- Distributed Map child executions were still denied `sts:AssumeRole` under v0.3.1 and v0.3.2. Step Functions requests child-task credentials without the confused-deputy context keys, and an absent key fails a strict `StringEquals` / `ArnLike` condition regardless of the values listed, so the `mapRun:` pattern (v0.3.1) and the four-form list (v0.3.2) could never match. The trust policy now uses `StringEqualsIfExists` on `aws:SourceAccount`: enforced whenever the key is present, tolerant when the service omits it. The `aws:SourceArn` condition is removed for the same reason. Trust-policy update only, no resource replacement ([#16](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/16))

### Security

- The state-machine trust policy is looser than the strict conditions v0.3.0 introduced, which never worked for the Distributed Map path: a `states.amazonaws.com` assume-role call that carries no context passes. Cross-account confused-deputy attempts still deny via the account check, and the posture is stronger than the upstream aws-samples solution and CDK-generated roles, which attach no conditions. The residual same-account risk is the one every Distributed Map user accepts: a principal already holding `iam:PassRole` on this role plus `states:CreateStateMachine` can attach it to their own state machine ([#16](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/16))

## [0.3.2] - 2026-09-16

### Fixed

- Distributed Map child tasks were still denied `sts:AssumeRole` after v0.3.1. Children run as Express executions and present the `express:<state machine name>/<map label>:<uuid>:<uuid>` execution ARN as `aws:SourceArn`, which the trust-policy condition did not allow. The condition now allows all four ARN shapes Step Functions can present for this state machine (`stateMachine`, `execution`, `mapRun`, `express`), each still scoped to this state machine and account, verified with the IAM policy simulator including wrong-name and wrong-account probes ([#13](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/13))
- The role's `states:DescribeExecution` / `states:StopExecution` grant for child executions now names the `execution:` and `express:` ARN shapes those executions actually have, instead of `stateMachine:<name>:*`, which matched neither ([#13](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/13))

### Added

- `sfn_child_execution_type` (`EXPRESS` | `STANDARD`, default `EXPRESS`): the Distributed Map child execution type was hardcoded to `EXPRESS`, which caps each date prefix at 5 minutes total regardless of `compact_lambda_timeout`. `STANDARD` lifts that ceiling for large backlogs ([#13](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/13))
- `sfn_tolerated_failure_count` and `sfn_tolerated_failure_percentage`: let a Distributed Map run tolerate some failed dates instead of failing the whole execution on the first one, after other dates have already written output. Both unset by default, which keeps the Step Functions behaviour of zero tolerated failures ([#13](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/13))

### Changed

- The Lambda invoke retry in the state machine now also retries `Lambda.Unknown` and `States.Timeout`, so a date whose compaction times out is retried instead of failing the run. Safe since v0.3.0 made compaction idempotent ([#13](https://github.com/fivexl/terraform-aws-s3-small-object-compaction/issues/13))
- `lambda_ephemeral_storage_size` documents the sizing rule: a single date prefix's merged bytes are staged in `/tmp`, so the value must exceed the largest day under any one prefix

## [0.3.1] - 2026-09-16

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
