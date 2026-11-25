"""Unzip the actual handoff, load its image archive and boot isolated offline services."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import httpx

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18885)
    parser.add_argument("--project", default="chengdu-handoff-acceptance")
    args = parser.parse_args()
    test_root = args.test_root.resolve()
    test_root.mkdir(parents=True, exist_ok=False)
    pointer = ROOT / "runtime/warehouse/current.json"
    current = json.loads(pointer.read_text(encoding="utf-8"))
    database = ROOT / "runtime/warehouse" / current["relative_path"] / "warehouse.duckdb"
    protected = {path: digest(path) for path in [pointer, database]}
    with ZipFile(args.archive) as archive:
        top = sorted({Path(name).parts[0] for name in archive.namelist()})
        if len(top) != 1:
            raise ValueError("Archive must contain one package folder")
        archive.extractall(test_root)
    folder = test_root / top[0]
    manifest = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    checks = {
        "fresh_directory_has_no_host_venv_or_frontend_build": not any(
            (folder / name).exists()
            for name in [".venv", "node_modules", "frontend/dist", "runtime"]
        )
    }
    env = dict(os.environ, MARKET_HTTP_PORT=str(args.port))
    compose = ["docker", "compose", "-p", args.project, "-f", str(folder / "compose.yaml")]

    def run(command, name, cwd=folder, timeout=600):
        log = test_root / (name + ".log")
        with log.open("wb") as output:
            result = subprocess.run(
                command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=timeout
            )
        if result.returncode:
            print(log.read_text(encoding="utf-8", errors="replace")[-7000:], flush=True)
            raise RuntimeError(f"{name} failed; see {log}")
        print(f"Passed {name}", flush=True)

    launcher = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(folder / "start.ps1"),
        "-NoBrowser",
        "-ProjectName",
        args.project,
        "-Port",
        str(args.port),
    ]
    existing = subprocess.check_output(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={args.project}",
            "--format",
            "{{.ID}}",
        ],
        text=True,
    ).strip()
    if existing or args.project == "chengdu-market-prices":
        raise ValueError("Use an unused test project name; existing containers are not modified")
    try:
        run([*launcher, "-ReloadImages"], "first-start-and-actual-image-load", timeout=900)
        checks["archive_import_and_first_start_succeeded"] = True
        with httpx.Client(
            base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=30
        ) as client:
            status = client.get("/api/v1/system/status").raise_for_status().json()
            checks["four_services_online_on_isolated_port"] = all(
                [
                    status["kafka"]["online"],
                    status["stream"]["online"],
                    status["forecast_service"]["online"],
                    bool(status["current_model"]),
                ]
            )
            checks["all_real_data_and_matching_model_are_present"] = (
                status["dataset"]["totals"]["source_rows"] == 75726
                and status["dataset"]["totals"]["quoted_rows"] == 75564
                and status["dataset"]["totals"]["missing_rows"] == 162
                and status["dataset"]["totals"]["periods"] == 105
                and status["dataset"]["meta"]["run_id"] == manifest["dataset_run_id"]
                and status["current_model"]["model_run_id"] == manifest["model_run_id"]
            )
            home = client.get("/").raise_for_status().text
            import re

            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', home)
            checks["embedded_compiled_frontend_and_assets_are_served"] = bool(assets) and all(
                client.get(asset).status_code == 200 for asset in assets
            )
            catalog = client.get("/api/v1/forecasts/catalog").raise_for_status().json()
            checks["pretrained_37_ready_and_7_excluded_specifications"] = (
                catalog["status"] == "ready"
                and catalog["report"]["ready_products"] == 37
                and catalog["report"]["excluded_products"] == 7
                and len(catalog["items"]) == 44
            )
            product = next(
                item["product"]
                for item in catalog["items"]
                if item["product"]["product_name"] == "茄子"
            )
            forecast = (
                client.get("/api/v1/forecasts", params={"product_id": product["product_id"]})
                .raise_for_status()
                .json()
            )
            checks["four_future_weeks_and_real_backtests_are_available"] = (
                len(forecast["forecasts"]) == 4
                and [row["target_date"] for row in forecast["forecasts"]]
                == ["2026-12-01", "2026-12-08", "2026-12-15", "2026-12-22"]
                and forecast["evaluation"]["models"]["lightgbm"]["n"] == 48
            )
            latest = client.get("/api/v1/alerts").raise_for_status().json()
            all_alerts = (
                client.get(
                    "/api/v1/alerts", params={"date_from": "2024-11-26", "date_to": "2026-11-24"}
                )
                .raise_for_status()
                .json()
            )
            checks["latest_3_and_historical_896_alerts_are_available"] = (
                latest["total"] == 3 and all_alerts["total"] == 896
            )
            with (folder / "samples/latest_week.csv").open("rb") as sample:
                preview = (
                    client.post(
                        "/api/v1/imports/preview",
                        files={"file": ("latest_week.csv", sample, "text/csv")},
                    )
                    .raise_for_status()
                    .json()
                )
            task = (
                client.post("/api/v1/imports", json={"preview_id": preview["preview_id"]})
                .raise_for_status()
                .json()
            )
            checks["actual_real_file_upload_is_idempotent"] = (
                preview["counts"]["existing_duplicate"] == 738
                and task["status"] == "no_op"
                and task["inserted_rows"] == 0
                and task["skipped_rows"] == 738
            )
        network_id = subprocess.check_output(
            [*compose, "ps", "-q", "api"], cwd=folder, env=env, text=True
        ).strip()
        container = json.loads(
            subprocess.check_output(["docker", "inspect", network_id], text=True)
        )[0]
        network = next(iter(container["NetworkSettings"]["Networks"]))
        network_data = json.loads(
            subprocess.check_output(["docker", "network", "inspect", network], text=True)
        )[0]
        checks["container_network_has_no_external_egress"] = network_data["Internal"] is True
        smoke_id = "handoff-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "api",
                "/app/.venv/bin/python",
                "scripts/publish_sample.py",
                "--run-id",
                smoke_id,
            ],
            "real-kafka-publication",
        )
        composition = json.loads((folder / "compose.yaml").read_text(encoding="utf-8"))
        jars = composition["services"]["stream"]["command"][6]
        run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "stream",
                "/opt/spark/bin/spark-submit",
                "--master",
                "local[2]",
                "--driver-memory",
                "2g",
                "--jars",
                jars,
                "scripts/spark_smoke.py",
                "--run-id",
                smoke_id,
            ],
            "offline-real-kafka-spark-parquet",
        )
        smoke = json.loads(
            (folder / "artifacts/day1" / smoke_id / "verification.json").read_text(encoding="utf-8")
        )
        checks["738_real_quotes_pass_offline_kafka_spark_and_duckdb_reconciliation"] = (
            smoke["passed"] and smoke["actual"]["rows"] == 738 and smoke["actual"]["quoted"] == 737
        )
        before_restart = digest(folder / "runtime/warehouse/current.json")
        run(launcher, "idempotent-second-start")
        run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(folder / "manage.ps1"),
                "-Action",
                "stop",
            ],
            "stop-with-data-retained",
        )
        run(launcher, "restart-with-data-retained")
        checks["repeat_start_stop_restart_keeps_existing_data"] = (
            digest(folder / "runtime/warehouse/current.json") == before_restart
        )
        checks["production_pointer_and_database_unchanged"] = all(
            digest(path) == value for path, value in protected.items()
        )
        report = {
            "passed": all(checks.values()),
            "checked_at": datetime.now(UTC).isoformat(),
            "archive_sha256": digest(args.archive),
            "project": args.project,
            "port": args.port,
            "test_directory": str(folder),
            "checks": checks,
            "stream_smoke": smoke,
            "scope": (
                "Windows PowerShell 5.1, actual image load, fresh runtime and Kafka volume, "
                "internal-only container network; same host Docker daemon."
            ),
        }
        (test_root / "verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if not report["passed"]:
            raise AssertionError("Windows handoff checks failed")
    finally:
        run([*compose, "down", "--remove-orphans"], "isolated-test-cleanup")


if __name__ == "__main__":
    main()
