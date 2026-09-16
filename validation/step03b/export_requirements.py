"""Export locked requirements without networking or importing AXE."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys


def main() -> int:
    hooks = runpy.run_path("/workspace/selftest.py")
    hooks["add_socket_filter"]()
    inputs = [Path("/inputs/pyproject.toml"), Path("/inputs/uv.lock")]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    print(json.dumps({"python": sys.version, "input_sha256": before}), flush=True)
    subprocess.run(["/usr/local/bin/uv", "--version"], check=True)
    command = [
        "/usr/local/bin/uv", "export", "--offline", "--frozen", "--no-config",
        "--project", "/inputs", "--format", "requirements-txt",
        "--extra", "dev", "--extra", "ocr", "--group", "dev", "--no-emit-project",
        "--output-file", "/scratch/requirements.txt",
    ]
    print(json.dumps({"command": command}), flush=True)
    result = subprocess.run(command, check=False)
    print(json.dumps({"export_exit": result.returncode}), flush=True)
    if result.returncode:
        return result.returncode
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    if before != after:
        raise RuntimeError("Input manifests changed during export")
    report = {"python": sys.version, "input_sha256": before, "export_exit": 0}
    Path("/scratch/export-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
