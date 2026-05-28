# Changelog

## [0.2.0] - 2026-05-28

- Added `SCAN_MODE` env var with three modes: `existing` (use only existing scans, skip missing regions), `new` (start fresh scans in every region in parallel), `auto` (existing where available, new for missing).
- Interactive prompt on a TTY when `SCAN_MODE` is unset; non-interactive runs default to `auto`.
- Parallel scan start + polling via `ThreadPoolExecutor`; each scan polled every 30 seconds with a 2-hour timeout per region.
- Graceful fallback in `new` mode: if `StartResourceScan` hits the daily limit, the region falls back to its latest existing scan (or is skipped if none exists).
- Picks up an already-running scan instead of failing when `start_resource_scan` reports one in progress.
- `templates_to_create` is now built dynamically from `REGIONS` (was previously hardcoded to `eu-central-1`, `us-east-1`, `eu-west-1`, silently dropping resources from any other region).
- Warns and drops the global template if `HOME_REGION` (`us-east-1`) is not in `REGIONS`.
- IAM policy adds `cloudformation:StartResourceScan`.
- Refactored module to wrap CLI execution in `main()` + `if __name__ == "__main__":` so the file can be imported in tests without triggering AWS calls.
- ASCII status icons in output (`[ok]`, `[PARTIAL]`) instead of Unicode glyphs.

## [0.1.0] - 2026-05-28

Initial public release.

- Multi-region orchestration of AWS CloudFormation IaC Generator: auto-detects latest `COMPLETE` resource scan per region, creates generated templates, polls until ready, downloads YAML.
- Deduplicates global resource types (IAM, CloudFront, Route 53, GlobalAccelerator, S3 bucket) so they appear only in the home-region (`us-east-1`) template.
- Chunks resources into batches of 499 to respect the `CreateGeneratedTemplate` hard limit.
- Idempotent re-runs: deletes existing generated templates with the same name before recreating.
- `REGIONS` and `OUT_DIR` are configurable via environment variables; safe cross-platform default for `OUT_DIR` via `tempfile.gettempdir()`.
- Minimal IAM policy provided.
