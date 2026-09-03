"""
One-way, insert-only merge: copies any Instagram posts (and their
product/media rows, AND the actual image files those media rows point at)
that exist locally but not in the Railway copy, into the deployed
dashboard -- without ever touching a row that already exists there.

This replaces a raw `cat catalog.db | ssh ... > /data/catalog.db` full-file
overwrite, which was used earlier and silently destroyed a reviewer's live
edit on the deployed dashboard the moment it ran. Existing rows on the
Railway copy are exactly where review edits live, so the database merge
only ever INSERTs brand-new posts (identified by shortcode, which is
unique) -- it never UPDATEs or DELETEs anything that's already there.

Syncing "new posts" without also syncing their image files leaves the
dashboard showing broken photos for everything just added, which defeats
the point -- so every run uploads the new posts' images too, in the same
command, not as a separate step someone has to remember.

Usage:
    python sync_to_railway.py            # merge and push (db + images)
    python sync_to_railway.py --dry-run  # merge into a scratch copy, don't push anything
"""
import argparse
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LOCAL_DB = DATA_DIR / "catalog.db"
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


def upload_images(local_paths: list[str]) -> int:
    """Tar up exactly the given local image files (paths relative to
    DATA_DIR) and extract them into /data on the volume -- same tar-over-ssh
    approach as the original bulk uploads, just scoped to only what's new."""
    rel_paths = []
    for p in local_paths:
        marker = "data/"
        idx = p.replace("\\", "/").rfind(marker)
        if idx == -1:
            continue
        rel = p[idx + len(marker):]
        if (DATA_DIR / rel).exists():
            rel_paths.append(rel)

    if not rel_paths:
        return 0

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(rel_paths))
        filelist = f.name

    tar = subprocess.Popen(
        ["tar", "czf", "-", "-C", str(DATA_DIR), "-T", filelist],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.run(
        SSH_ARGS + ["--", "cd /data && tar xzf -"],
        stdin=tar.stdout,
        check=True,
    )
    tar.stdout.close()
    tar.wait()
    Path(filelist).unlink(missing_ok=True)
    return len(rel_paths)


def merge(live_path: Path):
    local = sqlite3.connect(LOCAL_DB)
    live = sqlite3.connect(live_path)
    local.row_factory = sqlite3.Row
    live.row_factory = sqlite3.Row

    live_posts_by_shortcode = {r["shortcode"]: r["id"] for r in live.execute("SELECT id, shortcode FROM instagram_post")}
    all_local_posts = local.execute("SELECT * FROM instagram_post").fetchall()
    new_posts = [p for p in all_local_posts if p["shortcode"] not in live_posts_by_shortcode]

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
    image_paths = []
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
            if m["local_path"]:
                image_paths.append(m["local_path"])

    # Posts that already existed on both sides can still gain a product row
    # locally after the fact -- collection creates the post immediately,
    # extraction fills in its product later, often much later, in a
    # separate long-running process. Catch those too: any shared post where
    # live has zero product rows but local has one or more. (Skips posts
    # live already has at least one product for, to avoid guessing which
    # of several products on a multi-product post is the "new" one.)
    late_extracted_count = 0
    for post in all_local_posts:
        live_post_id = live_posts_by_shortcode.get(post["shortcode"])
        if live_post_id is None or post["id"] in post_id_map:
            continue  # brand-new post, already handled above
        local_products = local.execute("SELECT * FROM product WHERE post_id = ?", (post["id"],)).fetchall()
        if not local_products:
            continue
        already_has_product = live.execute(
            "SELECT 1 FROM product WHERE post_id = ? LIMIT 1", (live_post_id,)
        ).fetchone()
        if already_has_product:
            continue
        for prod in local_products:
            cur = live.execute(
                """INSERT INTO product (post_id, is_product_post, product_name, brand, category,
                     description, price, currency, sizes, colors, availability_status,
                     confidence_json, review_required, review_reason, duplicate_of, dedup_score,
                     raw_extraction_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (live_post_id, prod["is_product_post"], prod["product_name"], prod["brand"],
                 prod["category"], prod["description"], prod["price"], prod["currency"],
                 prod["sizes"], prod["colors"], prod["availability_status"], prod["confidence_json"],
                 prod["review_required"], prod["review_reason"], None, prod["dedup_score"],
                 prod["raw_extraction_json"], prod["created_at"]),
            )
            product_id_map[prod["id"]] = cur.lastrowid
            late_extracted_count += 1

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
    return len(post_id_map), len(product_id_map), media_count, image_paths, late_extracted_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("pulling live catalog.db from Railway...")
    pull_live(TMP_LIVE)

    posts, products, media, image_paths, late_extracted = merge(TMP_LIVE)
    print(
        f"merged {posts} new post(s), {products} new product(s) "
        f"({late_extracted} of those from posts already on Railway that finished extracting since), "
        f"{media} new media row(s)"
    )

    if posts == 0 and products == 0:
        print("nothing new -- not pushing")
    elif args.dry_run:
        scratch = Path("/tmp/railway_sync_dry_run.db")
        shutil.copy(TMP_LIVE, scratch)
        print(f"dry run -- merged result saved to {scratch}, NOT pushed (db or images)")
    else:
        push_live(TMP_LIVE)
        print("pushed merged database back to Railway")
        uploaded = upload_images(image_paths)
        print(f"uploaded {uploaded} new image file(s) to the volume")
