"""
Uploads cleaned (background-removed) product images to the real website's
Cloudflare R2 bucket -- the same bucket/credentials the admin app itself
uses -- so products get real, public image URLs before import. Idempotent:
already-uploaded (product_id, position) pairs are skipped.

Reads R2 credentials from the OKC Website admin app's .env (not copied
into this project -- read directly from its location each run).

Usage:
    python -m image_pipeline.upload_r2 chicstyle.ghana
"""
import argparse
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_conn

WEBSITE_ENV_PATH = Path("/Users/Ted/OKC Website/apps/admin/.env")
CLEAN_IMAGES_ROOT = Path(__file__).resolve().parents[1] / "data" / "clean_images"


def load_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def make_r2_client(env: dict):
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload_brand(profile: str, folder: str = "products"):
    env = load_env(WEBSITE_ENV_PATH)
    bucket = env["R2_BUCKET_NAME"]
    public_base = env["R2_PUBLIC_URL"].rstrip("/")
    s3 = make_r2_client(env)

    conn = get_conn()
    products = conn.execute(
        """SELECT pr.id FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ?
             AND pr.review_required = 0
             AND pr.duplicate_of IS NULL
             AND pr.availability_status = 'AVAILABLE'
             AND pr.product_name IS NOT NULL
             AND pr.price IS NOT NULL
             AND pr.sizes IS NOT NULL
             AND pr.colors IS NOT NULL
             AND (SELECT COUNT(*) FROM media m WHERE m.post_id = pr.post_id AND m.excluded = 0) > 2""",
        (profile,),
    ).fetchall()

    uploaded = 0
    skipped = 0
    no_image = 0

    for prod in products:
        pid = prod["id"]
        product_dir = CLEAN_IMAGES_ROOT / profile / f"product_{pid}"
        if not product_dir.is_dir():
            no_image += 1
            continue

        for png_path in sorted(product_dir.glob("*.png")):
            position = int(png_path.stem)
            existing = conn.execute(
                "SELECT id FROM uploaded_image WHERE product_id = ? AND position = ?",
                (pid, position),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            brand_slug = profile.split(".")[0]
            key = f"{folder}/{brand_slug}-{pid}-{position}.png"
            s3.upload_file(str(png_path), bucket, key, ExtraArgs={"ContentType": "image/png"})
            public_url = f"{public_base}/{key}"

            conn.execute(
                "INSERT INTO uploaded_image (product_id, position, public_url, r2_key) VALUES (?, ?, ?, ?)",
                (pid, position, public_url, key),
            )
            conn.commit()
            uploaded += 1
            print(f"uploaded product {pid} image {position} -> {public_url}")

    conn.close()
    print(f"done. {uploaded} uploaded, {skipped} already uploaded, {no_image} product(s) with no cleaned image yet.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--folder", default="products")
    args = parser.parse_args()
    upload_brand(args.profile, args.folder)
