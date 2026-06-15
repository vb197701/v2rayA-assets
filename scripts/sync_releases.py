import json
import os
import subprocess
import sys
import time
from pathlib import Path

UPSTREAM = os.environ.get("UPSTREAM_REPO")
TARGET = os.environ.get("TARGET_REPO", os.environ.get("TARGET"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "5"))
INCREMENTAL_SYNC = os.environ.get("INCREMENTAL_SYNC", "true").lower() == "true"
SYNC_ORDER = os.environ.get("SYNC_ORDER", "oldest_to_newest")
RATE_LIMIT_RETRY = int(os.environ.get("RATE_LIMIT_RETRY", "3"))
SLEEP_ON_RATE_LIMIT = int(os.environ.get("SLEEP_ON_RATE_LIMIT", "60"))

STATE_FILE = Path("/tmp/sync-state.json")


def sh(cmd, check=True, max_retries=RATE_LIMIT_RETRY):
    last_result = None
    for i in range(max_retries):
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        last_result = result

        if result.returncode == 0:
            return result

        stderr = result.stderr or ""
        if "403" in stderr and "rate limit" in stderr.lower():
            wait_time = SLEEP_ON_RATE_LIMIT * (i + 1)
            print(f"⚠ Rate limit hit, waiting {wait_time}s... (attempt {i+1}/{max_retries})")
            time.sleep(wait_time)
            continue

        if check:
            print(f"✗ Command failed: {cmd}")
            print(f"stderr: {stderr}")
        return result

    if check and last_result is not None:
        print(f"✗ Command failed after {max_retries} retries: {cmd}")
        print(f"stderr: {last_result.stderr}")
    return last_result


def is_valid_tag(tag: str) -> bool:
    if not tag:
        return False
    if " " in tag:
        return False
    return True


def get_releases(repo):
    r = sh(
        f'gh release list -R "{repo}" --limit 500 --order asc --json tagName,name,publishedAt,isDraft,isPrerelease',
        check=False
    )
    if r.returncode != 0:
        print(f"Failed to get releases: {r.stderr}")
        return []

    data = json.loads(r.stdout)
    releases = []
    for item in data:
        if item.get("isDraft"):
            continue
        releases.append({
            "tag_name": item.get("tagName"),
            "name": item.get("name") or item.get("tagName"),
            "published_at": item.get("publishedAt"),
            "is_prerelease": item.get("isPrerelease", False),
        })

    return releases


def get_release_detail(repo, release_tag):
    r = sh(
        f'gh release view "{release_tag}" -R "{repo}" --json tagName,name,body,assets,publishedAt,isPrerelease',
        check=False
    )
    if r.returncode != 0:
        print(f"Failed to get release detail for {release_tag}: {r.stderr}")
        return None
    return json.loads(r.stdout)


def release_exists(tag, repo):
    r = sh(f'gh release view "{tag}" -R "{repo}" >/dev/null 2>&1', check=False)
    return r.returncode == 0


def create_release(tag, repo, name, body, prerelease=False, draft=False):
    prerelease_flag = "--prerelease" if prerelease else ""
    draft_flag = "--draft" if draft else ""

    cmd = f'''gh release create "{tag}" -R "{repo}" \
--title "{name}" \
--notes "{body}" \
{prerelease_flag} \
{draft_flag}'''
    r = sh(cmd, check=False)
    if r.returncode == 0:
        print(f"✓ Created release {tag}")
        return True
    else:
        print(f"✗ Failed to create release {tag}: {r.stderr}")
        return False


def get_release_assets(repo, release_tag):
    r = sh(f'gh release view "{release_tag}" -R "{repo}" --json assets', check=False)
    if r.returncode != 0:
        print(f"Failed to get assets for {release_tag}: {r.stderr}")
        return []

    data = json.loads(r.stdout)
    assets = data.get("assets", [])

    print(f"Found {len(assets)} assets in upstream release {release_tag}")
    for a in assets:
        print(f" - {a['name']} ({a['size'] // 1024} KB)")

    return assets


