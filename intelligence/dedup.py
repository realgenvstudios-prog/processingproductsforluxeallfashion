"""
Lightweight duplicate detection via perceptual image hashing + brand/name
matching. Flags likely duplicates for human review rather than silently
merging them -- see the project notes on why conflicts should never be
auto-resolved.

Usage:
    python -m intelligence.dedup [profile]
"""
import sys
from collections import defaultdict
from pathlib import Path

import imagehash
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_conn

# Calibrated against chicstyle.ghana's real catalog: this account's product
# photos are all similarly-lit studio shots on plain backgrounds (screenshots
# from retail sites), so phash alone produces false positives even at
# distance 0 -- e.g. two entirely different sandals from different brands
# ("LOUCADES" vs "AIL LAMBER") matched exactly just because both are a shoe
# silhouette on white. A brand match is required unconditionally; phash
# distance only decides how close a look-alike has to be within that brand.
HAMMING_DUPLICATE_THRESHOLD = 2


def compute_phashes(conn):
    rows = conn.execute("SELECT id, local_path FROM media WHERE phash IS NULL").fetchall()
    for r in rows:
        try:
            h = imagehash.phash(Image.open(r["local_path"]))
            conn.execute("UPDATE media SET phash = ? WHERE id = ?", (str(h), r["id"]))
        except Exception:
            continue
    conn.commit()


def run_dedup(profile: str = None):
    conn = get_conn()
    compute_phashes(conn)

    if profile:
        products = conn.execute(
            """SELECT pr.id as product_id, pr.post_id, pr.brand, pr.product_name, p.profile
               FROM product pr JOIN instagram_post p ON p.id = pr.post_id
               WHERE pr.is_product_post = 1 AND p.profile = ?""",
            (profile,),
        ).fetchall()
    else:
        products = conn.execute(
            """SELECT pr.id as product_id, pr.post_id, pr.brand, pr.product_name, p.profile
               FROM product pr JOIN instagram_post p ON p.id = pr.post_id
               WHERE pr.is_product_post = 1"""
        ).fetchall()

    # Comparisons never cross brands -- group everything by profile first,
    # regardless of whether a specific profile filter was passed in.
    by_profile = defaultdict(list)
    for p in products:
        by_profile[p["profile"]].append(p)

    total_flagged = 0
    for prof, prof_products in by_profile.items():
        product_hashes = {}
        for p in prof_products:
            media = conn.execute(
                "SELECT phash FROM media WHERE post_id = ? AND phash IS NOT NULL", (p["post_id"],)
            ).fetchall()
            hashes = [imagehash.hex_to_hash(m["phash"]) for m in media]
            product_hashes[p["product_id"]] = (p, hashes)

        ids = list(product_hashes.keys())
        flagged_this_profile = 0
        for i in range(len(ids)):
            pid_a, (prod_a, hashes_a) = ids[i], product_hashes[ids[i]]
            if not hashes_a:
                continue
            for j in range(i):
                pid_b, (prod_b, hashes_b) = ids[j], product_hashes[ids[j]]
                if not hashes_b:
                    continue
                best_dist = min(ha - hb for ha in hashes_a for hb in hashes_b)

                brand_a, brand_b = prod_a["brand"], prod_b["brand"]
                brand_match = bool(brand_a) and bool(brand_b) and brand_a.strip().lower() == brand_b.strip().lower()
                if best_dist <= HAMMING_DUPLICATE_THRESHOLD and brand_match:
                    score = max(0.0, 1 - best_dist / 64)
                    conn.execute(
                        "UPDATE product SET duplicate_of = ?, dedup_score = ? WHERE id = ?",
                        (pid_b, round(score, 2), pid_a),
                    )
                    print(f"[{prof}] product {pid_a} ({prod_a['product_name']}) flagged as likely duplicate "
                          f"of product {pid_b} ({prod_b['product_name']}), dist={best_dist}, score={score:.2f}")
                    flagged_this_profile += 1
                    # Commit incrementally rather than holding one long
                    # write transaction for the whole run -- a slow dedup
                    # pass shouldn't block other processes (pipelines,
                    # dashboard edits) writing to the same SQLite file for
                    # longer than their busy_timeout.
                    conn.commit()
        print(f"[{prof}] {flagged_this_profile} duplicate(s) flagged among {len(ids)} product(s)")
        total_flagged += flagged_this_profile

    conn.commit()
    conn.close()
    print(f"done. {total_flagged} duplicate(s) flagged total.")


if __name__ == "__main__":
    arg_profile = sys.argv[1] if len(sys.argv) > 1 else None
    run_dedup(arg_profile)
