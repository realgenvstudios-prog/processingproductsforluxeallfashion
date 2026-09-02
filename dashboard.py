"""
Local live-progress dashboard. Read-only against the same SQLite DB the
collector and pipeline write to -- safe to run alongside them.

Usage:
    python dashboard.py
Then open http://localhost:5050 in a browser. Auto-refreshes every 8s.
Each Instagram profile (brand) gets its own page: /brand/<profile>
"""
import hmac
import json
import os
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

from flask import Flask, Response, abort, redirect, request, send_file

from intelligence.extraction import CATEGORY_SETS

# DATA_DIR points at wherever the real data (db + images) actually lives --
# a local "data/" folder in dev, a mounted Railway volume in production.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DB_PATH = DATA_DIR / "catalog.db"
CLEAN_IMAGES_ROOT = DATA_DIR / "clean_images"
DEFAULT_PAGE_SIZE = 50
ALLOWED_PAGE_SIZES = (20, 50, 100)

# Single shared password gate -- this dashboard can view AND edit/delete real
# product data, so it can't be left open once it's off localhost. One
# password for everyone (Ted + client) is intentional: nothing here is
# sensitive enough to need per-user accounts, just enough to keep it off the
# open internet. MUST be set via env var in any real deployment -- the
# fallback only exists so local `python dashboard.py` still works untouched.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "localdev")

app = Flask(__name__)


@app.before_request
def require_auth():
    if not os.environ.get("PORT"):
        return  # local dev (`python dashboard.py`) -- no password needed on localhost
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD):
        return Response(
            "Login required.", 401, {"WWW-Authenticate": 'Basic realm="Catalog Review"'}
        )


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


