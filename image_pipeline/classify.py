"""
Classifies a raw image before it's allowed anywhere near background removal.
Uses the same local gemma4:e2b model as the catalog pipeline's vision step --
no extra model download needed.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.ollama_client import generate

CATEGORIES = ["PRODUCT", "MODEL_PRODUCT", "LIFESTYLE", "PROMOTIONAL", "GRAPHIC", "UNKNOWN"]

# Only these categories are worth running through background removal --
# a lifestyle scene, a promo graphic, or a text graphic has no clean
# product/background split for rembg to find.
REMOVE_BACKGROUND_FOR = {"PRODUCT", "MODEL_PRODUCT"}

CLASSIFY_PROMPT = """Look at this image from a fashion resale Instagram account (shoes/clothing).

Classify it into exactly one of these categories:
- PRODUCT: a clean/isolated shot of the product itself (on a plain background, a retail website screenshot, or held up against a simple background), no person's face/body wearing it
- MODEL_PRODUCT: a person is wearing or holding the product prominently, product is the clear subject
- LIFESTYLE: a general scene/photo where the product isn't the clear isolated subject
- PROMOTIONAL: a marketing graphic, banner, sale announcement
- GRAPHIC: mostly text, a graphic/illustration, not a photo
- UNKNOWN: none of the above fit, or the image is unclear

Return ONLY a JSON object, no markdown, no commentary:
{"category": one of the above strings, "reasoning": short string}
"""


def classify_image(path: str) -> dict:
    try:
        raw = generate(CLASSIFY_PROMPT, images=[path], format_json=True)
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(match.group(0)) if match else {}
    except Exception as e:
        result = {"category": "UNKNOWN", "reasoning": f"classification failed: {e}"}

    category = result.get("category", "UNKNOWN")
    if category not in CATEGORIES:
        category = "UNKNOWN"

    return {
        "category": category,
        "reasoning": result.get("reasoning", ""),
        "remove_background": category in REMOVE_BACKGROUND_FOR,
    }
