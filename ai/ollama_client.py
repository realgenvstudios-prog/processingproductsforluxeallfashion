"""Thin wrapper around the local Ollama HTTP API. No accounts, no external calls."""
import base64
import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma4:e2b"


def generate(prompt: str, images: list[str] = None, format_json: bool = False, max_tokens: int = None) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if images:
        payload["images"] = [base64.b64encode(open(p, "rb").read()).decode() for p in images]
    if format_json:
        payload["format"] = "json"
    if max_tokens:
        payload["options"] = {"num_predict": max_tokens}

    resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["response"]
