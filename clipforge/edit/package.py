"""The folder a finished clip lives in, and the zip the dashboard hands out."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path


def folder(out_dir: Path, campaign_name: str, clip_id: int) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", campaign_name.lower()).strip("-")[:40] or "campaign"
    d = out_dir / slug / str(clip_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(dest: Path, *, clip: Path, thumb: Path, meta: dict) -> Path:
    """meta.json and package.zip beside the clip. Returns the zip's path."""
    meta_path = dest / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    zip_path = dest / "package.zip"
    # The mp4 is already compressed; storing it keeps the zip fast to write and open.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for path in (clip, thumb, meta_path):
            z.write(path, arcname=path.name)
    return zip_path
