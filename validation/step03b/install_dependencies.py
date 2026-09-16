"""Verify and install the acquired lock closure without network or source builds."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path


def main() -> int:
    hooks = runpy.run_path("/workspace/selftest.py")
    hooks["add_socket_filter"]()
    manifest = json.loads(Path("/inputs/wheel-manifest.json").read_text())
    assert manifest["lock_sha256"] == hashlib.sha256(Path("/inputs/uv.lock").read_bytes()).hexdigest()
    requirements = []
    for wheel in manifest["wheels"]:
        path = Path("/wheels") / wheel["filename"]
        assert path.parent == Path("/wheels") and path.is_file() and not path.is_symlink()
        assert path.stat().st_size == wheel["size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == wheel["sha256"]
        requirements.append(f'{wheel["name"]}=={wheel["version"]} --hash=sha256:{wheel["sha256"]}')
    requirement_path = Path("/scratch/selected-requirements.txt")
    requirement_path.write_text("\n".join(requirements) + "\n")
    command = [
        "/usr/local/bin/python", "-I", "-S", "-B", "-u", "-c",
        "import sys,runpy; sys.path.append('/usr/local/lib/python3.12/site-packages'); "
        "runpy.run_module('pip', run_name='__main__')",
        "--isolated", "--disable-pip-version-check", "--no-cache-dir", "install",
        "--no-index", "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-compile",
        "--find-links", "/wheels", "--target", "/scratch/dependencies",
        "--requirement", str(requirement_path),
    ]
    print(json.dumps({"command": command}), flush=True)
    result = subprocess.run(command, check=False, timeout=180)
    if result.returncode:
        return result.returncode
    installed = {re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(): distribution.version
                 for distribution in importlib.metadata.distributions(path=["/scratch/dependencies"])}
    expected = {wheel["name"]: wheel["version"] for wheel in manifest["wheels"]}
    assert installed == expected, (installed, expected)
    node = subprocess.run(["/usr/local/bin/node", "--version"], capture_output=True, text=True, check=True)
    report = {"python": sys.version, "node": node.stdout.strip(), "installed": installed,
              "lock_sha256": manifest["lock_sha256"], "exit_code": result.returncode}
    Path("/scratch/install-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"installed_count": len(installed), "node": node.stdout.strip(), "exit_code": 0}))
    # Inspect library source as text, without importing installed packages or running site hooks.
    source = Path("/scratch/dependencies/pydantic_settings/main.py").read_text()
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if "dotenv_settings =" in line or "settings_customise_sources(" in line:
            print("\n".join(f"{i + 1}: {lines[i]}" for i in range(max(0, index - 4), min(len(lines), index + 20))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
