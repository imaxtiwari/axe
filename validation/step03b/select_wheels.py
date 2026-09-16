"""Select locked wheels for this interpreter without downloading or building."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import sys
import tomllib
from urllib.parse import urlsplit


def main() -> int:
    hooks = runpy.run_path("/workspace/selftest.py")
    hooks["add_socket_filter"]()
    # Only the pinned image's pip-vendored parser is used; site hooks stay disabled.
    sys.path.append("/usr/local/lib/python3.12/site-packages")
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename

    ranks = {tag: rank for rank, tag in enumerate(sys_tags())}
    lock = tomllib.loads(Path("/inputs/uv.lock").read_text())
    packages = {
        (canonicalize_name(package["name"]), package["version"]): package
        for package in lock["package"]
    }
    text = Path("/scratch/requirements.txt").read_text().replace("\\\n", " ")
    selected = []
    missing = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line.split(" --hash=", 1)[0].strip())
        if requirement.marker and not requirement.marker.evaluate():
            continue
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or requirement.url:
            raise RuntimeError(f"Not an exact package pin: {requirement}")
        key = (canonicalize_name(requirement.name), specs[0].version)
        if key in seen:
            continue
        seen.add(key)
        package = packages[key]
        choices = []
        for wheel in package.get("wheels", []):
            url = urlsplit(wheel["url"])
            if url.scheme != "https" or url.netloc != "files.pythonhosted.org" or url.query or url.fragment:
                raise RuntimeError(f"Unapproved artifact URL for {key}")
            filename = url.path.rsplit("/", 1)[-1]
            wheel_name, version, _, tags = parse_wheel_filename(filename)
            if canonicalize_name(wheel_name) != key[0] or str(version) != key[1]:
                raise RuntimeError(f"Wheel identity mismatch: {filename}")
            compatible = [ranks[tag] for tag in tags if tag in ranks]
            if compatible:
                choices.append((min(compatible), filename, wheel))
        if not choices:
            missing.append(f"{key[0]}=={key[1]}")
            continue
        _, filename, wheel = min(choices, key=lambda choice: (choice[0], choice[1]))
        digest = wheel["hash"].removeprefix("sha256:")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError("Invalid SHA256")
        if digest not in line:
            raise RuntimeError(f"Selected hash missing from export: {filename}")
        selected.append({"name": key[0], "version": key[1], "filename": filename,
                         "url": wheel["url"], "sha256": digest, "size": wheel["size"]})
    report = {"python": sys.version, "selected_count": len(selected), "missing": missing,
              "total_bytes": sum(wheel["size"] for wheel in selected), "wheels": selected,
              "lock_sha256": hashlib.sha256(Path("/inputs/uv.lock").read_bytes()).hexdigest()}
    Path("/scratch/wheel-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "wheels"}), flush=True)
    if missing:
        return 1
    with Path("/scratch/wheels.tsv").open("x") as stream:
        for wheel in selected:
            stream.write("\t".join(str(wheel[key]) for key in ("filename", "sha256", "size", "url")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
