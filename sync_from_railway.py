"""
Pull edits made directly on the deployed (Railway) dashboard back down into
local. The reviewer edits products live on the deployed site -- that never
flows back to local on its own, since sync_to_railway.py only ever pushes
local -> Railway.

IMPORTANT: product.id and media.id are NOT reliable for matching rows
between the two databases. Both sides independently INSERT new product/
media rows over time (local's own extraction pipeline on one side,
sync_to_railway.py's merges on the other), so their autoincrement counters
drift apart -- the same numeric id can end up pointing at two completely
unrelated products on each side. instagram_post.id DOES stay aligned for
shared posts, because posts are only ever matched and inserted by
shortcode (globally unique), never renumbered. So everything here matches
through the post: live post -> shortcode -> local post -> local product/
media for that post_id. Matching by raw id was tried once and silently
corrupted an unrelated product; don't reintroduce that.

Only touches product rows for posts that already exist in both databases,
and only when Railway's review-editable fields actually differ from
local's -- Railway is authoritative for those since that's where editing
happens. Media rows are matched by (post_id, position), not id. Also
pulls down any reviewer-uploaded photo (added_by_reviewer=1) that exists
on Railway but not locally yet, downloading the actual image file.

Usage:
    python sync_from_railway.py            # pull and apply
    python sync_from_railway.py --dry-run  # report what would change, apply nothing
"""
import argparse
import sqlite3
import subprocess
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LOCAL_DB = DATA_DIR / "catalog.db"
TMP_LIVE = Path("/tmp/railway_sync_live.db")
SSH_ARGS = [
    "railway", "ssh", "-s", "dashboard",
    "-i", str(Path.home() / ".ssh" / "id_ed25519_railway_kevinbrown"),
]

# Exactly the columns dashboard.py's product_save() route writes -- nothing
# else. Fields like raw_extraction_json, confidence_json, dedup_score,
# brand, description, duplicate_of etc. are pipeline-owned and keep
# changing locally after a row is pushed to Railway (re-extraction, dedup
# re-scoring); comparing those would flag products as "edited" when it's
# really just local having moved on, and pulling Railway's stale snapshot
# of them down would regress local's newer data.
PRODUCT_EDITABLE_COLS = [
    "product_name", "category", "price", "sizes", "colors",
    "availability_status", "is_product_post", "review_required",
]


def pull_live(dest: Path):
    with open(dest, "wb") as f:
        subprocess.run(SSH_ARGS + ["--", "cat /data/catalog.db"], stdout=f, check=True)


def download_file(remote_path: str, local_path: Path):
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "wb") as f:
        subprocess.run(SSH_ARGS + ["--", f"cat '{remote_path}'"], stdout=f, check=True)


def sync_down(live_path: Path, dry_run: bool):
    local = sqlite3.connect(LOCAL_DB)
    live = sqlite3.connect(live_path)
    local.row_factory = sqlite3.Row
    live.row_factory = sqlite3.Row

    # shortcode -> local post_id, for every post local knows about
    local_post_by_shortcode = {
        r["shortcode"]: r["id"] for r in local.execute("SELECT id, shortcode FROM instagram_post")
    }

    updated_products = []
    excluded_flips = []
    new_media_files = []

    for live_post in live.execute("SELECT id, shortcode FROM instagram_post"):
        local_post_id = local_post_by_shortcode.get(live_post["shortcode"])
        if local_post_id is None:
            continue  # post doesn't exist locally at all yet -- not our concern here

        live_prod = live.execute("SELECT * FROM product WHERE post_id = ?", (live_post["id"],)).fetchone()
        local_prod = local.execute("SELECT * FROM product WHERE post_id = ?", (local_post_id,)).fetchone()
        if live_prod is not None and local_prod is not None:
            if any(live_prod[c] != local_prod[c] for c in PRODUCT_EDITABLE_COLS):
                updated_products.append((live_post["shortcode"], local_prod["id"]))
                if not dry_run:
                    local.execute(
                        f"""UPDATE product SET {", ".join(f"{c} = ?" for c in PRODUCT_EDITABLE_COLS)}
                           WHERE id = ?""",
                        tuple(live_prod[c] for c in PRODUCT_EDITABLE_COLS) + (local_prod["id"],),
                    )

        local_media_by_position = {
            r["position"]: r for r in local.execute(
                "SELECT id, position, excluded FROM media WHERE post_id = ?", (local_post_id,)
            )
        }
        for live_media in live.execute("SELECT * FROM media WHERE post_id = ?", (live_post["id"],)):
            lo = local_media_by_position.get(live_media["position"])
            if lo is not None:
                if lo["excluded"] != live_media["excluded"]:
                    excluded_flips.append((live_post["shortcode"], lo["id"]))
                    if not dry_run:
                        local.execute("UPDATE media SET excluded = ? WHERE id = ?", (live_media["excluded"], lo["id"]))
            elif live_media["added_by_reviewer"]:
                entry = dict(live_media)
                entry["local_post_id"] = local_post_id
                new_media_files.append(entry)
                if not dry_run:
                    local.execute(
                        """INSERT INTO media (post_id, position, local_path, ocr_text,
                             vision_description, phash, excluded, added_by_reviewer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (local_post_id, live_media["position"], live_media["local_path"], live_media["ocr_text"],
                         live_media["vision_description"], live_media["phash"], live_media["excluded"],
                         live_media["added_by_reviewer"]),
                    )

    if not dry_run:
        local.commit()
    local.close()
    live.close()
    return updated_products, excluded_flips, new_media_files


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("pulling live catalog.db from Railway...")
    pull_live(TMP_LIVE)

    updated_products, excluded_flips, new_media_files = sync_down(TMP_LIVE, args.dry_run)
    print(f"product edits to pull down ({len(updated_products)}): {updated_products}")
    print(f"media excluded-flag changes to pull down ({len(excluded_flips)}): {excluded_flips}")
    print(f"reviewer-uploaded photos to pull down: {len(new_media_files)}")

    if args.dry_run:
        print("dry run -- nothing applied")
    else:
        for m in new_media_files:
            remote_path = m["local_path"]
            marker = "data/"
            idx = remote_path.replace("\\", "/").rfind(marker)
            rel = remote_path[idx + len(marker):] if idx != -1 else remote_path.lstrip("/")
            dest = DATA_DIR / rel
            print(f"  downloading {remote_path} -> {dest}")
            download_file(remote_path, dest)
        print("done -- local catalog.db and image files updated")
