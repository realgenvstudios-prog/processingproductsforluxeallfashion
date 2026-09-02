"""
Combines caption + per-image OCR + per-image vision descriptions into one
structured product record via a local Ollama call. Never hallucinates:
unknown fields must be null.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.ollama_client import generate

# Per-brand fixed category sets, matching how the target website manages
# categories per storefront. Profiles not listed here keep free-form
# category text (whatever short label best fits).
CATEGORY_SETS = {
    "og_luxemen": ["Shirts", "Polos", "Footwear", "Trousers", "Accessories"],
    "chicstyle.ghana": ["Sandals", "Heels", "Flats & Loafers", "Sneakers", "Accessories"],
    "kiddies_spacegh": ["Tops", "Trousers", "Dresses & Sets", "Footwear", "Accessories"],
}

EXTRACTION_PROMPT = """You are extracting structured product data for an e-commerce catalog from one Instagram post belonging to a Ghanaian fashion resale account (shoes/clothing).

Combine ALL evidence below. Do not invent anything. If a field cannot be determined from the evidence, use null.

IMPORTANT caption convention used by this seller: a line like "AVAILABLE : 40" or "AVAILABLE: 38" means the shoe SIZE 40 (or 38) is the one in stock -- the number is a SIZE, not just a generic in-stock flag. Always parse a number following "AVAILABLE" as an entry in "sizes" when it looks like a plausible clothing/shoe size (e.g. 34-46 for shoes). Still also set availability_status normally.
{category_instruction}
=== CAPTION ===
{caption}

=== IMAGE EVIDENCE (per image: OCR text extracted from the image, plus a vision model's description) ===
{image_evidence}

=== YOUR TASK ===
Return ONLY a JSON object (no markdown, no commentary) with exactly this shape:

{{
  "is_product_post": true or false,
  "product_name": string or null,  // CONSTRUCT a short descriptive name from brand + category + visible details (e.g. "ALDO Floral Pumps", "Zebra Print Sandals with Gold Accents") whenever you have enough evidence for one -- this is normal product naming, not hallucination. Only use null if there truly isn't enough evidence to describe what the product even is (e.g. non-product post, or image/caption both uninformative).
  "brand": string or null,
  "category": string or null,
  "description": string or null,
  "price": string or null,
  "currency": string or null,
  "sizes": [list of strings] or null,
  "colors": [{{"name": string, "hex": "#RRGGBB"}}, ...] or null,  // one entry per distinct color you can identify. "name" stays descriptive (e.g. "light blue/gray" is fine). "hex" is YOUR best-match hex swatch for that color as actually seen in the images -- always fill it in whenever you name a color, using standard color knowledge (e.g. gold ~ #D4AF37, coral ~ #FF7F50).
  "availability_status": one of "AVAILABLE", "SOLD_OUT", "RESERVED", "PREORDER", "UNKNOWN",
  "conflicts": [list of short strings describing any contradictions between caption and image evidence, e.g. "price: caption says X, image says Y"] or [],
  "confidence": {{
    "product_name": 0.0-1.0,
    "brand": 0.0-1.0,
    "price": 0.0-1.0,
    "availability_status": 0.0-1.0
  }},
  "review_required": true or false,
  "review_reason": string or null
}}

Set is_product_post to false if this is not actually about a specific sellable product (e.g. a giveaway, testimonial, announcement).
Set review_required to true if confidence on any important field is low, or there are conflicts, or availability is ambiguous (e.g. "last one left" should be treated as low stock, not automatically SOLD_OUT, and should be flagged for review).
Currency in this market is typically Ghanaian Cedi (GHS/GHC/GH₵) unless evidence says otherwise.
"""


def build_image_evidence(media_rows) -> str:
    if not media_rows:
        return "(no images available)"
    parts = []
    for i, m in enumerate(media_rows):
        ocr = m["ocr_text"] or "(none)"
        vision = m["vision_description"] or "(none)"
        parts.append(f"--- Image {i} ---\nOCR: {ocr}\nVision: {vision}")
    return "\n\n".join(parts)


def extract_product(caption: str, media_rows, profile: str = None) -> dict:
    category_instruction = ""
    categories = CATEGORY_SETS.get(profile)
    if categories:
        options = ", ".join(f'"{c}"' for c in categories)
        category_instruction = (
            f'\nFor "category", choose exactly one of: {options}. '
            f'If the item genuinely does not fit any of these, use null and set review_required to true.\n'
        )

    prompt = EXTRACTION_PROMPT.format(
        caption=caption or "(no caption)",
        image_evidence=build_image_evidence(media_rows),
        category_instruction=category_instruction,
    )
    raw = generate(prompt, format_json=True, max_tokens=500)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {
            "is_product_post": None,
            "review_required": True,
            "review_reason": "extraction JSON parse failure",
            "raw_response": raw,
        }