STYLE = """
  * { box-sizing: border-box; }
  html, body { height: auto; min-height: 100%; overflow-y: auto; }
  body {
    background: #fafafa;
    color: #1a1a1a;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    padding: 32px 40px 120px;
  }
  .table-wrap { overflow-x: auto; }
  .page-size { display: flex; align-items: center; gap: 8px; margin: 10px 0 20px; font-size: 13px; color: #888; }
  .page-size a {
    padding: 3px 10px; border-radius: 6px; color: #555; background: #f4f4f4;
  }
  .page-size a.active { background: #1a1a1a; color: #fff; }
  h1 { font-size: 22px; font-weight: 700; margin: 0 0 4px; }
  .subtitle { color: #888; font-size: 13px; margin-bottom: 20px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .tab {
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
    text-decoration: none;
    color: #555;
    background: #fff;
    border: 1px solid #eee;
  }
  .tab.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .tips-banner {
    background: #eff6ff; border: 1px solid #dbeafe; color: #1e3a5f;
    border-radius: 10px; padding: 14px 18px; font-size: 13px; line-height: 1.6;
    margin-bottom: 28px;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 36px;
  }
  .stat { border: 1px solid #eee; border-radius: 10px; padding: 16px 18px; background: #fff; }
  .stat .value { font-size: 26px; font-weight: 700; }
  .stat .label { font-size: 12px; color: #888; margin-top: 2px; text-transform: uppercase; letter-spacing: 0.02em; }
  .stat.warn .value { color: #b45309; }
  .stat.good .value { color: #15803d; }
  .progress-label { display: flex; justify-content: space-between; font-size: 12px; color: #888; margin-bottom: 6px; }
  .progress-label .pct { font-weight: 700; color: #16a34a; }
  .progress-bar { height: 8px; background: #f1f1f1; border-radius: 4px; overflow: hidden; margin-bottom: 36px; }
  .progress-fill { height: 100%; background: #16a34a; }
  .progress-fill.img-fill { background: #2563eb; }
  .pct.img-pct { color: #2563eb; }
  table { border-collapse: collapse; width: 100%; }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em;
    color: #999; padding: 8px 10px; border-bottom: 1px solid #eee;
  }
  td { padding: 10px; border-bottom: 1px solid #f4f4f4; font-size: 13px; vertical-align: middle; }
  img.thumb { width: 48px; height: 48px; object-fit: cover; border-radius: 6px; background: #f7f7f7; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }
  .badge.AVAILABLE { background: #dcfce7; color: #166534; }
  .badge.SOLD_OUT { background: #fee2e2; color: #991b1b; }
  .badge.UNKNOWN { background: #f3f4f6; color: #6b7280; }
  .badge.RESERVED, .badge.PREORDER { background: #fef3c7; color: #92400e; }
  .badge.NOT_A_PRODUCT { background: #ede9fe; color: #5b21b6; }
  .review-yes { color: #b45309; font-weight: 600; }
  .review-no { color: #ccc; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .brand-card {
    display: block; border: 1px solid #eee; border-radius: 12px; padding: 20px 22px;
    margin-bottom: 12px; text-decoration: none; color: inherit; background: #fff;
    transition: border-color 0.15s;
  }
  .brand-card:hover { border-color: #999; text-decoration: none; }
  .brand-card .name { font-size: 16px; font-weight: 700; display: flex; justify-content: space-between; align-items: center; }
  .brand-card .meta { color: #888; font-size: 13px; margin-top: 4px; }
  .pagination {
    display: flex; align-items: center; justify-content: center; gap: 16px;
    margin-top: 24px; font-size: 13px; color: #666;
  }
  .pagination a {
    padding: 6px 14px; border: 1px solid #ddd; border-radius: 6px; color: #1a1a1a; background: #fff;
  }
  .pagination a.disabled { color: #ccc; border-color: #eee; pointer-events: none; }
  .pagination a:hover { border-color: #999; text-decoration: none; }
  .status-filters { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .status-filters a {
    padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; color: #555; background: #fff; border: 1px solid #eee;
  }
  .status-filters a.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .review-link { margin-left: 8px; color: #b45309; font-weight: 600; }

  /* Product grid (brand page) */
  .product-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 16px;
    margin-bottom: 8px;
  }
  .product-card {
    background: #fff; border: 1px solid #eee; border-radius: 12px; overflow: hidden;
    display: flex; flex-direction: column; position: relative; transition: box-shadow 0.15s, border-color 0.15s;
  }
  .product-card:hover { border-color: #ccc; box-shadow: 0 2px 10px rgba(0,0,0,0.06); }
  .needs-review-flag {
    position: absolute; top: 8px; left: 8px; z-index: 1; background: #b45309; color: #fff;
    font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 20px; letter-spacing: 0.02em;
  }
  .needs-review-flag.inline { position: static; display: inline-block; }
  .thumb-wrap { display: block; aspect-ratio: 3 / 4; background: #f7f7f7; overflow: hidden; }
  .thumb-wrap img.thumb, .thumb-wrap .thumb-empty {
    width: 100%; height: 100%; object-fit: cover; display: block;
  }
  .thumb-empty {
    display: flex; align-items: center; justify-content: center; color: #ccc; font-size: 12px; text-align: center;
  }
  .pinfo { padding: 10px 12px 4px; flex: 1; }
  .pname {
    font-size: 13px; font-weight: 600; line-height: 1.3; margin-bottom: 6px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    min-height: 2.6em;
  }
  .pmeta { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
  .pmeta .price { font-size: 13px; font-weight: 700; color: #1a1a1a; }
  .pactions { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px 12px; }
  .review-btn {
    background: #1a1a1a; color: #fff; font-size: 12px; font-weight: 600; padding: 7px 12px;
    border-radius: 7px; text-decoration: none;
  }
  .review-btn:hover { background: #333; text-decoration: none; }
  .view-link { font-size: 12px; color: #888; }

  /* Edit / review page */
  .edit-page { max-width: 760px; }
  .back-link { display: inline-block; color: #888; font-size: 13px; }
  .edit-topbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .edit-bottombar { margin-top: 28px; }
  .nav-buttons { display: flex; align-items: center; gap: 10px; }
  .nav-btn {
    padding: 7px 14px; border: 1px solid #ddd; border-radius: 7px; font-size: 13px; font-weight: 600;
    color: #1a1a1a; background: #fff;
  }
  .nav-btn:hover { border-color: #999; text-decoration: none; }
  .nav-btn.disabled { color: #ccc; border-color: #eee; }
  .nav-position { font-size: 12px; color: #888; }
  .section-card {
    background: #fff; border: 1px solid #eee; border-radius: 12px; padding: 20px 22px; margin-bottom: 18px;
  }
  .section-title {
    font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: #999;
    margin-bottom: 14px;
  }
  .edit-images { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .edit-image-tile { position: relative; width: 130px; }
  .edit-images img {
    width: 130px; height: 130px; object-fit: cover; border-radius: 9px; background: #f7f7f7; display: block;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .edit-image-tile form { position: absolute; top: 4px; right: 4px; margin: 0; }
  .edit-image-remove {
    width: 22px; height: 22px; border-radius: 50%; border: none; background: rgba(0,0,0,0.65); color: #fff;
    font-size: 13px; line-height: 1; cursor: pointer;
  }
  .edit-image-remove:hover { background: #dc2626; }
  .upload-form { margin: 0; display: flex; align-items: center; gap: 10px; }
  .upload-form input[type=file] { font-size: 12px; }
  .upload-btn {
    background: #f4f4f4; border: 1px solid #ddd; padding: 7px 14px; border-radius: 7px;
    font-size: 12px; font-weight: 600; cursor: pointer; color: #333;
  }
  .upload-btn:hover { border-color: #999; }
  .caption-box {
    background: #fafafa; border: 1px solid #eee; border-radius: 8px; padding: 14px 16px;
    font-size: 13px; color: #555; white-space: pre-wrap; max-height: 160px; overflow-y: auto;
  }
  .field { margin-bottom: 18px; }
  .field:last-child { margin-bottom: 0; }
  .field label { display: block; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; color: #888; margin-bottom: 6px; }
  .field input[type=text] {
    width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; box-sizing: border-box;
  }
  .field input[type=text]:focus, .field select:focus { outline: none; border-color: #1a1a1a; }
  .field select {
    width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px;
    box-sizing: border-box; background: #fff; font-family: inherit;
  }
  .field .hint { font-size: 11px; color: #aaa; margin-top: 5px; text-transform: none; letter-spacing: normal; }
  .field .custom-color-input { margin-top: 10px; }
  .color-picker {
    display: flex; flex-wrap: wrap; gap: 6px; max-height: 160px; overflow-y: auto;
    border: 1px solid #eee; border-radius: 8px; padding: 10px;
  }
  .color-picker label {
    display: inline-flex; align-items: center; gap: 5px; padding: 5px 11px; border-radius: 16px;
    border: 1px solid #ddd; font-size: 12px; font-weight: 500; cursor: pointer; color: #555;
    text-transform: none; letter-spacing: normal;
  }
  .color-picker input { display: none; }
  .color-picker label:has(input:checked) { background: #1a1a1a; border-color: #1a1a1a; color: #fff; }
  .status-toggle { display: flex; gap: 8px; flex-wrap: wrap; }
  .status-toggle label {
    display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 20px;
    border: 1px solid #ddd; font-size: 13px; font-weight: 600; cursor: pointer; color: #555;
    text-transform: none; letter-spacing: normal;
  }
  .status-toggle input { display: none; }
  .status-toggle input:checked + span { color: inherit; }
  .status-toggle label:has(input:checked) { border-color: #1a1a1a; }
  .status-toggle label.opt-AVAILABLE:has(input:checked) { background: #dcfce7; border-color: #166534; color: #166534; }
  .status-toggle label.opt-SOLD_OUT:has(input:checked) { background: #fee2e2; border-color: #991b1b; color: #991b1b; }
  .status-toggle label.opt-UNKNOWN:has(input:checked) { background: #f3f4f6; border-color: #6b7280; color: #6b7280; }
  .status-toggle label.opt-NOT_A_PRODUCT:has(input:checked) { background: #ede9fe; border-color: #5b21b6; color: #5b21b6; }
  .still-review {
    display: flex; align-items: center; gap: 8px; margin: 18px 0; font-size: 13px; color: #555;
    background: #fff; border: 1px solid #eee; border-radius: 10px; padding: 14px 16px;
  }
  .save-row { display: flex; align-items: center; gap: 12px; margin-top: 22px; }
  .save-btn {
    background: #1a1a1a; color: #fff; border: none; padding: 12px 28px; border-radius: 8px;
    font-size: 14px; font-weight: 600; cursor: pointer;
  }
  .save-btn:hover { background: #333; }
  .view-post-btn {
    display: inline-block; padding: 12px 20px; border: 1px solid #ddd; border-radius: 8px;
    font-size: 14px; color: #1a1a1a; background: #fff;
  }
  .saved-banner { padding: 12px 16px; border-radius: 8px; font-size: 13px; margin-bottom: 20px; display: flex; gap: 14px; flex-wrap: wrap; align-items: center; }
  .saved-banner.ready { background: #dcfce7; color: #166534; }
  .saved-banner.not-ready { background: #fef3c7; color: #92400e; }
  .saved-banner a { color: inherit; font-weight: 700; text-decoration: underline; }

  @media (max-width: 640px) {
    body { padding: 16px 14px 100px; }
    h1 { font-size: 19px; }
    .subtitle { font-size: 12px; margin-bottom: 16px; }

    .tabs {
      flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
      padding-bottom: 6px; margin-bottom: 16px;
    }
    .tab { flex: 0 0 auto; }

    .tips-banner { font-size: 12.5px; padding: 12px 14px; margin-bottom: 20px; }

    .stats { grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 24px; }
    .stat { padding: 12px 14px; }
    .stat .value { font-size: 20px; }

    .status-filters {
      flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
      padding-bottom: 6px; margin-bottom: 16px;
    }
    .status-filters a { flex: 0 0 auto; }

    .page-size { font-size: 12px; flex-wrap: wrap; }

    .product-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
    .pname { font-size: 12.5px; min-height: 2.4em; }
    .pactions { flex-direction: column; align-items: stretch; gap: 6px; }
    .review-btn, .view-link { text-align: center; padding: 9px 12px; }
    .view-link { border: 1px solid #eee; border-radius: 7px; }

    .pagination { flex-wrap: wrap; row-gap: 10px; }
    .pagination a { padding: 9px 16px; }

    /* Edit / review page */
    .section-card { padding: 16px; }
    .edit-topbar { flex-direction: column; align-items: stretch; gap: 12px; }
    .nav-buttons { justify-content: space-between; }
    .nav-btn { flex: 1; text-align: center; padding: 10px 12px; }

    .edit-image-tile { width: 100px; }
    .edit-images img { width: 100px; height: 100px; }

    .upload-form { flex-direction: column; align-items: stretch; gap: 8px; }
    .upload-btn { padding: 10px; }

    /* 16px prevents iOS Safari from auto-zooming in when a field is tapped */
    .field input[type=text], .field select { font-size: 16px; padding: 12px; }
    .color-picker label, .status-toggle label { padding: 8px 13px; }

    .save-row { flex-direction: column; align-items: stretch; }
    .save-btn, .view-post-btn { width: 100%; text-align: center; padding: 14px; }
  }
"""

