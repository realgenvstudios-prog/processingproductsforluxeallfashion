"""
V1: batch background removal, nothing more. Takes a folder of raw product
images and writes transparent PNGs to an output folder. Originals are never
touched. Idempotent -- rerunning skips images already cleaned.

Usage:
    python -m image_pipeline.clean --input data/raw_images --output data/clean_images [--model isnet-general-use]
"""
import argparse
from pathlib import Path

from rembg import remove, new_session

DEFAULT_MODEL = "isnet-general-use"


def clean_folder(input_dir: Path, output_dir: Path, model: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    session = new_session(model)

    images = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    )
    print(f"{len(images)} image(s) found in {input_dir}")

    for i, path in enumerate(images):
        out_path = output_dir / f"{path.stem}.png"
        if out_path.exists():
            print(f"[{i+1}/{len(images)}] skip (already cleaned): {path.name}")
            continue
        try:
            with open(path, "rb") as f:
                input_bytes = f.read()
            output_bytes = remove(input_bytes, session=session)
            out_path.write_bytes(output_bytes)
            print(f"[{i+1}/{len(images)}] cleaned: {path.name} -> {out_path.name}")
        except Exception as e:
            print(f"[{i+1}/{len(images)}] FAILED: {path.name}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/raw_images")
    parser.add_argument("--output", default="data/clean_images")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    clean_folder(Path(args.input), Path(args.output), args.model)
