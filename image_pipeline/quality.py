"""
Heuristic QC for background-removed images. rembg gives no confidence score,
so we infer failure from the alpha channel itself: if almost nothing was
removed, or almost everything was, the segmentation didn't find a real
foreground/background split.

Usage:
    python -m image_pipeline.quality --input data/clean_images
Writes data/image_qc_report.csv
"""
import argparse
import csv
from pathlib import Path

import numpy as np
from PIL import Image

FOREGROUND_RATIO_TOO_HIGH = 0.92   # almost nothing removed -> probably not a clean product shot
FOREGROUND_RATIO_TOO_LOW = 0.02    # almost everything removed -> product likely gone too
TINY_BBOX_RATIO = 0.03             # remaining product occupies too little of the canvas
BORDER_TOUCH_MARGIN_PX = 3         # how close to the edge counts as "touching"


def analyze(path: Path) -> dict:
    im = Image.open(path)
    if im.mode != "RGBA":
        return {"file": path.name, "status": "FAILED", "reason": "no alpha channel", "foreground_ratio": None}

    alpha = np.array(im.getchannel("A"))
    h, w = alpha.shape
    fg_mask = alpha > 128
    fg_ratio = fg_mask.sum() / (h * w)

    reasons = []
    status = "AUTO_APPROVED"

    if fg_ratio >= FOREGROUND_RATIO_TOO_HIGH:
        status = "FAILED"
        reasons.append(f"foreground ratio {fg_ratio:.2f} too high -- background likely not removed")
    elif fg_ratio <= FOREGROUND_RATIO_TOO_LOW:
        status = "FAILED"
        reasons.append(f"foreground ratio {fg_ratio:.2f} too low -- product likely removed with background")
    else:
        rows = np.any(fg_mask, axis=1)
        cols = np.any(fg_mask, axis=0)
        y0, y1 = np.where(rows)[0][[0, -1]]
        x0, x1 = np.where(cols)[0][[0, -1]]
        bbox_area = (y1 - y0) * (x1 - x0)
        bbox_ratio = bbox_area / (h * w)

        if bbox_ratio < TINY_BBOX_RATIO:
            status = "REVIEW_REQUIRED"
            reasons.append(f"product bounding box only {bbox_ratio:.3f} of canvas -- may be too small/cropped wrong")

        touches_border = (
            y0 <= BORDER_TOUCH_MARGIN_PX or y1 >= h - BORDER_TOUCH_MARGIN_PX or
            x0 <= BORDER_TOUCH_MARGIN_PX or x1 >= w - BORDER_TOUCH_MARGIN_PX
        )
        if touches_border:
            status = "REVIEW_REQUIRED" if status == "AUTO_APPROVED" else status
            reasons.append("product touches image edge -- may be cropped off")

    return {
        "file": path.name,
        "status": status,
        "reason": "; ".join(reasons) if reasons else "",
        "foreground_ratio": round(float(fg_ratio), 4),
    }


def run(input_dir: Path, report_path: Path):
    results = []
    for path in sorted(input_dir.glob("*.png")):
        results.append(analyze(path))

    with open(report_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "status", "reason", "foreground_ratio"])
        writer.writeheader()
        writer.writerows(results)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{len(results)} image(s) checked: {counts}")
    print(f"report written to {report_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/clean_images")
    parser.add_argument("--report", default="data/image_qc_report.csv")
    args = parser.parse_args()
    run(Path(args.input), Path(args.report))
