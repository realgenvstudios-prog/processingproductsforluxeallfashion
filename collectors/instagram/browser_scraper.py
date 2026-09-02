"""
Instagram collector driven by a real browser (Playwright), reusing the
persistent login session created by browser_login.py. Avoids Instagram's
undocumented internal JSON APIs entirely by reading the rendered page,
the same way a human browsing the site would.

Usage:
    python -m collectors.instagram.browser_scraper chicstyle.ghana --limit 10
"""
import argparse
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from db import get_conn

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MEDIA_DIR = DATA_DIR / "raw" / "media"


def collect_shortcodes(page, username: str, limit: int):
    page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    shortcodes = []
    seen = set()
    stagnant_rounds = 0
    max_stagnant = 60
    last_height = 0
    while len(shortcodes) < limit and stagnant_rounds < max_stagnant:
        hrefs = page.eval_on_selector_all(
            "a[href*='/p/'], a[href*='/reel/']",
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs:
            m = re.search(r"/(?:p|reel)/([^/]+)/", href or "")
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                shortcodes.append(m.group(1))

        if len(shortcodes) >= limit:
            break

        before = len(shortcodes)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)
        height = page.evaluate("document.body.scrollHeight")

        # progress if either new shortcodes appeared or the page actually grew
        # (virtualization can unmount old link elements, temporarily hiding
        # shortcodes we already recorded, without that meaning we're stuck)
        if len(shortcodes) == before and height <= last_height:
            stagnant_rounds += 1
            page.wait_for_timeout(1500 + min(stagnant_rounds, 20) * 500)
        else:
            stagnant_rounds = 0
        last_height = height

    print(f"  (scroll loop ended: {len(shortcodes)} shortcodes, stagnant_rounds={stagnant_rounds}, page_height={last_height})")

    return shortcodes[:limit]


SCOPE_JS = "document.querySelector('main').children[0].children[0]"


def scrape_post(page, shortcode: str):
    url = f"https://www.instagram.com/p/{shortcode}/"
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)

    caption = ""
    try:
        meta = page.eval_on_selector("meta[property='og:description']", "el => el.content")
        caption = meta or ""
    except Exception:
        pass

    post_date = ""
    try:
        post_date = page.eval_on_selector("time", "el => el.getAttribute('datetime')") or ""
    except Exception:
        pass

    is_video = page.evaluate(f"() => {{ const s = {SCOPE_JS}; return !!(s && s.querySelector('video')); }}")

    image_urls = []
    if not is_video:
        seen_urls = set()
        for _ in range(10):
            srcs = page.evaluate(
                f"""() => {{
                    const s = {SCOPE_JS};
                    if (!s) return [];
                    return Array.from(s.querySelectorAll('img'))
                        .filter(i => i.naturalWidth > 200)
                        .map(i => i.src);
                }}"""
            )
            for s in srcs:
                if s and s not in seen_urls:
                    seen_urls.add(s)
                    image_urls.append(s)
            next_btn = page.query_selector("button[aria-label='Next']")
            if next_btn:
                next_btn.click()
                page.wait_for_timeout(700)
            else:
                break

    return {"url": url, "caption": caption, "post_date": post_date, "image_urls": image_urls, "is_video": is_video}


def download_file(url: str, dest: Path):
    if dest.exists():
        return
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def collect(username: str, limit: int, profile: str = "browser_profile"):
    conn = get_conn()
    profile_dir = DATA_DIR / profile
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        shortcodes = collect_shortcodes(page, username, limit)
        print(f"found {len(shortcodes)} post shortcodes")

        count = 0
        for shortcode in shortcodes:
            existing = conn.execute(
                "SELECT id FROM instagram_post WHERE shortcode = ?", (shortcode,)
            ).fetchone()
            if existing:
                print(f"skip (already collected): {shortcode}")
                continue

            try:
                data = scrape_post(page, shortcode)
            except Exception as e:
                print(f"  failed to scrape {shortcode}: {e}")
                continue

            post_url = f"https://www.instagram.com/p/{shortcode}/"
            if data["is_video"]:
                media_type = "video"
            elif len(data["image_urls"]) > 1:
                media_type = "carousel"
            else:
                media_type = "image"
            cur = conn.execute(
                """INSERT INTO instagram_post (shortcode, post_url, caption, post_date, media_type, profile)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (shortcode, post_url, data["caption"], data["post_date"], media_type, username),
            )
            post_id = cur.lastrowid
            post_dir = MEDIA_DIR / shortcode
            post_dir.mkdir(parents=True, exist_ok=True)
            media_count = 0

            if data["is_video"]:
                local_path = post_dir / "frame_0.jpg"
                try:
                    video_el = page.query_selector("main video")
                    if video_el:
                        video_el.screenshot(path=str(local_path))
                        conn.execute(
                            "INSERT INTO media (post_id, position, local_path) VALUES (?, ?, ?)",
                            (post_id, 0, str(local_path)),
                        )
                        media_count = 1
                except Exception as e:
                    print(f"  video frame capture failed for {shortcode}: {e}")
            else:
                for i, img_url in enumerate(data["image_urls"]):
                    local_path = post_dir / f"{i}.jpg"
                    try:
                        download_file(img_url, local_path)
                    except Exception as e:
                        print(f"  media download failed for {shortcode}[{i}]: {e}")
                        continue
                    conn.execute(
                        "INSERT INTO media (post_id, position, local_path) VALUES (?, ?, ?)",
                        (post_id, i, str(local_path)),
                    )
                    media_count += 1

            conn.commit()
            count += 1
            print(f"collected {shortcode} ({count}/{len(shortcodes)}) [{media_type}], {media_count} media file(s)")
            time.sleep(1.5)

        context.close()

    conn.close()
    print(f"done. {count} new posts collected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--profile", default="browser_profile")
    args = parser.parse_args()
    collect(args.username, args.limit, args.profile)
