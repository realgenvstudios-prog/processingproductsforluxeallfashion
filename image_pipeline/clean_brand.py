"""
Scaled-up background removal: runs rembg over every real, non-duplicate
product's images for one brand, pulling directly from the catalog database
instead of a manually-populated folder. Idempotent -- rerunning skips
already-cleaned files. Originals are never touched.

Usage:
    python -m image_pipeline.clean_brand chicstyle.ghana [--max-images 4] [--model isnet-general-use]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

from rembg import new_session, remove

DEFAULT_MODEL = "isnet-general-use"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog.db"
OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "data" / "clean_images"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def clean_brand(profile: str, max_images: int, model: str):
    conn = get_conn()
    # "Fully ready" -- same bar as the dashboard's stat: no review needed,
    # not a duplicate, every key field filled in, and enough photos to be
    # worth cleaning. Only these are worth spending rembg time on right now.
    products = conn.execute(
        """SELECT pr.id as product_id, pr.post_id, pr.product_name
           FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ?
             AND pr.review_required = 0
             AND pr.duplicate_of IS NULL
             AND pr.product_name IS NOT NULL
             AND pr.price IS NOT NULL
             AND pr.sizes IS NOT NULL
             AND pr.colors IS NOT NULL
             AND pr.availability_status IS NOT NULL
             AND pr.availability_status = 'AVAILABLE'
             AND (SELECT COUNT(*) FROM media m WHERE m.post_id = pr.post_id AND m.excluded = 0) > 2
           ORDER BY pr.id""",
        (profile,),
    ).fetchall()
    print(f"{len(products)} real, non-duplicate product(s) for {profile}")

    output_dir = OUTPUT_ROOT / profile
    output_dir.mkdir(parents=True, exist_ok=True)
    session = new_session(model)

    total_images = 0
    cleaned = 0
    skipped = 0
    failed = 0
    t_start = time.time()

    for i, prod in enumerate(products):
        media_rows = conn.execute(
            "SELECT id, local_path, position FROM media WHERE post_id = ? AND excluded = 0 ORDER BY position LIMIT ?",
            (prod["post_id"], max_images),
        ).fetchall()

        product_dir = output_dir / f"product_{prod['product_id']}"
        for m in media_rows:
            total_images += 1
            src = Path(m["local_path"])
            out_path = product_dir / f"{m['position']}.png"
            if out_path.exists():
                skipped += 1
                continue
            if not src.exists():
                failed += 1
                continue
            try:
                product_dir.mkdir(parents=True, exist_ok=True)
                output_bytes = remove(src.read_bytes(), session=session)
                out_path.write_bytes(output_bytes)
                cleaned += 1
            except Exception as e:
                print(f"  FAILED product {prod['product_id']} image {m['id']}: {e}")
                failed += 1

        if (i + 1) % 20 == 0:
            elapsed = time.time() - t_start
            print(f"[{i+1}/{len(products)}] products done -- "
                  f"{cleaned} cleaned, {skipped} skipped, {failed} failed ({elapsed:.0f}s elapsed)")

    conn.close()
    print(f"done. {cleaned} cleaned, {skipped} already done, {failed} failed, "
          f"{total_images} image(s) considered across {len(products)} product(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--max-images", type=int, default=4)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    clean_brand(args.profile, args.max_images, args.model)