def release_has_missing_assets(release_tag, repo):
    r = sh(f'gh release view "{release_tag}" -R "{UPSTREAM}" --json assets', check=False)
    if r.returncode != 0:
        return False

    upstream_assets = json.loads(r.stdout).get("assets", [])
    if not upstream_assets:
        return False

    r = sh(f'gh release view "{release_tag}" -R "{repo}" --json assets', check=False)
    if r.returncode != 0:
        return False

    target_assets = json.loads(r.stdout).get("assets", [])
    target_asset_names = {a["name"] for a in target_assets}

    missing = [a["name"] for a in upstream_assets if a["name"] not in target_asset_names]
    if missing:
        print(f" Release {release_tag} missing {len(missing)} assets: {missing}")
        return True

    return False


def sync_assets(release_tag, repo):
    work = Path("/tmp/release-sync")
    work.mkdir(parents=True, exist_ok=True)

    for f in work.glob("*"):
        try:
            f.unlink()
        except:
            pass

    upstream_assets = get_release_assets(repo, release_tag)
    if not upstream_assets:
        print(f"No assets found for release {release_tag}")
        return False

    print(f"Downloading assets from {repo}...")
    downloaded = []
    for asset in upstream_assets:
        name = asset["name"]
        asset_path = work / name

        if asset_path.exists():
            print(f" - {name} already exists in work dir, skipping download")
            downloaded.append(asset_path)
            continue

        cmd = f'gh release download "{release_tag}" -R "{repo}" -p "{name}" -D "{work}"'
        r = sh(cmd, check=False)

        if r.returncode == 0 and asset_path.exists():
            print(f" ✓ Downloaded {name}")
            downloaded.append(asset_path)
        else:
            print(f" ✗ Failed to download {name}: {r.stderr}")

    if not downloaded:
        print("No assets downloaded")
        return False

    print(f"Downloaded {len(downloaded)} assets")

    print(f"Uploading assets to {TARGET}...")
    uploaded = 0
    for asset_path in downloaded:
        name = asset_path.name

        r = sh(f'gh release view "{release_tag}" -R "{TARGET}" --json assets', check=False)
        if r.returncode == 0:
            target_assets = json.loads(r.stdout).get("assets", [])
            target_asset_names = {a["name"] for a in target_assets}
            if name in target_asset_names:
                print(f" - {name} already exists in target, skipping upload")
                uploaded += 1
                continue

        cmd = f'gh release upload "{release_tag}" "{asset_path}" -R "{TARGET}" --clobber'
        r = sh(cmd, check=False)

        if r.returncode == 0:
            print(f" ✓ Uploaded {name}")
            uploaded += 1
        else:
            print(f" ✗ Failed to upload {name}: {r.stderr}")

    print(f"Uploaded {uploaded}/{len(downloaded)} assets")
    return uploaded > 0


def sync_assets_to_existing_release(release_tag):
    work = Path("/tmp/release-sync")
    work.mkdir(parents=True, exist_ok=True)

    for f in work.glob("*"):
        try:
            f.unlink()
        except:
            pass

    r = sh(f'gh release view "{release_tag}" -R "{UPSTREAM}" --json assets', check=False)
    if r.returncode != 0:
        print(f"Failed to get upstream assets for {release_tag}")
        return False

    upstream_assets = json.loads(r.stdout).get("assets", [])
    if not upstream_assets:
        print(f"No upstream assets for {release_tag}")
        return False

    r = sh(f'gh release view "{release_tag}" -R "{TARGET}" --json assets', check=False)
    if r.returncode != 0:
        print(f"Failed to get target assets for {release_tag}")
        return False

    target_assets = json.loads(r.stdout).get("assets", [])
    target_asset_names = {a["name"] for a in target_assets}

    missing_count = 0
    uploaded_count = 0
    for asset in upstream_assets:
        name = asset["name"]
        if name in target_asset_names:
            print(f" - {name} already exists in target, skipping")
            continue

        missing_count += 1
        asset_path = work / name

        print(f" Missing asset: {name}, downloading...")
        r = sh(f'gh release download "{release_tag}" -R "{UPSTREAM}" -p "{name}" -D "{work}"', check=False)
        if r.returncode != 0 or not asset_path.exists():
            print(f" ✗ Failed to download {name}: {r.stderr}")
            continue

        print(f" ✓ Downloaded {name}")
        cmd = f'gh release upload "{release_tag}" "{asset_path}" -R "{TARGET}" --clobber'
        r = sh(cmd, check=False)
        if r.returncode == 0:
            print(f" ✓ Uploaded missing asset {name}")
            uploaded_count += 1
        else:
            print(f" ✗ Failed to upload {name}: {r.stderr}")

    if missing_count > 0:
        print(f" Synced {uploaded_count}/{missing_count} missing assets to {release_tag}")

    return uploaded_count > 0