TABS_TEMPLATE = """<div class="tabs">
  <a class="tab {home_active}" href="/">All brands</a>
  {tab_links}
</div>"""

PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="8">
<title>Catalog Progress</title>
<style>{style}</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="subtitle">auto-refreshes every 8s &middot; {total_posts} posts collected</div>
  {tabs}

  <div class="tips-banner">
    Click <strong>Review &amp; Edit</strong> on any product below to check its details, fix anything
    that's wrong, and press <strong>Save</strong>. Once you're inside a product you can use the
    <strong>Prev / Next</strong> buttons to move through the whole list without coming back here each time.
    Cards marked <span class="needs-review-flag inline">Needs review</span> need your attention first.
  </div>

  <div class="stats">
    <div class="stat"><div class="value">{total_posts}</div><div class="label">Posts collected</div></div>
    <div class="stat good"><div class="value">{total_products}</div><div class="label">Extracted</div></div>
    <div class="stat warn"><div class="value">{pending}</div><div class="label">Pending</div></div>
    <div class="stat"><div class="value">{is_product}</div><div class="label">Real products</div></div>
    <div class="stat warn"><div class="value">{review_required}</div><div class="label">Needs review</div></div>
    <div class="stat"><div class="value">{duplicates}</div><div class="label">Likely duplicates</div></div>
    <div class="stat good"><div class="value">{fully_ready}</div><div class="label">Fully ready</div></div>
    <div class="stat good"><div class="value">{available_count}</div><div class="label">Available</div></div>
    <div class="stat"><div class="value">{sold_out_count}</div><div class="label">Sold out</div></div>
    <div class="stat warn"><div class="value">{unknown_count}</div><div class="label">Unknown status</div></div>
  </div>

  <div class="progress-label"><span>Extraction progress</span><span class="pct">{pct}%</span></div>
  <div class="progress-bar"><div class="progress-fill" style="width: {pct}%"></div></div>

  <div class="progress-label"><span>Image cleaning progress (fully-ready products only) &middot; {image_cleaned_count}/{image_ready_total}</span><span class="pct img-pct">{image_clean_pct}%</span></div>
  <div class="progress-bar"><div class="progress-fill img-fill" style="width: {image_clean_pct}%"></div></div>

  {status_filters}

  <div class="page-size">Show: {page_size_links}</div>

  <div class="product-grid">
    {product_cards}
  </div>

  {pagination}
