import boto3, json, time, os, sys, tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

REGIONS = os.environ.get("REGIONS", "eu-central-1,us-east-1,eu-west-1").split(",")

GLOBAL_TYPES = frozenset([
    "AWS::IAM::Role", "AWS::IAM::User", "AWS::IAM::Group",
    "AWS::IAM::ManagedPolicy", "AWS::IAM::InstanceProfile",
    "AWS::CloudFront::Distribution", "AWS::CloudFront::CachePolicy",
    "AWS::CloudFront::OriginRequestPolicy", "AWS::CloudFront::OriginAccessControl",
    "AWS::CloudFront::CloudFrontOriginAccessIdentity",
    "AWS::Route53::HostedZone", "AWS::Route53::RecordSet",
    "AWS::GlobalAccelerator::Accelerator", "AWS::GlobalAccelerator::Listener",
    "AWS::GlobalAccelerator::EndpointGroup",
    "AWS::S3::Bucket", "AWS::S3::BucketPolicy",
])
HOME_REGION = "us-east-1"
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(tempfile.gettempdir(), "cfn-templates"))

SCAN_POLL_INTERVAL_SEC = 30
SCAN_TIMEOUT_MINUTES = 120
MAX_CONCURRENT_TEMPLATES = int(os.environ.get("MAX_CONCURRENT_TEMPLATES", "5"))
TEMPLATE_POLL_DEADLINE_MINUTES = 30
TEMPLATE_POLL_INTERVAL_SEC = 10
CHUNK_SIZE = 499  # CreateGeneratedTemplate hard limit


def _aws_error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def discover_existing_scans(regions):
    result = {}
    for region in regions:
        client = boto3.client("cloudformation", region_name=region)
        try:
            resp = client.list_resource_scans()
            scans = [s for s in resp.get("ResourceScanSummaries", []) if s["Status"] == "COMPLETE"]
            result[region] = scans[0] if scans else None
        except Exception as e:
            print(f"  {region}: list_resource_scans failed: {e}")
            result[region] = None
    return result


def print_discovery(scans_by_region):
    now = datetime.now(timezone.utc)
    for region, scan in scans_by_region.items():
        if scan:
            delta = now - scan["EndTime"]
            if delta.days >= 1:
                age = f"{delta.days} day{'s' if delta.days != 1 else ''} old"
            else:
                hours = delta.seconds // 3600
                age = f"{hours} hour{'s' if hours != 1 else ''} old" if hours else "fresh"
            print(f"  {region}: {scan['EndTime'].strftime('%Y-%m-%d %H:%M UTC')} ({age})")
        else:
            print(f"  {region}: no COMPLETE scans found")


def start_and_wait_for_scan(region):
    client = boto3.client("cloudformation", region_name=region)
    try:
        resp = client.start_resource_scan()
        scan_id = resp["ResourceScanId"]
        print(f"  [{region}] scan started: ...{scan_id[-8:]}")
    except Exception as exc:
        code = _aws_error_code(exc)
        if "InProgress" in code:
            list_resp = client.list_resource_scans()
            in_progress = [s for s in list_resp.get("ResourceScanSummaries", [])
                           if s["Status"] == "IN_PROGRESS"]
            if not in_progress:
                raise RuntimeError(f"[{region}] start_resource_scan reported in-progress but none listed")
            scan_id = in_progress[0]["ResourceScanId"]
            print(f"  [{region}] scan already running, monitoring: ...{scan_id[-8:]}")
        elif "LimitExceeded" in code:
            raise RuntimeError(f"[{region}] resource scan limit exceeded (daily quota)")
        else:
            raise

    deadline = time.time() + SCAN_TIMEOUT_MINUTES * 60
    poll_count = 0
    while time.time() < deadline:
        time.sleep(SCAN_POLL_INTERVAL_SEC)
        poll_count += 1
        desc = client.describe_resource_scan(ResourceScanId=scan_id)
        status = desc["Status"]
        progress = desc.get("PercentageCompleted", 0)
        if poll_count % 5 == 0 or status in ("COMPLETE", "FAILED", "EXPIRED"):
            print(f"  [{region}] {status} ({progress:.0f}%)")
        if status == "COMPLETE":
            return scan_id
        if status in ("FAILED", "EXPIRED"):
            raise RuntimeError(f"[{region}] scan {status}: {desc.get('StatusReason', '')}")
    raise RuntimeError(f"[{region}] scan timeout after {SCAN_TIMEOUT_MINUTES} minutes")


