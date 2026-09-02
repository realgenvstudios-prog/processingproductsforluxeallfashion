"""
One-way, insert-only merge: copies any Instagram posts (and their
product/media rows) that exist in the local catalog.db but not in the
Railway copy, into a working copy of the Railway DB -- without ever
touching a row that already exists there.

This replaces a raw `cat catalog.db | ssh ... > /data/catalog.db` full-file
overwrite, which was used earlier and silently destroyed a reviewer's live
edit on the deployed dashboard the moment it ran. Existing rows on the
Railway copy are exactly where review edits live, so this script only ever
INSERTs brand-new posts (identified by shortcode, which is unique) -- it
never UPDATEs or DELETEs anything that's already there.

Usage:
    python sync_to_railway.py            # merge and push
    python sync_to_railway.py --dry-run  # merge into a scratch copy, don't push
"""
import argparse
import shutil
import sqlite3
import subprocess
from pathlib import Path

LOCAL_DB = Path(__file__).parent / "data" / "catalog.db"
TMP_LIVE = Path("/tmp/railway_sync_live.db")
SSH_ARGS = [
    "railway", "ssh", "-s", "dashboard",
    "-i", str(Path.home() / ".ssh" / "id_ed25519_railway_kevinbrown"),
]


def pull_live(dest: Path):
    with open(dest, "wb") as f:
        subprocess.run(SSH_ARGS + ["--", "cat /data/catalog.db"], stdout=f, check=True)


def push_live(src: Path):
    with open(src, "rb") as f:
        subprocess.run(SSH_ARGS + ["--", "cat > /data/catalog.db"], stdin=f, check=True)


def merge(live_path: Path):
    local = sqlite3.connect(LOCAL_DB)
    live = sqlite3.connect(live_path)
    local.row_factory = sqlite3.Row

    live_shortcodes = {r[0] for r in live.execute("SELECT shortcode FROM instagram_post")}
    new_posts = [
        p for p in local.execute("SELECT * FROM instagram_post").fetchall()
        if p["shortcode"] not in live_shortcodes
    ]

    post_id_map = {}
    product_id_map = {}

    for post in new_posts:
        cur = live.execute(
            """INSERT INTO instagram_post (shortcode, post_url, caption, post_date, media_type, profile, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post["shortcode"], post["post_url"], post["caption"], post["post_date"],
             post["media_type"], post["profile"], post["collected_at"]),
        )
        post_id_map[post["id"]] = cur.lastrowid

    media_count = 0
    for old_post_id, new_post_id in post_id_map.items():
        for prod in local.execute("SELECT * FROM product WHERE post_id = ?", (old_post_id,)):
            cur = live.execute(
                """INSERT INTO product (post_id, is_product_post, product_name, brand, category,
                     description, price, currency, sizes, colors, availability_status,
                     confidence_json, review_required, review_reason, duplicate_of, dedup_score,
                     raw_extraction_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_post_id, prod["is_product_post"], prod["product_name"], prod["brand"],
                 prod["category"], prod["description"], prod["price"], prod["currency"],
                 prod["sizes"], prod["colors"], prod["availability_status"], prod["confidence_json"],
                 prod["review_required"], prod["review_reason"], None, prod["dedup_score"],
                 prod["raw_extraction_json"], prod["created_at"]),
            )
            product_id_map[prod["id"]] = cur.lastrowid

        for m in local.execute("SELECT * FROM media WHERE post_id = ?", (old_post_id,)):
            live.execute(
                """INSERT INTO media (post_id, position, local_path, ocr_text, vision_description,
                     phash, excluded, added_by_reviewer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_post_id, m["position"], m["local_path"], m["ocr_text"], m["vision_description"],
                 m["phash"], m["excluded"], m["added_by_reviewer"]),
            )
            media_count += 1

    # duplicate_of on a newly-inserted product either points at another
    # product inserted in this same batch (remap it) or at a pre-existing
    # product (id already correct -- pre-existing rows are byte-identical
    # between local and live since nothing but inserts has happened on
    # either side since the last full clone).
    for old_id, new_id in product_id_map.items():
        old_dup = local.execute("SELECT duplicate_of FROM product WHERE id = ?", (old_id,)).fetchone()[0]
        if old_dup is not None and old_dup in product_id_map:
            live.execute("UPDATE product SET duplicate_of = ? WHERE id = ?", (product_id_map[old_dup], new_id))

    live.commit()
    local.close()
    live.close()
    return len(post_id_map), len(product_id_map), media_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("pulling live catalog.db from Railway...")
    pull_live(TMP_LIVE)

    posts, products, media = merge(TMP_LIVE)
    print(f"merged {posts} new post(s), {products} new product(s), {media} new media row(s)")

    if posts == 0:
        print("nothing new -- not pushing")
    elif args.dry_run:
        scratch = Path("/tmp/railway_sync_dry_run.db")
        shutil.copy(TMP_LIVE, scratch)
        print(f"dry run -- merged result saved to {scratch}, NOT pushed to Railway")
    else:
        push_live(TMP_LIVE)
        print("pushed merged database back to Railway")