</body>
</html>
"""

HOME_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Catalog Progress</title>
<style>{style}</style>
</head>
<body>
  <h1>Catalog progress</h1>
  <div class="subtitle">auto-refreshes every 15s &middot; pick a brand below to start reviewing its products</div>
  {cards}
</body>
</html>
"""

PRODUCT_CARD_TEMPLATE = """<div class="product-card">
  {review_flag}
  <a class="thumb-wrap" href="/product/{product_id}?back={back}">{img}</a>
  <div class="pinfo">
    <div class="pname">{product_name}</div>
    <div class="pmeta">
      <span class="badge {availability}">{availability_label}</span>
      <span class="price">{price}</span>
    </div>
  </div>
  <div class="pactions">
    <a class="review-btn" href="/product/{product_id}?back={back}">Review &amp; Edit</a>
    <a class="view-link" href="{post_url}" target="_blank">View post</a>
  </div>
</div>"""

EDIT_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review product</title>
<style>{style}</style>
</head>
<body>
  <div class="edit-page">
    <div class="edit-topbar">
      <a class="back-link" href="{back_href}">&larr; back to {profile}</a>
      {nav_buttons}
    </div>
    <h1>{title}</h1>
    <div class="subtitle">Instagram post from {profile} &middot; {post_date}</div>

    {saved_banner}

    <div class="section-card">
      <div class="section-title">Photos</div>
      <div class="edit-images">{images_html}</div>
      <form class="upload-form" method="post" action="/product/{product_id}/media/upload" enctype="multipart/form-data">
        <input type="hidden" name="back" value="{back_href}">
        <input type="file" name="image" accept="image/*" required>
        <button type="submit" class="upload-btn">+ Add photo</button>
      </form>
    </div>

    <div class="section-card">
      <div class="section-title">Original Instagram caption</div>
      <div class="caption-box">{caption}</div>
    </div>

    <form method="post" action="/product/{product_id}/save">
      <input type="hidden" name="back" value="{back_href}">

      <div class="section-card">
        <div class="section-title">Product details</div>
        <div class="field">
          <label>Product name</label>
          <input type="text" name="product_name" value="{product_name}" placeholder="e.g. Zara Floral Midi Dress">
        </div>
        <div class="field">
          <label>Category</label>
          <select name="category">
            {category_options}
          </select>
        </div>
        <div class="field">
          <label>Price</label>
          <input type="text" name="price" value="{price}" placeholder="e.g. 150">
          <div class="hint">GHS. Numbers only -- no currency symbol or commas.</div>
        </div>
        <div class="field">
          <label>Sizes</label>
          <input type="text" name="sizes" value="{sizes}" placeholder="e.g. S, M, L or 38, 39, 40">
          <div class="hint">Comma-separated.</div>
        </div>
        <div class="field">
          <label>Colors</label>
          <div class="color-picker">
            {color_options}
          </div>
          <input type="text" class="custom-color-input" name="colors_custom" placeholder="Add other color(s), comma-separated" value="{colors_custom}">
          <div class="hint">Click a color to select it, or type new ones on the line below.</div>
        </div>
      </div>

      <div class="section-card">
        <div class="section-title">Availability</div>
        <div class="status-toggle">
          {status_options}
        </div>
      </div>

      <label class="still-review">
        <input type="checkbox" name="still_needs_review" {still_review_checked}>
        This product still needs another look after this edit (leave unchecked once it's good to go)
      </label>

      <div class="save-row">
        <button type="submit" class="save-btn">Save</button>
        <a class="view-post-btn" href="{post_url}" target="_blank">View original post &rarr;</a>
      </div>
    </form>

    <div class="edit-bottombar">{nav_buttons}</div>
  </div>
</body>
</html>
"""


def get_profiles(conn):
    return [r["profile"] for r in conn.execute(
        "SELECT DISTINCT profile FROM instagram_post ORDER BY profile"
    ).fetchall()]


FULLY_READY_SQL = """
             AND pr.review_required = 0
             AND pr.duplicate_of IS NULL
             AND pr.product_name IS NOT NULL
             AND pr.price IS NOT NULL
             AND pr.sizes IS NOT NULL
             AND pr.colors IS NOT NULL
             AND pr.availability_status IS NOT NULL
             AND pr.availability_status = 'AVAILABLE'
             AND (SELECT COUNT(*) FROM media m WHERE m.post_id = pr.post_id AND m.excluded = 0) > 2"""


def fully_ready_ids(conn, profile):
    rows = conn.execute(
        f"""SELECT pr.id FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? {FULLY_READY_SQL}""",
        (profile,),
    ).fetchall()
    return [r["id"] for r in rows]


def readiness_check(conn, product_id):
    """Same bar as FULLY_READY_SQL, but for one product and explaining
    exactly what's still missing -- used to give real feedback after a
    save instead of a generic "Saved." with no indication of whether the
    edit actually finished the job."""
    r = conn.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()
    if not r:
        return False, ["product not found"]

    media_count = conn.execute(
        "SELECT COUNT(*) FROM media WHERE post_id = ? AND excluded = 0", (r["post_id"],)
    ).fetchone()[0]

    missing = []
    if r["review_required"]:
        missing.append('"still needs review" is checked')
    if r["duplicate_of"] is not None:
        missing.append("flagged as a duplicate")
    if not r["product_name"]:
        missing.append("product name")
    if not r["price"]:
        missing.append("price")
    if not r["sizes"]:
        missing.append("sizes")
    if not r["colors"]:
        missing.append("colors")
    if r["availability_status"] != "AVAILABLE":
        missing.append(f'availability is {r["availability_status"] or "not set"}, not Available')
    if media_count <= 2:
        missing.append(f"only {media_count} photo(s) (need more than 2)")

    return (len(missing) == 0), missing


def has_cleaned_image(profile, product_id):
    product_dir = CLEAN_IMAGES_ROOT / profile / f"product_{product_id}"
    return product_dir.is_dir() and any(product_dir.glob("*.png"))


def image_cleaning_progress(conn, profile):
    ids = fully_ready_ids(conn, profile)
    if not ids:
        return 0, 0
    cleaned = sum(1 for pid in ids if has_cleaned_image(profile, pid))
    return cleaned, len(ids)


def ready_to_ship_ids(conn, profile):
    return [pid for pid in fully_ready_ids(conn, profile) if has_cleaned_image(profile, pid)]


FILTER_CLAUSES = {
    "all": "",
    "available": "AND pr.availability_status = 'AVAILABLE'",
    "sold_out": "AND pr.availability_status = 'SOLD_OUT'",
    "unknown": "AND (pr.availability_status IS NULL OR pr.availability_status = 'UNKNOWN')",
    "review": "AND pr.review_required = 1",
}
ID_FILTER_KEYS = ("fully_ready", "ready_to_ship")


def filtered_product_ids(conn, profile, status_filter):
    """Full list of product ids (newest first) matching one status filter --
    shared by the brand page (for counting/paging) and the edit page's
    Prev/Next nav, so both always agree on exactly the same ordered list."""
    if status_filter == "fully_ready":
        return sorted(fully_ready_ids(conn, profile), reverse=True)
    if status_filter == "ready_to_ship":
        return sorted(ready_to_ship_ids(conn, profile), reverse=True)
    clause = FILTER_CLAUSES.get(status_filter, "")
    rows = conn.execute(
        f"""SELECT pr.id FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? {clause} ORDER BY pr.id DESC""",
        (profile,),
    ).fetchall()
    return [row["id"] for row in rows]


def render_brand_page(profile: str):
    conn = get_conn()
    profiles = get_profiles(conn)
    if profile not in profiles:
        conn.close()
        abort(404)

    total_posts = conn.execute(
        "SELECT COUNT(*) FROM instagram_post WHERE profile = ?", (profile,)
    ).fetchone()[0]
    total_products = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ?""", (profile,)
    ).fetchone()[0]
    is_product = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.is_product_post = 1""", (profile,)
    ).fetchone()[0]
    review_required = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.review_required = 1""", (profile,)
    ).fetchone()[0]
    duplicates = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.duplicate_of IS NOT NULL""", (profile,)
    ).fetchone()[0]
    fully_ready = len(fully_ready_ids(conn, profile))
    available_count = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.availability_status = 'AVAILABLE'""", (profile,)
    ).fetchone()[0]
    sold_out_count = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.availability_status = 'SOLD_OUT'""", (profile,)
    ).fetchone()[0]
    unknown_count = conn.execute(
        """SELECT COUNT(*) FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND (pr.availability_status IS NULL OR pr.availability_status = 'UNKNOWN')""",
        (profile,),
    ).fetchone()[0]
    pending = max(total_posts - total_products, 0)
    pct = round((total_products / total_posts) * 100) if total_posts else 0

    image_cleaned_count, image_ready_total = image_cleaning_progress(conn, profile)
    image_clean_pct = round((image_cleaned_count / image_ready_total) * 100) if image_ready_total else 0
    ready_to_ship_count = image_cleaned_count  # same set: fully-ready AND image-cleaned

    status_filter = request.args.get("status", "all")
    valid_statuses = set(FILTER_CLAUSES) | set(ID_FILTER_KEYS)
    if status_filter not in valid_statuses:
        status_filter = "all"

    all_ids = filtered_product_ids(conn, profile, status_filter)
    visible_count = len(all_ids)

    page_size = request.args.get("size", DEFAULT_PAGE_SIZE, type=int)
    if page_size not in ALLOWED_PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE
    total_pages = max((visible_count + page_size - 1) // page_size, 1)
    page = request.args.get("page", 1, type=int)
    page = min(max(page, 1), total_pages)
    offset = (page - 1) * page_size
    page_ids = all_ids[offset:offset + page_size]

    if page_ids:
        placeholders = ",".join("?" for _ in page_ids)
        rows = conn.execute(
            f"""SELECT pr.*, p.post_url FROM product pr
               JOIN instagram_post p ON p.id = pr.post_id
               WHERE pr.id IN ({placeholders})""", page_ids
        ).fetchall()
        rows_by_id = {row["id"]: row for row in rows}
        products = [rows_by_id[i] for i in page_ids if i in rows_by_id]
    else:
        products = []

    back_url = quote(f"/brand/{profile}?page={page}&size={page_size}&status={status_filter}", safe="")

    cards_html = []
    for r in products:
        media = conn.execute(
            "SELECT id FROM media WHERE post_id = ? AND excluded = 0 ORDER BY position LIMIT 1", (r["post_id"],)
        ).fetchone()
        img_tag = (
            f'<img class="thumb" src="/media/{media["id"]}" loading="lazy">'
            if media else '<div class="thumb-empty">No photo yet</div>'
        )
        availability = r["availability_status"] or "UNKNOWN"
        cards_html.append(PRODUCT_CARD_TEMPLATE.format(
            review_flag='<div class="needs-review-flag">Needs review</div>' if r["review_required"] else "",
            img=img_tag,
            product_name=r["product_name"] or "(no name yet)",
            availability=availability,
            availability_label=STATUS_LABELS.get(availability, availability),
            price=f'GHS {r["price"]}' if r["price"] else "No price yet",
            post_url=r["post_url"],
            product_id=r["id"],
            back=back_url,
        ))

    tab_links = "\n".join(
        f'<a class="tab {"active" if p == profile else ""}" href="/brand/{quote(p)}">{p}</a>'
        for p in profiles
    )
    tabs = TABS_TEMPLATE.format(home_active="", tab_links=tab_links)

    prev_href = f"/brand/{quote(profile)}?page={page-1}&size={page_size}&status={status_filter}"
    next_href = f"/brand/{quote(profile)}?page={page+1}&size={page_size}&status={status_filter}"
    prev_disabled = "disabled" if page <= 1 else ""
    next_disabled = "disabled" if page >= total_pages else ""
    pagination = (
        f'<div class="pagination">'
        f'<a class="{prev_disabled}" href="{prev_href}">&larr; Prev</a>'
        f'<span>Page {page} of {total_pages} ({visible_count} shown)</span>'
        f'<a class="{next_disabled}" href="{next_href}">Next &rarr;</a>'
        f'</div>'
    )

    page_size_links = " &middot; ".join(
        f'<a class="{"active" if s == page_size else ""}" href="/brand/{quote(profile)}?page=1&size={s}&status={status_filter}">{s}/page</a>'
        for s in ALLOWED_PAGE_SIZES
    )

    status_labels = [
        ("all", f"All ({total_products})"),
        ("available", f"Available ({available_count})"),
        ("sold_out", f"Sold out ({sold_out_count})"),
        ("unknown", f"Unknown status ({unknown_count})"),
        ("review", f"Needs review ({review_required})"),
        ("fully_ready", f"Fully ready ({fully_ready})"),
        ("ready_to_ship", f"Ready to ship ({ready_to_ship_count})"),
    ]
    status_filters_html = "\n".join(
        f'<a class="{"active" if key == status_filter else ""}" '
        f'href="/brand/{quote(profile)}?page=1&size={page_size}&status={key}">{label}</a>'
        for key, label in status_labels
    )
    status_filters = f'<div class="status-filters">{status_filters_html}</div>'

    conn.close()
    return PAGE_TEMPLATE.format(
        style=STYLE,
        title=profile,
        tabs=tabs,
        total_posts=total_posts,
        total_products=total_products,
        pending=pending,
        is_product=is_product,
        review_required=review_required,
        duplicates=duplicates,
        fully_ready=fully_ready,
        available_count=available_count,
        sold_out_count=sold_out_count,
        unknown_count=unknown_count,
        status_filters=status_filters,
        pct=pct,
        image_cleaned_count=image_cleaned_count,
        image_ready_total=image_ready_total,
        image_clean_pct=image_clean_pct,
        product_cards="\n".join(cards_html) or "<p>No products in this view.</p>",
        pagination=pagination,
        page_size_links=page_size_links,
    )


@app.route("/")
def home():
    conn = get_conn()
    profiles = get_profiles(conn)
    cards = []
    for p in profiles:
        total_posts = conn.execute(
            "SELECT COUNT(*) FROM instagram_post WHERE profile = ?", (p,)
        ).fetchone()[0]
        total_products = conn.execute(
            """SELECT COUNT(*) FROM product pr JOIN instagram_post ip ON ip.id = pr.post_id
               WHERE ip.profile = ?""", (p,)
        ).fetchone()[0]
        cards.append(
            f'<a class="brand-card" href="/brand/{quote(p)}">'
            f'<div class="name"><span>{p}</span><span>&rarr;</span></div>'
            f'<div class="meta">{total_posts} posts collected &middot; {total_products} extracted</div>'
            f'</a>'
        )
    conn.close()
    return HOME_TEMPLATE.format(style=STYLE, cards="\n".join(cards) or "<p>No posts collected yet.</p>")


@app.route("/brand/<profile>")
def brand(profile):
    return render_brand_page(profile)


STATUS_OPTIONS = ["AVAILABLE", "SOLD_OUT", "UNKNOWN", "NOT_A_PRODUCT"]
STATUS_LABELS = {
    "AVAILABLE": "Available",
    "SOLD_OUT": "Sold out",
    "UNKNOWN": "Unknown",
    "NOT_A_PRODUCT": "Not a product",
}


def _names_from_colors(raw_colors):
    if not raw_colors:
        return []
    try:
        parsed = json.loads(raw_colors)
    except (json.JSONDecodeError, TypeError):
        return [raw_colors]
    if not isinstance(parsed, list):
        return [str(parsed)]
    names = []
    for item in parsed:
        if isinstance(item, dict):
            names.append(item.get("name", ""))
        elif item:
            names.append(str(item))
    return [n for n in names if n]


def _list_from_csv(text):
    items = [s.strip() for s in (text or "").split(",")]
    return [s for s in items if s]


def _existing_colors_for_profile(conn, profile, limit=80):
    rows = conn.execute(
        """SELECT pr.colors FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE p.profile = ? AND pr.colors IS NOT NULL""",
        (profile,),
    ).fetchall()
    # Extracted color names are messy/inconsistently cased across records
    # ("Beige" vs "beige" vs "Beige/Cream") -- group case-insensitively and
    # keep whichever exact casing showed up most often as the canonical one,
    # then cap the list so the picker stays usable on brands with lots of history.
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        for name in _names_from_colors(row["colors"]):
            key = name.lower()
            bucket = counts.setdefault(key, {})
            bucket[name] = bucket.get(name, 0) + 1

    canonical = []
    for key, variants in counts.items():
        best_name = max(variants, key=variants.get)
        total = sum(variants.values())
        canonical.append((best_name, total))

    canonical.sort(key=lambda item: (-item[1], item[0].lower()))
    return sorted((name for name, _ in canonical[:limit]), key=str.lower)


def _parse_back(back_raw, fallback_profile):
    """Pull the profile + status filter back out of a stored back-link like
    /brand/chicstyle.ghana?page=2&size=50&status=review, so Prev/Next on the
    edit page can walk the exact same filtered list the reviewer was
    browsing instead of falling back to "all"."""
    if not back_raw:
        return fallback_profile, "all"
    parsed = urlparse(back_raw)
    qs = parse_qs(parsed.query)
    profile = fallback_profile
    if parsed.path.startswith("/brand/"):
        profile = parsed.path[len("/brand/"):] or fallback_profile
    status = qs.get("status", ["all"])[0]
    return profile, status


def _nav_buttons_html(prev_href, next_href, position_label):
    prev = (
        f'<a class="nav-btn" href="{prev_href}">&larr; Prev</a>' if prev_href
        else '<span class="nav-btn disabled">&larr; Prev</span>'
    )
    nxt = (
        f'<a class="nav-btn" href="{next_href}">Next &rarr;</a>' if next_href
        else '<span class="nav-btn disabled">Next &rarr;</span>'
    )
    pos = f'<span class="nav-position">{position_label}</span>' if position_label else ""
    return f'<div class="nav-buttons">{prev}{pos}{nxt}</div>'


@app.route("/product/<int:product_id>")
def product_edit(product_id):
    conn = get_conn()
    r = conn.execute(
        """SELECT pr.*, p.post_url, p.caption, p.post_date, p.profile
           FROM product pr JOIN instagram_post p ON p.id = pr.post_id
           WHERE pr.id = ?""",
        (product_id,),
    ).fetchone()
    if not r:
        conn.close()
        abort(404)

    media_rows = conn.execute(
        "SELECT id FROM media WHERE post_id = ? AND excluded = 0 ORDER BY position", (r["post_id"],)
    ).fetchall()
    known_colors = _existing_colors_for_profile(conn, r["profile"])
    is_ready, missing = readiness_check(conn, product_id)

    back_param = request.args.get("back", "")
    nav_profile, nav_status = _parse_back(back_param, r["profile"])
    nav_ids = filtered_product_ids(conn, nav_profile, nav_status)
    conn.close()

    if product_id in nav_ids:
        idx = nav_ids.index(product_id)
        position_label = f"{idx + 1} of {len(nav_ids)} in this view"
        prev_id = nav_ids[idx - 1] if idx > 0 else None
        next_id = nav_ids[idx + 1] if idx < len(nav_ids) - 1 else None
    else:
        position_label = ""
        prev_id = next_id = None

    def _nav_link(pid):
        return f"/product/{pid}?back={quote(back_param, safe='')}" if pid else None

    prev_href = _nav_link(prev_id)
    next_href = _nav_link(next_id)
    nav_buttons = _nav_buttons_html(prev_href, next_href, position_label)

    images_html = "\n".join(
        f'<div class="edit-image-tile">'
        f'<img src="/media/{m["id"]}">'
        f'<form method="post" action="/product/{product_id}/media/{m["id"]}/exclude">'
        f'<input type="hidden" name="back" value="{back_param}">'
        f'<button type="submit" class="edit-image-remove" title="Remove photo" '
        f'onclick="return confirm(\'Remove this photo?\')">&times;</button>'
        f'</form></div>'
        for m in media_rows
    ) or "<p>No images.</p>"

    sizes_display = ", ".join(json.loads(r["sizes"])) if r["sizes"] else ""

    current_status = r["availability_status"] or "UNKNOWN"
    status_options_html = "\n".join(
        f'<label class="opt-{s}"><input type="radio" name="availability_status" value="{s}" '
        f'{"checked" if s == current_status else ""}><span>{STATUS_LABELS[s]}</span></label>'
        for s in STATUS_OPTIONS
    )

    profile_categories = CATEGORY_SETS.get(r["profile"], [])
    current_category = r["category"] or ""
    all_categories = list(profile_categories)
    if current_category and current_category not in all_categories:
        all_categories.append(current_category)
    category_options_html = '<option value="">(none)</option>\n' + "\n".join(
        f'<option value="{c}" {"selected" if c == current_category else ""}>{c}</option>'
        for c in all_categories
    )

    current_colors = _names_from_colors(r["colors"])
    current_colors_lower = {c.lower() for c in current_colors}
    known_colors_lower = {c.lower() for c in known_colors}
    color_options_html = "\n".join(
        f'<label><input type="checkbox" name="colors_select" value="{c}" '
        f'{"checked" if c.lower() in current_colors_lower else ""}><span>{c}</span></label>'
        for c in known_colors
    )
    colors_custom_display = ", ".join(c for c in current_colors if c.lower() not in known_colors_lower)

    back_href = request.args.get("back") or f"/brand/{r['profile']}"
    saved = request.args.get("saved") == "1"
    if saved:
        next_cta = f' <a href="{next_href}">Next product &rarr;</a>' if next_href else ""
        if is_ready:
            saved_banner = (
                '<div class="saved-banner ready">Saved. This product is now '
                f'<strong>Fully Ready</strong>. <a href="{back_href}">&larr; Back to list</a>{next_cta}</div>'
            )
        else:
            missing_list = ", ".join(missing)
            saved_banner = (
                f'<div class="saved-banner not-ready">Saved, but still not Fully Ready. '
                f'Missing: <strong>{missing_list}</strong>. <a href="{back_href}">&larr; Back to list</a>{next_cta}</div>'
            )
    else:
        saved_banner = ""

    return EDIT_PAGE_TEMPLATE.format(
        style=STYLE,
        back_href=back_href,
        nav_buttons=nav_buttons,
        profile=r["profile"],
        title=r["product_name"] or "(no name yet)",
        post_date=r["post_date"] or "",
        saved_banner=saved_banner,
        images_html=images_html,
        caption=r["caption"] or "(no caption)",
        product_id=product_id,
        product_name=r["product_name"] or "",
        category_options=category_options_html,
        price=r["price"] or "",
        sizes=sizes_display,
        color_options=color_options_html,
        colors_custom=colors_custom_display,
        status_options=status_options_html,
        still_review_checked="checked" if r["review_required"] else "",
        post_url=r["post_url"],
    )


@app.route("/product/<int:product_id>/save", methods=["POST"])
def product_save(product_id):
    conn = get_conn()
    existing = conn.execute("SELECT colors FROM product WHERE id = ?", (product_id,)).fetchone()
    if not existing:
        conn.close()
        abort(404)

    existing_hex_by_name = {}
    if existing["colors"]:
        try:
            for item in json.loads(existing["colors"]):
                if isinstance(item, dict) and item.get("name"):
                    existing_hex_by_name[item["name"]] = item.get("hex")
        except (json.JSONDecodeError, TypeError):
            pass

    product_name = request.form.get("product_name", "").strip() or None
    category = request.form.get("category", "").strip() or None
    price = request.form.get("price", "").strip() or None
    sizes = _list_from_csv(request.form.get("sizes"))

    selected_colors = request.form.getlist("colors_select")
    custom_colors = _list_from_csv(request.form.get("colors_custom"))
    seen = set()
    colors = []
    for c in selected_colors + custom_colors:
        if c not in seen:
            seen.add(c)
            colors.append(c)

    availability_status = request.form.get("availability_status", "UNKNOWN")
    if availability_status not in STATUS_OPTIONS:
        availability_status = "UNKNOWN"
    is_product_post = 0 if availability_status == "NOT_A_PRODUCT" else 1
    still_needs_review = 1 if request.form.get("still_needs_review") == "on" else 0

    conn.execute(
        """UPDATE product SET
             product_name = ?, category = ?, price = ?,
             sizes = ?, colors = ?, availability_status = ?, is_product_post = ?, review_required = ?
           WHERE id = ?""",
        (
            product_name,
            category,
            price,
            json.dumps(sizes) if sizes else None,
            json.dumps([{"name": c, "hex": existing_hex_by_name.get(c)} for c in colors]) if colors else None,
            availability_status,
            is_product_post,
            still_needs_review,
            product_id,
        ),
    )
    conn.commit()
    conn.close()

    # Always land back on this product's page first to show whether the edit
    # actually made it Fully Ready -- jumping straight back to the list (the
    # old behavior) meant saving never confirmed whether it worked.
    back = request.form.get("back", "")
    return redirect(f"/product/{product_id}?saved=1&back={quote(back, safe='')}")


@app.route("/product/<int:product_id>/media/<int:media_id>/exclude", methods=["POST"])
def product_media_exclude(product_id, media_id):
    conn = get_conn()
    # Soft-delete only -- the raw Instagram archive is never touched, this
    # just stops the image from being shown, cleaned, uploaded, or counted
    # toward "fully ready". Reversible by hand in the DB if ever needed.
    conn.execute(
        """UPDATE media SET excluded = 1
           WHERE id = ? AND post_id = (SELECT post_id FROM product WHERE id = ?)""",
        (media_id, product_id),
    )
    conn.commit()
    conn.close()
    back = request.form.get("back", "")
    return redirect(f"/product/{product_id}?back={quote(back, safe='')}")


@app.route("/product/<int:product_id>/media/upload", methods=["POST"])
def product_media_upload(product_id):
    conn = get_conn()
    row = conn.execute("SELECT post_id FROM product WHERE id = ?", (product_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    post_id = row["post_id"]

    file = request.files.get("image")
    back = request.form.get("back", "")
    if not file or not file.filename:
        conn.close()
        return redirect(f"/product/{product_id}?back={quote(back, safe='')}")

    upload_dir = DATA_DIR / "reviewer_uploads" / str(post_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    dest = upload_dir / f"{uuid.uuid4().hex}{ext}"
    file.save(dest)

    next_position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM media WHERE post_id = ?", (post_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO media (post_id, position, local_path, added_by_reviewer) VALUES (?, ?, ?, 1)",
        (post_id, next_position, str(dest)),
    )
    conn.commit()
    conn.close()
    return redirect(f"/product/{product_id}?back={quote(back, safe='')}")


def resolve_media_path(stored_path: str) -> Path:
    """Stored local_path values are absolute paths from whatever machine
    collected them (Ted's Mac in dev). On a different machine (Railway)
    that exact path won't exist -- fall back to re-joining everything
    after the last "data/" segment onto this environment's DATA_DIR."""
    path = Path(stored_path)
    if path.exists():
        return path
    marker = "data/"
    idx = stored_path.replace("\\", "/").rfind(marker)
    if idx != -1:
        return DATA_DIR / stored_path[idx + len(marker):]
    return path


@app.route("/media/<int:media_id>")
def media(media_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT m.local_path, m.position, m.post_id, p.profile
           FROM media m JOIN instagram_post p ON p.id = m.post_id
           WHERE m.id = ?""", (media_id,),
    ).fetchone()
    if not row:
        conn.close()
        abort(404)
    product_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM product WHERE post_id = ?", (row["post_id"],)
        ).fetchall()
    ]
    conn.close()

    # Prefer the cleaned (background-removed) version when one has been
    # generated for this post's product(s) -- same source image, same
    # position number, just cleaner. Falls back to the raw original.
    for pid in product_ids:
        clean_path = CLEAN_IMAGES_ROOT / row["profile"] / f"product_{pid}" / f"{row['position']}.png"
        if clean_path.exists():
            return send_file(clean_path)

    path = resolve_media_path(row["local_path"])
    if not path.exists():
        abort(404)
    return send_file(path)


if __name__ == "__main__":
    # 0.0.0.0 + $PORT for Railway; localhost:5050 unchanged for local dev
    # since PORT is unset there.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    port = int(os.environ.get("PORT", 5050))
    app.run(host=host, port=port, debug=False)
