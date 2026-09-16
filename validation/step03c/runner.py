"""Offline entry point: confinement first, synthetic settings before all AXE imports."""

from __future__ import annotations

import base64
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def configure() -> None:
    for name in ("data", "chroma", "ready", "cache", "reports", "tmp"):
        Path(f"/scratch/{name}").mkdir(exist_ok=True)
    os.environ.update({
        "AXE_VALIDATION": "1", "APP_ENV": "test", "LOG_LEVEL": "WARNING",
        "DATABASE_URL": "sqlite+aiosqlite:////scratch/data/axe.db",
        "CHROMA_PERSIST_DIR": "/scratch/chroma", "READINESS_DIR": "/scratch/ready",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "TMPDIR": "/scratch/tmp", "XDG_CACHE_HOME": "/scratch/cache",
        "COVERAGE_FILE": "/scratch/reports/.coverage", "RUFF_CACHE_DIR": "/scratch/cache/ruff",
        "MYPY_CACHE_DIR": "/scratch/cache/mypy", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "AWS_EC2_METADATA_DISABLED": "true",
        "OTEL_SDK_DISABLED": "true", "ANONYMIZED_TELEMETRY": "false",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PYRIGHT_PYTHON_FORCE_VERSION": "1.1.411",
    })
    sys.path.extend(["/usr/local/lib/python3.12/site-packages", "/app/src"])
    os.chdir("/app")


def child(stage: str) -> int:
    configure()
    plugins = ["-p", "pytest_asyncio.plugin", "-p", "pytest_cov.plugin", "-p", "respx.plugin"]
    common = ["-c", "/app/pyproject.toml", *plugins, "-o", f"cache_dir=/scratch/cache/{stage}",
              f"--basetemp=/scratch/tmp/{stage}", f"--junitxml=/scratch/reports/{stage}.xml"]
    if stage in {"focused", "full", "compliance", "worker", "identity"}:
        import pytest

        os.environ["COVERAGE_FILE"] = f"/scratch/reports/.coverage-{stage}"
        if stage == "focused":
            return int(pytest.main([*common, "/harness/test_settings.py"]))
        if stage == "identity":
            return int(pytest.main([*common, "tests/test_step04_identity.py", "-v"]))
        if stage == "worker":
            return int(pytest.main([*common, "tests/test_ingestion.py", "-v"]))
        if stage == "full":
            return int(pytest.main([*common, "-q", "tests", "--cov=src/axe",
                                   "--cov-report=xml:/scratch/reports/coverage-full.xml"]))
        coverage = [f"--cov={name}" for name in (
            "axe.security", "axe.services.mnpi", "axe.services.retention", "axe.services.export",
            "axe.agents.mnpi_review", "axe.security.encryption")]
        return int(pytest.main([*common, "tests/test_compliance.py", "-v", "-o", "addopts=",
                               *coverage, "--cov-report=term-missing",
                               "--cov-report=xml:/scratch/reports/coverage-compliance.xml",
                               "--cov-fail-under=90"]))
    if stage == "policy":
        runpy.run_path("/app/scripts/check_isolation_policy.py", run_name="__main__")
        return 0
    if stage == "pyright":
        return subprocess.call(["/usr/local/bin/node",
                                "/usr/local/lib/python3.12/site-packages/pyright/dist/index.js",
                                "src", "tests", "scripts"])
    module = "mypy" if stage == "mypy" else "ruff"
    arguments = ["src", "tests", "scripts"]
    if stage == "lint":
        arguments.insert(0, "check")
    elif stage == "format":
        arguments = ["format", "--check", *arguments]
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--stage":
        # Inherits the parent's additional seccomp filter across exec.
        hooks = runpy.run_path("/workspace/selftest.py")
        assert hooks["security_state"]() >= 2
        return child(sys.argv[2])
    groups = {
        "baseline": ("focused", "full", "compliance", "lint", "format"),
        "types": ("mypy", "pyright"),
        "policy": ("policy",),
        "worker": ("focused", "worker", "lint", "format"),
        "identity": ("focused", "identity", "lint", "format"),
    }
    if len(sys.argv) != 3 or sys.argv[1] != "--group" or sys.argv[2] not in groups:
        raise ValueError("An explicit validation group is required")
    stages = groups[sys.argv[2]]
    # The unchanged synthetic suite verifies the final image, including inherited child boundaries.
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-u", "/workspace/selftest.py"],
                            check=False, timeout=40)
    if result.returncode:
        return result.returncode
    hooks = runpy.run_path("/workspace/selftest.py")
    hooks["add_socket_filter"]()
    configure()
    results: dict[str, int] = {}
    for stage in stages:
        command = [sys.executable, "-I", "-S", "-B", "-u", "/harness/runner.py", "--stage", stage]
        with Path(f"/scratch/reports/{stage}.log").open("x") as log:
            try:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        check=False, timeout=180)
                results[stage] = result.returncode
            except subprocess.TimeoutExpired:
                results[stage] = 124
        print(json.dumps({"stage": stage, "command": command, "exit_code": results[stage]}), flush=True)
        if stage == "focused" and results[stage]:
            break
    Path("/scratch/reports/result.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(results.values()))


if __name__ == "__main__":
    sys.exit(main())
