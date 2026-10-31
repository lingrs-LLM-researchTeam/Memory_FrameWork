"""Install the unified plugin into a stopped, fresh AstrBot data directory.

Default is read-only. Existing plugins/configurations are never overwritten.
Dependencies are installed by AstrBot when it next loads requirements.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("astrbot_plugin_livingmemory",)
CONFLICT_NAMES = (*PLUGINS, "astrbot_plugin_self_learning")


def inspect_packages(dist: Path):
    manifest = json.loads((dist / "packages.json").read_text("utf-8"))
    rows = manifest["packages"]
    if [r["plugin"] for r in rows] != list(PLUGINS):
        raise ValueError("Unexpected plugin set or installation order")
    packages = []
    for row in rows:
        filename = row["file"]
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("Invalid package filename")
        package = dist / filename
        payload = package.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError(f"Checksum mismatch: {filename}")
        with zipfile.ZipFile(package) as z:
            seen = set()
            for info in z.infolist():
                name = info.filename
                path = PurePosixPath(name)
                parts = path.parts
                if (not parts or parts[0] != row["plugin"] or len(parts) < 2
                        or path.is_absolute() or ".." in parts or "\\" in name or ":" in name
                        or any(p.endswith((".", " ")) for p in parts)
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    raise ValueError(f"Unsafe archive member: {name}")
                key = name.casefold()
                if key in seen:
                    raise ValueError(f"Duplicate archive member: {name}")
                seen.add(key)
            if len(seen) != row["file_count"] or z.testzip() is not None:
                raise ValueError(f"Archive integrity error: {filename}")
            for required in ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "LICENSE"):
                if f'{row["plugin"]}/{required}' not in z.namelist():
                    raise ValueError(f"Missing plugin file: {required}")
        packages.append((row["plugin"], package))
    return packages


def install(data_dir: Path, *, apply=False, dist=None):
    packages = inspect_packages(dist or ROOT / "dist")
    data = data_dir.resolve(strict=True)
    if not data.is_dir():
        raise ValueError("--data-dir must be the existing AstrBot data directory")
    plugin_dir = data / "plugins"
    if plugin_dir.is_symlink() or plugin_dir.is_junction():
        raise ValueError("plugins directory must not be a link/junction")
    for name in CONFLICT_NAMES:
        for path in (plugin_dir / name, data / "config" / f"{name}_config.json"):
            if os.path.lexists(path):
                raise FileExistsError(f"Existing plugin/config: {path}; use the documented upgrade procedure")
    result = {"mode": "install" if apply else "check", "data_dir": str(data),
              "plugins": list(PLUGINS), "written": False}
    if apply:
        plugin_dir.mkdir(exist_ok=True)
        installed = []
        # Validate and stage the complete archive before the plugin becomes visible.
        with tempfile.TemporaryDirectory(prefix=".memory-framework-", dir=data) as stage:
            for name, archive in packages:
                with zipfile.ZipFile(archive) as z:
                    z.extractall(stage)
            try:
                for name, _ in packages:
                    if os.path.lexists(plugin_dir / name):
                        raise FileExistsError(f"Plugin appeared during installation: {name}")
                    (Path(stage) / name).rename(plugin_dir / name)
                    installed.append(name)
            except OSError as exc:
                raise RuntimeError(f"Installation interrupted; installed={installed}. "
                                   "Keep AstrBot stopped and inspect these directories before retrying.") from exc
        result["written"] = True
        result["next_step"] = "Start AstrBot; install dependencies, select LLM/Embedding providers, verify /lmem status."
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write new plugins; stop AstrBot first")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.data_dir, apply=args.apply), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Installation refused: {exc}\n")