def start_scans_parallel(regions):
    if not regions:
        return {}
    print(f"\nStarting parallel scans for: {', '.join(regions)}")
    print("(typical scan takes 10-30 min; polling every 30s)")
    scan_ids = {}
    with ThreadPoolExecutor(max_workers=max(1, len(regions))) as ex:
        futures = {ex.submit(start_and_wait_for_scan, r): r for r in regions}
        for fut in as_completed(futures):
            region = futures[fut]
            try:
                scan_ids[region] = fut.result()
            except Exception as e:
                print(f"  [{region}] scan failed: {e}")
                scan_ids[region] = None
    return scan_ids


def prompt_mode():
    print("\nWhat do you want to do?")
    print("  [e] Use existing scans only (skip regions without one)")
    print("  [n] Start new scans in all regions (10-30 min per region)")
    print("  [a] Auto: use existing where available, start new for missing only (default)")
    print("  [q] Quit")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("e", "existing"): return "existing"
        if choice in ("n", "new"): return "new"
        if choice in ("a", "auto", ""): return "auto"
        if choice in ("q", "quit"): return "quit"
        print("Invalid choice, try again (e/n/a/q)")


def resolve_scan_ids(regions):
    env_mode = os.environ.get("SCAN_MODE", "").lower()
    mode = env_mode if env_mode in ("existing", "new", "auto") else None

    print("Discovering existing scans...")
    existing = discover_existing_scans(regions)
    print_discovery(existing)

    if mode is None:
        if sys.stdin.isatty():
            mode = prompt_mode()
            if mode == "quit":
                print("Exiting.")
                sys.exit(0)
        else:
            mode = "auto"
            print("\nNon-interactive run, defaulting to mode: auto")
            print("(set SCAN_MODE=existing|new|auto to override)")

    print(f"\nMode: {mode}")

    if mode == "existing":
        skipped = [r for r, s in existing.items() if not s]
        if skipped:
            print(f"Warning: no scans in {skipped}, these regions will be skipped")
        return {r: s["ResourceScanId"] for r, s in existing.items() if s}

    if mode == "new":
        new_scans = start_scans_parallel(regions)
        result = {}
        for r in regions:
            if new_scans.get(r):
                result[r] = new_scans[r]
            elif existing.get(r):
                print(f"  [{r}] new scan failed, falling back to existing scan")
                result[r] = existing[r]["ResourceScanId"]
        return result

    result = {r: s["ResourceScanId"] for r, s in existing.items() if s}
    missing = [r for r in regions if r not in result]
    if missing:
        new_scans = start_scans_parallel(missing)
        for r, sid in new_scans.items():
            if sid:
                result[r] = sid
    return result


