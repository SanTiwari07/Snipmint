"""Licence gate for the core install (NarraReel NarraReel WHAT_TO_DO.md section 6.4).

Creates a clean virtualenv, installs Snipmint's core dependencies only, lists every package's
licence with pip-licenses and fails if one is outside the allow-list. Optional extras are only
reported (they may contain documented copyleft components, see THIRD_PARTY_LICENSES.md).

Usage: python scripts/check_licenses.py [--extras offline,align]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Substrings of licence names that are acceptable in the core install (policy rule 2).
ALLOWED = (
    "MIT", "BSD", "Apache", "ISC", "Python Software Foundation", "PSF", "MPL", "Mozilla",
    "HPND", "Historical Permission Notice", "LGPL", "GNU Lesser", "Unlicense", "Public Domain",
    "CC0", "Zlib",
)  # fmt: skip
# Packages whose metadata is unhelpful but whose licence was checked by hand.
MANUAL_OK = {
    "snipmint": "MIT (this project)",
    "pip": "MIT",
    "setuptools": "MIT",
    "pip-licenses": "MIT",
    "prettytable": "BSD-3-Clause",
    "wcwidth": "MIT",
}


def is_allowed(licence: str) -> bool:
    parts = [p.strip() for p in licence.replace(";", " AND ").split(" AND ")]
    return all(any(a.lower() in p.lower() for a in ALLOWED) for p in parts if p)


def venv_python(env_dir: Path) -> Path:
    return env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def licences_for(extras: str | None) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp) / "env"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        py = str(venv_python(env_dir))
        target = f"{ROOT}[{extras}]" if extras else str(ROOT)
        run([py, "-m", "pip", "install", "-q", target])
        run([py, "-m", "pip", "install", "-q", "pip-licenses"])
        out = run([py, "-m", "piplicenses", "--format=json", "--with-system"]).stdout
        return json.loads(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extras", help="comma separated extras to report on (informational)")
    args = ap.parse_args()

    core = licences_for(None)
    bad = [
        p for p in core
        if p["Name"].lower() not in MANUAL_OK and not is_allowed(p["License"])
    ]  # fmt: skip
    print(f"Core install: {len(core)} packages")
    for p in sorted(core, key=lambda p: p["Name"].lower()):
        print(f"  {p['Name']:30} {p['Version']:12} {p['License']}")
    if bad:
        print("\nNOT ALLOWED in core:")
        for p in bad:
            print(f"  {p['Name']} {p['Version']}: {p['License']}")
        return 1

    if args.extras:
        print(f"\nInformational report for extras [{args.extras}]:")
        core_names = {p["Name"] for p in core}
        for p in licences_for(args.extras):
            if p["Name"] not in core_names:
                flag = "" if is_allowed(p["License"]) else "   <-- check THIRD_PARTY_LICENSES.md"
                print(f"  {p['Name']:30} {p['Version']:12} {p['License']}{flag}")
    print("\nLicence check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
