# cfn-extractor

A Python script that orchestrates AWS CloudFormation's IaC Generator service across multiple regions: starts (or reuses) resource scans, waits for them to complete, then downloads one YAML template per region. Useful when you want a snapshot of "everything in this account, as CloudFormation" without clicking through the console four times per region.

What you get at the end is one YAML file per (region, chunk): one global template for IAM/CloudFront/Route53/S3, plus one regional template per AWS region you list. Each is a valid CloudFormation template you can import into a stack, version-control, or diff against last week's snapshot.

## Why this exists

The AWS console's IaC Generator does the same job, but only one template at a time, with a UI that times out on large accounts. This script:

- Discovers existing `COMPLETE` scans per region, or starts new ones for you (parallel across regions).
- Deduplicates resources across regions for global types (IAM lives in `us-east-1` only).
- Chunks resources into batches of 499 (the `CreateGeneratedTemplate` hard limit).
- Polls until every template is generated, then downloads the YAML in one pass.

You go from "I need an audit-ready snapshot" to "I have eight YAML files on my disk" in about ten minutes (using existing scans) or 30-50 minutes (starting fresh scans).

## What you need

1. **AWS CLI credentials** for the account you want to scan, with the permissions in [`iam-policy.json`](iam-policy.json). Admin-equivalent is easiest.
2. **boto3**: `pip install -r requirements.txt`.

You do not need to pre-start a resource scan in the AWS console. The script can start one for you if none exists.

## Run

```bash
python3 gen_templates.py
```

On first run with no existing scans, an interactive prompt appears:

```
Discovering existing scans...
  eu-central-1: no COMPLETE scans found
  us-east-1: no COMPLETE scans found
  eu-west-1: no COMPLETE scans found

What do you want to do?
  [e] Use existing scans only (skip regions without one)
  [n] Start new scans in all regions (10-30 min per region)
  [a] Auto: use existing where available, start new for missing only (default)
  [q] Quit
>
```

In a CI/cron context (no TTY), the script defaults to `auto`. Override with `SCAN_MODE`.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `REGIONS` | `eu-central-1,us-east-1,eu-west-1` | Comma-separated regions to scan |
| `OUT_DIR` | `<system temp>/cfn-templates` | Where the YAML files are written |
| `SCAN_MODE` | (prompt, or `auto` non-interactively) | `existing` (use only existing scans, skip missing regions), `new` (start fresh scans everywhere), `auto` (existing where available, new for missing) |
| `MAX_CONCURRENT_TEMPLATES` | `5` | How many `CreateGeneratedTemplate` calls can be in flight at once. AWS's per-account soft limit is around 25, but real-world throttling kicks in earlier when other tooling shares the account. The script keeps at most this many in flight and tops up as others finish. Bump it on idle accounts; lower it (e.g. to `3`) if you still hit `ConcurrentResourcesLimitExceeded`. |

## How modes behave

- **existing**: Lists scans in every region. Uses the latest `COMPLETE` one. Regions without a scan are skipped with a warning. Closest to the previous version's behavior, but no longer crashes when a region has no scan.
- **new**: Starts a new scan in every region in parallel. Polls each every 30 seconds until `COMPLETE` (or fails after 2 hours). If `StartResourceScan` returns the daily limit error, that region falls back to its latest existing scan (if any).
- **auto**: For each region, uses the existing latest scan if one exists. For regions with no existing scan, starts a new one.

If a scan is already running in a region (e.g., someone clicked the console button), `start_and_wait_for_scan` picks up that one instead of failing.

## Resume

If a run fails halfway (expired credentials, network drop, AWS throttling), just re-run. Same-day re-runs detect templates already on AWS by name and skip the work that's done:

- `COMPLETE` templates are downloaded as-is, no re-generation
- `CREATE_IN_PROGRESS` templates are monitored from where they are
- `FAILED` templates are skipped (re-submitting the same input usually fails the same way)
- Everything else is queued for fresh creation

