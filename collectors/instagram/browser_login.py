"""
One-time interactive login. Opens a real, visible browser window pointed at
Instagram's login page. YOU log in manually (password/2FA never touch this
codebase). The session is saved to a persistent browser profile on disk and
reused by browser_scraper.py for future runs.

Use --profile to keep multiple separate logged-in accounts side by side
(e.g. to switch accounts if one gets rate-limited) without overwriting an
existing session.

Usage:
    python -m collectors.instagram.browser_login [--profile browser_profile_2]
"""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def main(profile: str):
    profile_dir = DATA_DIR / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.instagram.com/accounts/login/")
        print("\nA browser window has opened.")
        print("Log in to Instagram manually in that window (your own account).")
        print("Once you see your home feed / profile loaded, come back here and press Enter.")
        input()
        context.close()
        print(f"Session saved to data/{profile}. Use --profile {profile} with browser_scraper.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="browser_profile")
    args = parser.parse_args()
    main(args.profile)