def list_all_resources(region, scan_id):
    client = boto3.client("cloudformation", region_name=region)
    resources = []
    kwargs = {"ResourceScanId": scan_id}
    while True:
        resp = client.list_resource_scan_resources(**kwargs)
        resources.extend(resp.get("Resources", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return resources


def to_cfn_resources(resources):
    seen = set()
    result = []
    for r in resources:
        key = (r["ResourceType"], json.dumps(r["ResourceIdentifier"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            result.append({
                "ResourceType": r["ResourceType"],
                "ResourceIdentifier": r["ResourceIdentifier"],
            })
    return result


def wait_for_deletion(cfn, name, timeout=90):
    for _ in range(timeout // 3):
        time.sleep(3)
        try:
            cfn.describe_generated_template(GeneratedTemplateName=name)
        except Exception:
            return
    print(f"  Warning: '{name}' may not have been fully deleted yet")


def discover_existing_templates(regions, name_prefix):
    result = {}
    for region in regions:
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            for page in cfn.get_paginator("list_generated_templates").paginate():
                for t in page.get("Summaries", []):
                    if t["GeneratedTemplateName"].startswith(name_prefix):
                        result.setdefault(region, {})[t["GeneratedTemplateName"]] = t["Status"]
        except Exception as e:
            print(f"  {region}: list_generated_templates failed, no resume: {e}")
    return result


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # Stage 1: Resolve scan IDs
    scan_ids = resolve_scan_ids(REGIONS)
    if not scan_ids:
        print("\nNo usable scans available, exiting.")
        sys.exit(1)
    print(f"\nUsing scans for {len(scan_ids)} region(s): {list(scan_ids.keys())}")

    # Stage 2: Fetch all resources
    print("\nFetching resources...")
    all_resources = {}
    for region, scan_id in list(scan_ids.items()):
        try:
            all_resources[region] = list_all_resources(region, scan_id)
            print(f"  {region}: {len(all_resources[region])} resources")
        except Exception as e:
            print(f"  {region}: fetch failed, dropping region: {e}")
            scan_ids.pop(region)

    if not all_resources:
        print("\nNo regions returned resources, exiting.")
        sys.exit(1)

    # Stage 3: Split global vs regional
    global_resources = []
    regional_resources = defaultdict(list)
    for region, resources in all_resources.items():
        for r in resources:
            if r["ResourceType"] in GLOBAL_TYPES:
                if region == HOME_REGION:
                    global_resources.append(r)
            else:
                regional_resources[region].append(r)

    print(f"\nGlobal   template : {len(global_resources)} resources")
    for region in scan_ids:
        print(f"Regional ({region}): {len(regional_resources[region])} resources")

    # Stage 4: Build templates_to_create
    templates_to_create = {}
    if HOME_REGION in scan_ids:
        templates_to_create["global"] = (HOME_REGION, global_resources)
    elif global_resources:
        print(f"\nWarning: global resources found but HOME_REGION ({HOME_REGION}) was not scanned, dropping global template")
    for region in scan_ids:
        templates_to_create[region] = (region, regional_resources[region])

    # Stage 5: Build create queue (resume-aware)
    TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d")
    existing_per_region = discover_existing_templates(scan_ids.keys(), "iac-gen-")
    create_queue = []
    template_arns = {}
    in_flight = {}
    failed_templates = set()
    resumed_complete = 0
    resumed_in_flight = 0
    resumed_failed = 0

    for name, (region, resources) in templates_to_create.items():
        if not resources:
            print(f"Skipping {name}, no resources")
            continue
        cfn_resources = to_cfn_resources(resources)
        chunks = [cfn_resources[i:i + CHUNK_SIZE] for i in range(0, len(cfn_resources), CHUNK_SIZE)]
        cfn_name = f"iac-gen-{name}-{TIMESTAMP}"
        for idx, chunk in enumerate(chunks):
            chunk_name = cfn_name if len(chunks) == 1 else f"{cfn_name}-part{idx + 1}"
            existing_status = existing_per_region.get(region, {}).get(chunk_name)
            if existing_status == "COMPLETE":
                template_arns[chunk_name] = (region, None)
                resumed_complete += 1
            elif existing_status in ("CREATE_IN_PROGRESS", "CREATE_PENDING",
                                     "UPDATE_IN_PROGRESS", "UPDATE_PENDING"):
                template_arns[chunk_name] = (region, None)
                in_flight[chunk_name] = (region, time.time() + TEMPLATE_POLL_DEADLINE_MINUTES * 60)
                resumed_in_flight += 1
            elif existing_status == "FAILED":
                failed_templates.add(chunk_name)
                resumed_failed += 1
            else:
                create_queue.append((chunk_name, region, chunk))

    if resumed_complete or resumed_in_flight or resumed_failed:
        print(f"\nResume: {resumed_complete} already complete, "
              f"{resumed_in_flight} still in flight, "
              f"{resumed_failed} previously failed (skipped)")

    # Stage 6: Create + poll with backpressure
    print(f"\nCreating + polling {len(create_queue)} new chunk(s) "
          f"(max {MAX_CONCURRENT_TEMPLATES} in flight, "
          f"{TEMPLATE_POLL_DEADLINE_MINUTES} min per-template deadline)")
    last_status_print = 0
    while create_queue or in_flight:
        for chunk_name, (region, deadline) in list(in_flight.items()):
            if time.time() > deadline:
                print(f"  [{chunk_name}] poll timeout, dropping")
                failed_templates.add(chunk_name)
                in_flight.pop(chunk_name)
                continue
            try:
                cfn = boto3.client("cloudformation", region_name=region)
                status = cfn.describe_generated_template(GeneratedTemplateName=chunk_name)["Status"]
                if status == "COMPLETE":
                    in_flight.pop(chunk_name)
                elif status == "FAILED":
                    print(f"  [{chunk_name}] generation FAILED")
                    failed_templates.add(chunk_name)
                    in_flight.pop(chunk_name)
            except Exception as e:
                print(f"  [{chunk_name}] describe failed, will retry: {e}")

        while create_queue and len(in_flight) < MAX_CONCURRENT_TEMPLATES:
            chunk_name, region, chunk = create_queue.pop(0)
            cfn = boto3.client("cloudformation", region_name=region)

            try:
                cfn.delete_generated_template(GeneratedTemplateName=chunk_name)
                wait_for_deletion(cfn, chunk_name)
            except Exception:
                pass

            try:
                resp = cfn.create_generated_template(
                    GeneratedTemplateName=chunk_name,
                    Resources=chunk,
                )
                template_arns[chunk_name] = (region, resp["GeneratedTemplateId"])
                in_flight[chunk_name] = (region, time.time() + TEMPLATE_POLL_DEADLINE_MINUTES * 60)
                print(f"  [{chunk_name}] create submitted ({len(chunk)} resources) "
                      f"[{len(in_flight)}/{MAX_CONCURRENT_TEMPLATES} in flight, {len(create_queue)} queued]")
            except Exception as e:
                err_str = str(e)
                if "ExpiredToken" in err_str or "security token" in err_str.lower():
                    print(f"  [{chunk_name}] AWS session token expired. "
                          f"Refresh credentials and re-run with SCAN_MODE=auto; remaining work will be re-queued.")
                    failed_templates.add(chunk_name)
                    for queued_name, _, _ in create_queue:
                        failed_templates.add(queued_name)
                    create_queue.clear()
                    break
                print(f"  [{chunk_name}] create failed, skipping: {e}")
                failed_templates.add(chunk_name)

        now = time.time()
        if now - last_status_print > 60:
            succeeded = (
                len(template_arns)
                - len(in_flight)
                - len(failed_templates & template_arns.keys())
            )
            print(f"  status: {len(in_flight)} in flight, {len(create_queue)} queued, "
                  f"{succeeded} succeeded, {len(failed_templates)} failed")
            last_status_print = now

        if in_flight or create_queue:
            time.sleep(TEMPLATE_POLL_INTERVAL_SEC)
    print("All templates settled.")

    # Stage 7: Download
    print("\nDownloading templates...")
    for name, (region, _) in template_arns.items():
        if name in failed_templates:
            print(f"  [skip] {name}: template generation failed or timed out")
            continue
        try:
            cfn = boto3.client("cloudformation", region_name=region)
            resp = cfn.get_generated_template(GeneratedTemplateName=name, Format="YAML")
            status = resp.get("Status")
            body = resp.get("TemplateBody", "")
            path = os.path.join(OUT_DIR, f"{name}.yaml")
            with open(path, "w") as f:
                f.write(body)
            flag = "[PARTIAL]" if status == "FAILED" else "[ok]"
            print(f"  {flag} {path}  ({len(body):,} bytes)")
        except Exception as e:
            print(f"  [fail] {name}: download failed: {e}")

    print(f"\nDone. Templates saved to: {OUT_DIR}")


if __name__ == "__main__":
    main()
