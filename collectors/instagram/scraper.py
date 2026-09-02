"""
Anonymous Instagram collector for public profiles (instaloader-based).
Saves raw post data + downloads media untouched, before any AI processing.

Usage:
    python -m collectors.instagram.scraper <username> --limit 10
"""
import argparse
import sys
import time
from pathlib import Path

import instaloader
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from db import get_conn

MEDIA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "media"


def download_file(url: str, dest: Path):
    if dest.exists():
        return
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def collect(username: str, limit: int, session_user: str = None):
    L = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    if session_user:
        L.load_session_from_file(session_user)
    profile = instaloader.Profile.from_username(L.context, username)
    conn = get_conn()

    count = 0
    for post in profile.get_posts():
        if count >= limit:
            break

        post_url = f"https://www.instagram.com/p/{post.shortcode}/"
        existing = conn.execute(
            "SELECT id FROM instagram_post WHERE shortcode = ?", (post.shortcode,)
        ).fetchone()
        if existing:
            print(f"skip (already collected): {post.shortcode}")
            continue

        cur = conn.execute(
            """INSERT INTO instagram_post (shortcode, post_url, caption, post_date, media_type, profile)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (post.shortcode, post_url, post.caption or "", post.date_utc.isoformat(), post.typename, username),
        )
        post_id = cur.lastrowid
        post_dir = MEDIA_DIR / post.shortcode
        post_dir.mkdir(parents=True, exist_ok=True)

        nodes = []
        if post.typename == "GraphSidecar":
            nodes = [n for n in post.get_sidecar_nodes() if not n.is_video]
        elif not post.is_video:
            nodes = [post]

        for i, node in enumerate(nodes):
            display_url = node.display_url
            local_path = post_dir / f"{i}.jpg"
            try:
                download_file(display_url, local_path)
            except Exception as e:
                print(f"  media download failed for {post.shortcode}[{i}]: {e}")
                continue
            conn.execute(
                "INSERT INTO media (post_id, position, local_path) VALUES (?, ?, ?)",
                (post_id, i, str(local_path)),
            )

        conn.commit()
        count += 1
        print(f"collected {post.shortcode} ({count}/{limit}), {len(nodes)} image(s)")
        time.sleep(2)  # be polite, avoid rate limiting

    conn.close()
    print(f"done. {count} new posts collected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--session-user", help="Your own IG username, after running `instaloader --login=<you>` once")
    args = parser.parse_args()
    collect(args.username, args.limit, args.session_user)
