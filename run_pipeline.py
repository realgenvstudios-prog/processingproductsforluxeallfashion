"""
Runs OCR + vision + extraction over every collected post that hasn't been
processed into a product record yet. Raw archive is untouched by this --
rerun freely as extraction logic improves.

Usage:
    python run_pipeline.py
"""
import json
import re
import time

from db import get_conn
from processing.ocr import ocr_image
from processing.vision import describe_image
from intelligence.extraction import extract_product

# Cap how many images per post get full OCR+vision analysis. Carousels rarely
# put new product info (brand/price/size) past the first few images -- later
# slides are usually repeat angles. Keeps quality on what matters most while
# avoiding paying full analysis cost on 8-10 image carousels.
MAX_IMAGES_PER_POST = 5

# Only matches unambiguous, unhedged "sold out" -- deliberately narrow so
# something like "one left, almost sold out" or "sold out sizes: 38" still
# goes through full extraction instead of being short-circuited on a guess.
EXPLICIT_SOLD_OUT_RE = re.compile(r"^\s*sold[\s-]*out\b", re.IGNORECASE)


def caption_is_explicitly_sold_out(caption: str) -> bool:
    if not caption:
        return False
    # og_luxemen/chicstyle captions from instaloader look like:
    # '12 likes, 3 comments - handle on <date>: "actual caption text". '
    # Strip that wrapper so the sold-out check runs against what the
    # seller actually wrote, not the scraped metadata prefix.
    match = re.search(r':\s*"(.*)"\.\s*$', caption, re.DOTALL)
    text = match.group(1) if match else caption
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return bool(EXPLICIT_SOLD_OUT_RE.match(first_line))


def process_sold_out_shortcut(conn, post_row):
    """Skip OCR/vision/full extraction entirely for posts whose caption is
    unambiguously just a sold-out notice -- there's nothing to sell, so
    there's nothing worth spending a vision-model call figuring out."""
    conn.execute(
        """INSERT INTO product (
            post_id, is_product_post, availability_status,
            review_required, review_reason
        ) VALUES (?, 1, 'SOLD_OUT', 0, ?)""",
        (post_row["id"], "caption explicitly says sold out -- skipped full extraction"),
    )
    conn.commit()
    return {
        "is_product_post": True,
        "product_name": None,
        "brand": None,
        "availability_status": "SOLD_OUT",
        "review_required": False,
    }


def process_post(conn, post_row):
    if caption_is_explicitly_sold_out(post_row["caption"]):
        return process_sold_out_shortcut(conn, post_row)

    post_id = post_row["id"]
    media_rows = conn.execute(
        "SELECT * FROM media WHERE post_id = ? AND excluded = 0 ORDER BY position LIMIT ?",
        (post_id, MAX_IMAGES_PER_POST),
    ).fetchall()

    for m in media_rows:
        if m["ocr_text"] is None:
            ocr_text = ocr_image(m["local_path"])
            conn.execute("UPDATE media SET ocr_text = ? WHERE id = ?", (ocr_text, m["id"]))
            conn.commit()
        if m["vision_description"] is None:
            vision_desc = describe_image(m["local_path"])
            conn.execute("UPDATE media SET vision_description = ? WHERE id = ?", (vision_desc, m["id"]))
            conn.commit()

    media_rows = conn.execute(
        "SELECT * FROM media WHERE post_id = ? AND excluded = 0 ORDER BY position LIMIT ?",
        (post_id, MAX_IMAGES_PER_POST),
    ).fetchall()

    result = extract_product(post_row["caption"], media_rows, profile=post_row["profile"])

    def as_text(value):
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return json.dumps(value)

    conn.execute(
        """INSERT INTO product (
            post_id, is_product_post, product_name, brand, category, description,
            price, currency, sizes, colors, availability_status, confidence_json,
            review_required, review_reason, raw_extraction_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            post_id,
            int(bool(result.get("is_product_post"))) if result.get("is_product_post") is not None else None,
            as_text(result.get("product_name")),
            as_text(result.get("brand")),
            as_text(result.get("category")),
            as_text(result.get("description")),
            as_text(result.get("price")),
            as_text(result.get("currency")),
            json.dumps(result.get("sizes")) if result.get("sizes") is not None else None,
            json.dumps(result.get("colors")) if result.get("colors") is not None else None,
            as_text(result.get("availability_status")),
            json.dumps(result.get("confidence")) if result.get("confidence") is not None else None,
            int(bool(result.get("review_required"))),
            as_text(result.get("review_reason")),
            json.dumps(result),
        ),
    )
    conn.commit()
    return result


def fetch_unprocessed(conn, profile: str = None):
    if profile:
        return conn.execute(
            """SELECT p.* FROM instagram_post p
               LEFT JOIN product pr ON pr.post_id = p.id
               WHERE pr.id IS NULL AND p.profile = ?""",
            (profile,),
        ).fetchall()
    return conn.execute(
        """SELECT p.* FROM instagram_post p
           LEFT JOIN product pr ON pr.post_id = p.id
           WHERE pr.id IS NULL"""
    ).fetchall()


def main(watch: bool, poll_seconds: int, profile: str = None):
    conn = get_conn()

    while True:
        posts = fetch_unprocessed(conn, profile)
        if not posts:
            if watch:
                print(f"no pending posts, waiting {poll_seconds}s for more to be collected...")
                time.sleep(poll_seconds)
                continue
            else:
                print("no pending posts.")
                break

        print(f"{len(posts)} post(s) to process")
        for i, post in enumerate(posts):
            print(f"[{i+1}/{len(posts)}] processing {post['shortcode']}...")
            t0 = time.time()
            try:
                result = process_post(conn, post)
            except Exception as e:
                # A single post failing (e.g. transient "database is locked"
                # from another process writing at the same time) shouldn't
                # kill an hours-long --watch run. Log it and move on --
                # fetch_unprocessed will pick this post back up next pass.
                print(f"  FAILED: {e} ({time.time()-t0:.0f}s) -- skipping for now")
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(2)
                continue
            print(f"  -> is_product_post={result.get('is_product_post')} "
                  f"product_name={result.get('product_name')} brand={result.get('brand')} "
                  f"availability={result.get('availability_status')} "
                  f"review_required={result.get('review_required')} ({time.time()-t0:.0f}s)")

        if not watch:
            break

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="keep running, polling for newly collected posts")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--profile", default=None, help="only process posts from this Instagram profile")
    args = parser.parse_args()
    main(args.watch, args.poll_seconds, args.profile)
