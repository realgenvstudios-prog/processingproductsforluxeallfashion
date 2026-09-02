"""
Full V2 image pipeline: classify -> background-remove (only where appropriate)
-> quality-check. Originals are never touched. Rerun-safe: skips images
already in the manifest.

Usage:
    python -m image_pipeline.pipeline --input data/raw_images --output data/clean_images
Writes data/image_manifest.csv
"""
import argparse
import csv
from pathlib import Path

from rembg import remove, new_session
from PIL import Image

from image_pipeline.classify import classify_image
from image_pipeline.quality import analyze as qc_analyze

DEFAULT_MODEL = "isnet-general-use"
FIELDNAMES = ["file", "category", "reasoning", "remove_background", "output_file", "qc_status", "qc_reason", "foreground_ratio"]


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    with open(manifest_path) as f:
        return {row["file"]: row for row in csv.DictReader(f)}


def save_manifest(manifest_path: Path, rows: dict):
    with open(manifest_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows.values():
            writer.writerow(row)


def run(input_dir: Path, output_dir: Path, manifest_path: Path, model: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    session = new_session(model)
    manifest = load_manifest(manifest_path)

    images = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    )
    print(f"{len(images)} image(s) found in {input_dir}")

    for i, path in enumerate(images):
        if path.name in manifest:
            print(f"[{i+1}/{len(images)}] skip (already in manifest): {path.name}")
            continue

        print(f"[{i+1}/{len(images)}] classifying: {path.name}...")
        cls = classify_image(str(path))
        row = {
            "file": path.name,
            "category": cls["category"],
            "reasoning": cls["reasoning"],
            "remove_background": cls["remove_background"],
            "output_file": "",
            "qc_status": "",
            "qc_reason": "",
            "foreground_ratio": "",
        }

        if cls["remove_background"]:
            out_path = output_dir / f"{path.stem}.png"
            try:
                output_bytes = remove(path.read_bytes(), session=session)
                out_path.write_bytes(output_bytes)
                qc = qc_analyze(out_path)
                row.update({
                    "output_file": out_path.name,
                    "qc_status": qc["status"],
                    "qc_reason": qc["reason"],
                    "foreground_ratio": qc["foreground_ratio"],
                })
                print(f"  -> {cls['category']}, background removed, QC={qc['status']}")
            except Exception as e:
                row.update({"qc_status": "FAILED", "qc_reason": f"processing error: {e}"})
                print(f"  -> FAILED: {e}")
        else:
            out_path = output_dir / f"{path.stem}.png"
            try:
                Image.open(path).convert("RGB").save(out_path, "PNG")
                row.update({
                    "output_file": out_path.name,
                    "qc_status": "NOT_PROCESSED",
                    "qc_reason": f"category={cls['category']}, background removal skipped",
                })
                print(f"  -> {cls['category']}, background removal skipped")
            except Exception as e:
                row.update({"qc_status": "FAILED", "qc_reason": f"copy error: {e}"})

        manifest[path.name] = row
        save_manifest(manifest_path, manifest)

    counts = {}
    for row in manifest.values():
        counts[row["qc_status"]] = counts.get(row["qc_status"], 0) + 1
    print(f"\ndone. {counts}")
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/raw_images")
    parser.add_argument("--output", default="data/clean_images")
    parser.add_argument("--manifest", default="data/image_manifest.csv")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    run(Path(args.input), Path(args.output), Path(args.manifest), args.model)