def main():
    if not UPSTREAM or not TARGET:
        print("UPSTREAM_REPO or TARGET_REPO/TARGET is not set")
        sys.exit(1)

    print(f"Syncing releases from {UPSTREAM} to {TARGET}")
    print("=" * 60)
    print("Configuration:")
    print(f" - UPSTREAM_REPO: {UPSTREAM}")
    print(f" - TARGET_REPO: {TARGET}")
    print(f" - MAX_WORKERS: {MAX_WORKERS}")
    print(f" - INCREMENTAL_SYNC: {INCREMENTAL_SYNC}")
    print(f" - SYNC_ORDER: {SYNC_ORDER}")
    print(f" - RATE_LIMIT_RETRY: {RATE_LIMIT_RETRY}")
    print(f" - SLEEP_ON_RATE_LIMIT: {SLEEP_ON_RATE_LIMIT}s")
    print("=" * 60)

    upstream = get_releases(UPSTREAM)
    print(f"Found {len(upstream)} upstream releases")

    if not upstream:
        print("No upstream releases found")
        sys.exit(1)

    if SYNC_ORDER == "oldest_to_newest":
        upstream = list(reversed(upstream))
        print("Sorted releases from oldest to newest:")
        print(f" First: {upstream[0]['tag_name']}")
        print(f" Last: {upstream[-1]['tag_name']}")
    elif SYNC_ORDER == "newest_to_oldest":
        print("Sorted releases from newest to oldest:")
        print(f" First: {upstream[0]['tag_name']}")
        print(f" Last: {upstream[-1]['tag_name']}")
    else:
        print(f"Unknown SYNC_ORDER: {SYNC_ORDER}, using oldest_to_newest")
        upstream = list(reversed(upstream))

    if INCREMENTAL_SYNC and STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            last_published_at = data.get("last_sync_published_at")
        except Exception as e:
            print(f"⚠ Failed to read sync state: {e}")
            last_published_at = None

        if last_published_at:
            upstream = [
                r for r in upstream
                if r.get("published_at") and r["published_at"] > last_published_at
            ]
            print(f"Incremental sync: skipping releases before {last_published_at}")
            print(f" Remaining: {len(upstream)} releases")

    success_count = 0
    assets_synced_count = 0
    last_synced_release = None

    for idx, rel in enumerate(upstream, 1):
        tag = rel["tag_name"]

        if not is_valid_tag(tag):
            print(f"⚠ Skipping release with invalid tag: '{tag}'")
            continue

        detail = get_release_detail(UPSTREAM, tag)
        if not detail:
            print(f"⚠ Skipping release {tag} because detail fetch failed")
            continue

        name = detail.get("name") or rel.get("name") or tag
        body = detail.get("body") or f"Synced from upstream {UPSTREAM}"
        published_at = detail.get("publishedAt") or rel.get("published_at")
        is_prerelease = detail.get("isPrerelease", False)

        print(f"\n[{idx}/{len(upstream)}] Processing release {tag}")

        if release_exists(tag, TARGET):
            if release_has_missing_assets(tag, TARGET):
                print(f" Syncing missing assets to existing release {tag}")
                if sync_assets_to_existing_release(tag):
                    print(f" ✓ Synced assets to {tag}")
                    assets_synced_count += 1
            else:
                print(f" Release {tag} already exists with all assets, skipping")
            last_synced_release = {
                "tag_name": tag,
                "published_at": published_at,
            }
            continue

        if create_release(tag, TARGET, name, body, prerelease=is_prerelease):
            if sync_assets(tag, UPSTREAM):
                success_count += 1
            last_synced_release = {
                "tag_name": tag,
                "published_at": published_at,
            }

    if INCREMENTAL_SYNC and last_synced_release and last_synced_release.get("published_at"):
        STATE_FILE.write_text(
            json.dumps({
                "last_sync_tag": last_synced_release["tag_name"],
                "last_sync_published_at": last_synced_release["published_at"],
            }),
            encoding="utf-8"
        )
        print(f"\nSaved sync state: {last_synced_release}")

    print("\n" + "=" * 60)
    print("Sync complete:")
    print(f" - Created {success_count}/{len(upstream)} new releases with assets")
    print(f" - Synced assets to {assets_synced_count}/{len(upstream)} existing releases")


if __name__ == "__main__":
    main()
