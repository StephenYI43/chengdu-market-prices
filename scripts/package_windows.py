"""Build a Windows handoff with actual images, real data and offline Spark connectors."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def command(args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, text=True, encoding="utf-8", **kwargs).strip()


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def compose(images, jars):
    mounts = ["./runtime:/app/runtime", "./artifacts:/app/artifacts"]
    environment = {
        "PYTHONPATH": "/app/src",
        "MARKET_RUNTIME_DIR": "/app/runtime",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
    }

    def service(role, args, memory, health):
        return {
            "image": images[role]["tag"],
            "platform": "linux/amd64",
            "pull_policy": "never",
            "entrypoint": [],
            "command": args,
            "environment": dict(environment),
            "volumes": list(mounts),
            "restart": "unless-stopped",
            "mem_limit": memory,
            "healthcheck": {
                "test": ["CMD", "/app/.venv/bin/python", "-c", health],
                "interval": "5s",
                "timeout": "5s",
                "retries": 60,
                "start_period": "20s",
            },
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }

    api = service(
        "app",
        ["/app/.venv/bin/uvicorn", "market_prices.api:app", "--host", "0.0.0.0", "--port", "8000"],
        "1g",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=3)",
    )
    api["ports"] = ["127.0.0.1:${MARKET_HTTP_PORT:-18880}:8000"]
    api["networks"] = ["web", "backend"]
    api["depends_on"] = {"kafka": {"condition": "service_healthy"}}
    stream = service(
        "app",
        [
            "/opt/spark/bin/spark-submit",
            "--master",
            "local[2]",
            "--driver-memory",
            "2g",
            "--jars",
            ",".join("/opt/spark/portable-jars/" + jar for jar in jars),
            "scripts/stream_worker.py",
        ],
        "5g",
        "from market_prices.job_store import JobStore; "
        "raise SystemExit(0 if JobStore().worker_status()['online'] else 1)",
    )
    stream["volumes"].append("./connectors:/opt/spark/portable-jars:ro")
    stream["environment"].update(
        SPARK_LOCAL_IP="127.0.0.1",
        PYSPARK_PYTHON="/app/.venv/bin/python",
        PYSPARK_DRIVER_PYTHON="/app/.venv/bin/python",
        MARKET_IMPORT_TOPIC="price.imports.v1",
    )
    stream["depends_on"] = {"kafka": {"condition": "service_healthy"}}
    forecast = service(
        "forecast",
        ["/app/.venv/bin/python", "scripts/forecast_worker.py"],
        "3g",
        "from market_prices.forecast_store import service_status; "
        "raise SystemExit(0 if service_status()['online'] else 1)",
    )
    forecast["environment"]["MLFLOW_ENABLE_TELEMETRY"] = "false"
    stream["networks"] = ["backend"]
    forecast["networks"] = ["backend"]
    kafka = {
        "image": images["kafka"]["tag"],
        "platform": "linux/amd64",
        "pull_policy": "never",
        "environment": {
            "CLUSTER_ID": "MkU3OEVBNTcwNTJENDM2Qk",
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "INTERNAL://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "INTERNAL://kafka:9092",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
            "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
            "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
            "KAFKA_LOG_DIRS": "/var/lib/kafka/data",
        },
        "volumes": ["kafka-data:/var/lib/kafka/data"],
        "networks": ["backend"],
        "restart": "unless-stopped",
        "mem_limit": "1536m",
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "/opt/kafka/bin/kafka-broker-api-versions.sh "
                "--bootstrap-server kafka:9092 >/dev/null 2>&1",
            ],
            "interval": "10s",
            "timeout": "10s",
            "retries": 30,
            "start_period": "20s",
        },
    }
    return {
        "name": "chengdu-market-demo",
        "services": {"kafka": kafka, "api": api, "stream": stream, "forecast": forecast},
        "volumes": {"kafka-data": {}},
        "networks": {"web": {}, "backend": {"internal": True}},
    }


def copy_sqlite(source, destination):
    with (
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(destination) as backup,
    ):
        original.backup(backup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-archive", required=True, type=Path)
    parser.add_argument("--images-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path, default=Path("D:/bigdatasource"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    images = json.loads(args.images_json.read_text(encoding="utf-8"))
    for role, service in [("app", "api"), ("forecast", "forecast"), ("kafka", "kafka")]:
        container = command(["docker", "compose", "ps", "-q", service])
        actual = command(["docker", "inspect", container, "--format", "{{.Image}}"])
        if actual != images[role]["id"]:
            raise ValueError(f"Exported image does not match running {service}")
        if images[role]["architecture"] != "amd64" or images[role]["os"] != "linux":
            raise ValueError("The Windows x64 handoff needs Linux amd64 images")
    shutil.copyfile(args.image_archive, output / "images.tar.gz")
    print("Copied actual image archive", flush=True)
    for source in (ROOT / "distribution/windows").iterdir():
        if source.suffix == ".ps1":
            (output / source.name).write_text(
                source.read_text(encoding="utf-8-sig"), encoding="utf-8-sig", newline="\r\n"
            )
        elif source.suffix == ".cmd":
            (output / source.name).write_text(
                source.read_text(encoding="ascii"), encoding="ascii", newline="\r\n"
            )
        else:
            shutil.copyfile(source, output / source.name)
    connectors = output / "connectors"
    connectors.mkdir()
    stream = command(["docker", "compose", "ps", "-q", "stream"])
    subprocess.run(["docker", "cp", f"{stream}:/opt/spark/ivy/jars/.", str(connectors)], check=True)
    jars = sorted(path.name for path in connectors.glob("*.jar"))
    if not jars or not any("spark-sql-kafka" in name for name in jars):
        raise ValueError("Required Spark Kafka connector cache is missing")
    composition = compose(images, jars)
    (output / "compose.yaml").write_text(json.dumps(composition, indent=2), encoding="utf-8")

    pointer_path = ROOT / "runtime/warehouse/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    warehouse = ROOT / "runtime/warehouse" / pointer["relative_path"]
    report = json.loads((warehouse / "verification.json").read_text(encoding="utf-8"))
    model_pointer = ROOT / "runtime/forecasts/by-dataset" / (pointer["run_id"] + ".json")
    model_id = json.loads(model_pointer.read_text(encoding="utf-8"))["model_run_id"]
    model = ROOT / "runtime/forecasts/runs" / model_id
    before = {path: digest(path) for path in [pointer_path, warehouse / "warehouse.duckdb"]}
    frozen = [(pointer_path, "runtime/warehouse/current.json")]
    frozen.extend(
        (
            path,
            "runtime/warehouse/"
            + str(path.relative_to(ROOT / "runtime/warehouse")).replace("\\", "/"),
        )
        for path in warehouse.rglob("*")
        if path.is_file()
    )
    frozen.extend(
        (path, "runtime/forecasts/" + path.relative_to(ROOT / "runtime/forecasts").as_posix())
        for path in [model_pointer, *[p for p in model.rglob("*") if p.is_file()]]
    )
    experiment_artifacts = ROOT / "runtime/forecasts/mlartifacts"
    frozen.extend(
        (path, "runtime/forecasts/" + path.relative_to(ROOT / "runtime/forecasts").as_posix())
        for path in experiment_artifacts.rglob("*")
        if path.is_file()
    )
    backup_folder = ROOT / "artifacts/handoff-build" / (output.name + "-sqlite-backups")
    backup_folder.parent.mkdir(parents=True, exist_ok=True)
    backup_folder.mkdir(exist_ok=False)
    for rel in ["forecasts/mlflow.sqlite", "alerts/reviews.sqlite"]:
        source = ROOT / "runtime" / rel
        if source.exists():
            destination = backup_folder / source.name
            copy_sqlite(source, destination)
            frozen.append((destination, "runtime/" + rel))
    with ZipFile(output / "initial-state.zip", "w", ZIP_DEFLATED, compresslevel=6) as seed:
        for path, name in frozen:
            seed.write(path, name)
    if not all(digest(path) == value for path, value in before.items()):
        raise ValueError("Active source snapshot changed during packaging")

    source_files = sorted(args.source_dir.glob("成都农贸市场价格_近两年*"))
    if not source_files:
        raise ValueError("Original real source files are missing")
    with ZipFile(output / "source-data.zip", "w", ZIP_DEFLATED, compresslevel=6) as source_zip:
        for path in source_files:
            source_zip.write(path, path.name)
    subprocess.run(
        ["git", "archive", "--format=zip", f"--output={output / 'source-code.zip'}", "HEAD"],
        cwd=ROOT,
        check=True,
    )
    (output / "samples").mkdir()
    shutil.copyfile(ROOT / "data/samples/latest_week.csv", output / "samples/latest_week.csv")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": command(["git", "rev-parse", "HEAD"]),
        "images": list(images.values()),
        "dataset_run_id": pointer["run_id"],
        "model_run_id": model_id,
        "source_summary": report["summary"],
        "offline_connectors": jars,
        "fresh_state": "Import jobs, leases, Kafka offsets and Spark checkpoints start fresh.",
        "files": [
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
            for path in sorted(output.rglob("*"))
            if path.is_file()
        ],
    }
    (output / "bundle.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    archive_path = output.with_suffix(".zip")
    with ZipFile(archive_path, "w", ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, output.name + "/" + path.relative_to(output).as_posix())
    print(
        json.dumps(
            {
                "folder": str(output),
                "archive": str(archive_path),
                "bytes": archive_path.stat().st_size,
                "sha256": digest(archive_path),
                "files": len(manifest["files"]),
                "jars": len(jars),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
