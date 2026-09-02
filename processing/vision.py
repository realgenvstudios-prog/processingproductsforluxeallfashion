"""Local vision analysis of a single product image via Ollama."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.ollama_client import generate

VISION_PROMPT = """Look at this image from an Instagram post selling a fashion product (shoes, clothing, or accessories).

In 3-4 short sentences ONLY, state whatever of this you can actually see:
- product type, visible brand/logo text (read exactly as printed), color(s), visible text (sizes/price/labels), and whether it's a retail website screenshot / physical product photo / person holding-wearing it.

Be factual and terse -- no preamble, no restating the question, no speculation about anything not visible."""


def describe_image(path: str) -> str:
    try:
        return generate(VISION_PROMPT, images=[path], max_tokens=200).strip()
    except Exception as e:
        return f"[vision analysis failed: {e}]"