Cross-day resume is not supported (template names include today's date). Refresh credentials and re-run before midnight UTC if you have a partial result you want to finish.

## Output sample (auto mode, mixed)

```
Discovering existing scans...
  eu-central-1: 2026-02-27 14:23 UTC (3 days old)
  us-east-1: no COMPLETE scans found
  eu-west-1: 2026-02-27 13:58 UTC (3 days old)

Mode: auto

Starting parallel scans for: us-east-1
(typical scan takes 10-30 min; polling every 30s)
  [us-east-1] scan started: ...a1b2c3d4
  [us-east-1] IN_PROGRESS (32%)
  [us-east-1] IN_PROGRESS (78%)
  [us-east-1] COMPLETE (100%)

Using scans for 3 region(s): ['eu-central-1', 'eu-west-1', 'us-east-1']

Fetching resources...
  eu-central-1: 1432 resources
  eu-west-1: 47 resources
  us-east-1: 612 resources

Global   template : 89 resources
Regional (eu-central-1): 1418 resources
Regional (eu-west-1): 47 resources
Regional (us-east-1): 523 resources

Creating templates in AWS...
  Creating 'iac-gen-global-20260528' (89 resources)...
  Creating 'iac-gen-eu-central-1-20260528-part1' (499 resources)...
  ...

Waiting for templates to generate.......... done

Downloading templates...
  [ok] /tmp/cfn-templates/iac-gen-global-20260528.yaml  (412,891 bytes)
  ...

Done. Templates saved to: /tmp/cfn-templates
```

## Output handling

The generated templates contain real resource IDs from your account (access key IDs, ARNs with account numbers, distribution IDs). Treat them like CloudTrail logs:

- Do not commit raw output to a public repository. The included `.gitignore` already excludes `cfn-templates/` and `cfn-templates.zip`.
- If you want to track drift over time, store snapshots in a private S3 bucket with versioning + SSE, not in git.
- Diff with `diff -u snapshot-old.yaml snapshot-new.yaml | less` for change review.

## How the deduplication works

`AWS::IAM::Role` and friends are global, so they show up in every region's resource scan. The script keeps them only from `us-east-1` (the `HOME_REGION` constant) and drops them from the regional buckets. Edit `GLOBAL_TYPES` in `gen_templates.py` if a future global type needs the same treatment.

The chunk size is 499 because `CreateGeneratedTemplate` rejects 500+. The script also deletes any existing generated template with the same name before re-creating, so re-running on the same day overwrites yesterday's run cleanly.

If `HOME_REGION` is not in `REGIONS`, the script drops the global template entirely with a warning (because there is no scan to source IAM/CloudFront/etc. from).

## Things to keep in mind

Resource scans cost money. AWS charges per resource scanned; check pricing before pointing this at a 50k-resource account daily.

`StartResourceScan` is rate-limited to a small number of scans per 24 hours per region. When it returns `ResourceScanLimitExceeded`, `new` mode falls back to the latest existing scan if there is one, otherwise the region is skipped.

Typical scan takes 10 to 30 minutes. Large accounts (>10k resources) can push 1 hour. The script's timeout is 2 hours per region.

If one resource in a chunk fails to template-ize (rare, usually custom resources or unusual states), `get_generated_template` returns `Status=FAILED` and a partial body. The script writes it anyway with a `[PARTIAL]` flag so you can grep for them.

No retry on transient AWS API failures. A `cloudformation:*` 5xx aborts the run, but re-running picks up from the latest scan, so it's a nuisance, not a data loss.

`OUT_DIR` is local disk. Large accounts produce multi-megabyte YAML files; make sure the directory has room.

The downloaded YAML is the raw template AWS generates. Access key IDs, account numbers, resource ARNs, real config: it's all there. Sanitize before sharing.

## Files

| File | What it is |
|---|---|
| `gen_templates.py` | The script |
| `iam-policy.json` | Minimal IAM policy for the runner |
| `requirements.txt` | `boto3` |

## License

Released under MIT ([LICENSE](LICENSE)).
