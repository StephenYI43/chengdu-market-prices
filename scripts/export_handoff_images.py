"""Export images used by the running project without a registry or PowerShell binary pipe."""

import argparse
import gzip
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/handoff-build")
    parser.add_argument("--version", default="20261125")
    args = parser.parse_args()
    folder = args.output_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    images = {}
    for role, service, image_name in [
        ("app", "api", "chengdu-market-prices-app"),
        ("forecast", "forecast", "chengdu-market-prices-forecast"),
        ("kafka", "kafka", "chengdu-market-prices-kafka"),
    ]:
        container = subprocess.check_output(
            ["docker", "compose", "ps", "-q", service], cwd=ROOT, text=True
        ).strip()
        image_id = subprocess.check_output(
            ["docker", "inspect", container, "--format", "{{.Image}}"], text=True
        ).strip()
        tag = image_name + ":handoff-" + args.version
        info = json.loads(
            subprocess.check_output(["docker", "image", "inspect", image_id], text=True)
        )[0]
        images[role] = {
            "tag": tag,
            "id": image_id,
            "architecture": info["Architecture"],
            "os": info["Os"],
            "uncompressed_bytes": info["Size"],
        }
    destination = folder / "images.tar.gz"
    metadata = folder / "images.json"
    if destination.exists():
        if not metadata.exists() or json.loads(metadata.read_text(encoding="utf-8")) != images:
            raise ValueError("Existing archive belongs to other images; use a new output directory")
    else:
        for image in images.values():
            subprocess.run(["docker", "tag", image["id"], image["tag"]], check=True)
        temporary = folder / "images.tar.gz.partial"
        started = time.monotonic()
        copied = 0
        last = started
        with (folder / "image-save.log").open("wb") as errors:
            process = subprocess.Popen(
                ["docker", "image", "save", *[i["tag"] for i in images.values()]],
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            with gzip.open(temporary, "wb", compresslevel=1) as target:
                while chunk := process.stdout.read(4 * 1024 * 1024):
                    target.write(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last > 12:
                        print(f"Exported {copied / 1024**3:.2f} GiB image tar", flush=True)
                        last = time.monotonic()
            if process.wait():
                raise RuntimeError("Image export failed; see image-save.log")
        temporary.replace(destination)
        metadata.write_text(json.dumps(images, indent=2), encoding="utf-8")
    with destination.open("rb") as archive:
        checksum = hashlib.file_digest(archive, "sha256").hexdigest()
    print(
        json.dumps(
            {"file": str(destination), "bytes": destination.stat().st_size, "sha256": checksum}
        )
    )


if __name__ == "__main__":
    main()
