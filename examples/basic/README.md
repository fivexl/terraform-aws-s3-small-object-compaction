# Basic example

Creates a source and a target bucket, then deploys both compaction variants
against them. The EventBridge schedules are left disabled; trigger a run
manually with the payload documented in the module README, then set
`schedule_enabled = true`.

```bash
terraform init
terraform apply
```
