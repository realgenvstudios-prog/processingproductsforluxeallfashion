"""
Pushes "ready to ship" products (fully reviewed + images already uploaded to
R2) from our local catalog into the real website's live Postgres database as
actual Product + ProductVariant rows. Idempotent -- a product already
imported (tracked in the local `imported_product` table) is skipped on
rerun, so this is safe to run again after reviewing/uploading more.

One size+color simplification for V1: our extraction sometimes returns
multiple color entries for what's really one item's two-tone colorway, not
separate stock colors -- so all colors are joined into one descriptive
colorName per variant, one variant per size, rather than a full size x color
cartesian product that would invent SKUs that don't exist.

Usage:
    python website_import.py chicstyle.ghana [--dry-run]
"""
import argparse
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import psycopg2

DB_PATH = Path(__file__).parent / "data" / "catalog.db"
WEBSITE_ENV_PATH = Path("/Users/Ted/OKC Website/apps/admin/.env")

BRAND_ENUM = {
    "chicstyle.ghana": "CHICSTYLE",
    "og_luxemen": "OG_LUXEMEN",
    "kiddies_spacegh": "KIDDIES_SPACE_GH",
}
DEFAULT_COLOR_HEX = "#808080"


def load_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS imported_product (
            product_id INTEGER PRIMARY KEY,
            live_product_id TEXT NOT NULL,
            imported_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    return conn


def fetch_ready_products(sconn, profile):
    return sconn.execute(
        """SELECT pr.* FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ?
             AND pr.review_required = 0
             AND pr.duplicate_of IS NULL
             AND pr.availability_status = 'AVAILABLE'
             AND pr.product_name IS NOT NULL
             AND pr.price IS NOT NULL
             AND pr.sizes IS NOT NULL
             AND pr.colors IS NOT NULL
             AND (SELECT COUNT(*) FROM media m WHERE m.post_id = pr.post_id AND m.excluded = 0) > 2
             AND pr.id NOT IN (SELECT product_id FROM imported_product)
           ORDER BY pr.id""",
        (profile,),
    ).fetchall()


def unique_name(base_name, colors, used_slugs, product_id):
    # The DB constraint that actually matters is on slug, not name -- two
    # names differing only by case ("Zara..." vs "ZARA...") collide once
    # slugified, so uniqueness has to be checked at that level too.
    candidates = [base_name]
    if colors:
        candidates.append(f"{base_name} ({colors[0]['name']})")
    candidates.append(f"{base_name} #{product_id}")

    for candidate in candidates:
        slug = slugify(candidate)
        if slug not in used_slugs:
            used_slugs.add(slug)
            return candidate
    # Shouldn't happen (the #product_id candidate is always unique), but
    # fall back to guarantee uniqueness rather than ever silently colliding.
    candidate = f"{base_name} #{product_id}-{uuid.uuid4().hex[:6]}"
    used_slugs.add(slugify(candidate))
    return candidate


def parse_price_pesewas(price_text):
    digits = re.sub(r"[^\d.]", "", price_text or "")
    if not digits:
        return None
    try:
        return int(round(float(digits) * 100))
    except ValueError:
        return None


def import_brand(profile: str, dry_run: bool):
    brand_enum = BRAND_ENUM.get(profile)
    if not brand_enum:
        print(f"unknown profile {profile}, no Brand enum mapping")
        return

    env = load_env(WEBSITE_ENV_PATH)
    pconn = psycopg2.connect(env["DATABASE_URL"])
    pconn.autocommit = False
    pcur = pconn.cursor()

    pcur.execute('SELECT id, name FROM "Category" WHERE brand = %s AND "parentId" IS NULL', (brand_enum,))
    top_categories = {name: cid for cid, name in pcur.fetchall()}
    pcur.execute(
        """SELECT c.id, c.name, p.name FROM "Category" c
           JOIN "Category" p ON p.id = c."parentId"
           WHERE c.brand = %s""",
        (brand_enum,),
    )
    child_categories = {name: cid for cid, name, _parent in pcur.fetchall()}
    category_by_name = {**top_categories, **child_categories}

    pcur.execute('SELECT slug FROM "Product"')
    used_slugs = {row[0] for row in pcur.fetchall()}

    sconn = get_sqlite_conn()
    products = fetch_ready_products(sconn, profile)
    print(f"{len(products)} product(s) ready to import for {profile}")

    brand_handle = profile.split(".")[0].lower()
    imported = 0
    skipped_no_category = 0
    skipped_no_price = 0
    skipped_no_images = 0
    skipped_no_sizes = 0
    skipped_suspicious = 0

    for prod in products:
        # Guards against a real failure mode we found by hand: extraction
        # occasionally mistakes the seller's own Instagram handle for the
        # manufacturer brand (and it leaks into color/name too), and it can
        # score high-confidence despite being wrong. Don't ship those blind.
        if brand_handle in (prod["brand"] or "").lower():
            print(f"  skip product {prod['id']}: brand '{prod['brand']}' looks like the account handle, not a real brand")
            skipped_suspicious += 1
            continue

        sizes_check = json.loads(prod["sizes"])
        if not sizes_check:
            print(f"  skip product {prod['id']}: sizes list is empty, nothing to make variants from")
            skipped_no_sizes += 1
            continue

        category_name = prod["category"]
        category_id = category_by_name.get(category_name)
        if not category_id:
            print(f"  skip product {prod['id']}: category '{category_name}' not found on live site")
            skipped_no_category += 1
            continue

        price_pesewas = parse_price_pesewas(prod["price"])
        if not price_pesewas:
            print(f"  skip product {prod['id']}: unparseable price '{prod['price']}'")
            skipped_no_price += 1
            continue

        image_rows = sconn.execute(
            "SELECT public_url FROM uploaded_image WHERE product_id = ? ORDER BY position", (prod["id"],)
        ).fetchall()
        images = [r["public_url"] for r in image_rows]
        if not images:
            print(f"  skip product {prod['id']}: no uploaded images")
            skipped_no_images += 1
            continue

        colors = json.loads(prod["colors"])
        sizes = json.loads(prod["sizes"])
        color_name = " / ".join(c["name"] for c in colors if c.get("name")) or "Default"
        color_hex = next((c["hex"] for c in colors if c.get("hex")), DEFAULT_COLOR_HEX)

        name = unique_name(prod["product_name"], colors, used_slugs, prod["id"])
        slug = slugify(name)
        description = prod["description"] or prod["product_name"]

        product_id = str(uuid.uuid4())
        brand_slug = profile.split(".")[0]

        if dry_run:
            print(f"  [dry-run] would import product {prod['id']} -> '{name}' "
                  f"({category_name}, {price_pesewas} pesewas, sizes={sizes}, color='{color_name}')")
            imported += 1
            continue

        pcur.execute(
            """INSERT INTO "Product" (id, name, slug, description, brand, "categoryId", images,
                                       "isActive", "isNewIn", "createdAt", "updatedAt")
               VALUES (%s, %s, %s, %s, %s, %s, %s, true, false, now(), now())""",
            (product_id, name, slug, description, brand_enum, category_id, images),
        )

        for size in sizes:
            variant_id = str(uuid.uuid4())
            sku = f"{brand_slug}-{prod['id']}-{slugify(str(size))}".upper()
            pcur.execute(
                """INSERT INTO "ProductVariant" (id, "productId", size, "colorName", "colorHex",
                                                   sku, quantity, "priceGhs", "createdAt", "updatedAt")
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())""",
                (variant_id, product_id, str(size), color_name, color_hex, sku, 1, price_pesewas),
            )

        sconn.execute(
            "INSERT INTO imported_product (product_id, live_product_id) VALUES (?, ?)",
            (prod["id"], product_id),
        )
        sconn.commit()
        pconn.commit()
        imported += 1
        print(f"  imported product {prod['id']} -> '{name}' (live id {product_id}, {len(sizes)} variant(s))")

    pcur.close()
    pconn.close()
    sconn.close()
    print(f"done. {imported} imported, {skipped_suspicious} skipped (suspicious brand), "
          f"{skipped_no_sizes} skipped (empty sizes), {skipped_no_category} skipped (no category), "
          f"{skipped_no_price} skipped (no price), {skipped_no_images} skipped (no images).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    import_brand(args.profile, args.dry_run)
