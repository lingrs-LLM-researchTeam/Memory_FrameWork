"""Build deterministic, source-inclusive AstrBot plugin ZIPs and a framework bundle."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "1.1.0"
NAME = f"astrbot-memory-framework-{RELEASE}"
PLUGINS = ("astrbot_plugin_livingmemory",)
EXCLUDED = {"__pycache__", ".pytest_cache", "node_modules", ".git", ".astrbot-plugin", ".env"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and not set(p.relative_to(root).parts) & EXCLUDED
                  and p.suffix not in {".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".index"})


def runtime_files(plugin):
    """Ship runtime code/assets; complete tests and frontend sources stay in the source bundle."""
    for path in files(plugin):
        rel = path.relative_to(plugin)
        if set(rel.parts) & {"tests", "web_src"}:
            continue
        if path.name in {"package.json", "package-lock.json", "pytest.ini", "requirements-test.txt", "ruff.toml"}:
            continue
        yield path


def archive(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content in sorted(entries):
            info = zipfile.ZipInfo(name, (2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            z.writestr(info, content)


def build():
    from export_sources import record_sources
    record_sources(ROOT)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    packages = []
    for name in PLUGINS:
        plugin = ROOT / "plugins" / name
        entries = [(f"{name}/{p.relative_to(plugin).as_posix()}", p.read_bytes()) for p in runtime_files(plugin)]
        artifact = dist / f"{name}-memoryfw-{RELEASE}.zip"
        archive(artifact, entries)
        packages.append({"plugin": name, "file": artifact.name, "sha256": sha(artifact.read_bytes()),
                         "bytes": artifact.stat().st_size, "file_count": len(entries)})
    (dist / "packages.json").write_text(json.dumps({"release": RELEASE, "install_order": list(PLUGINS),
                                                   "packages": packages}, indent=2) + "\n", "utf-8")
    entries = []
    for p in files(ROOT):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in {"dist", "validation"}:
            continue
        entries.append((f"{NAME}/{rel.as_posix()}", p.read_bytes()))
    for p in [dist / "packages.json"] + [dist / x["file"] for x in packages]:
        entries.append((f"{NAME}/dist/{p.name}", p.read_bytes()))
    component_files = [dist / x["file"] for x in packages] + [dist / "packages.json"]
    inner_checksums = "".join(f"{sha(p.read_bytes())}  {p.name}\n" for p in component_files)
    entries.append((f"{NAME}/dist/SHA256SUMS.txt", inner_checksums.encode("utf-8")))
    archive(dist / f"{NAME}.zip", entries)
    outputs = [dist / x["file"] for x in packages] + [dist / f"{NAME}.zip", dist / "packages.json"]
    (dist / "SHA256SUMS.txt").write_text("".join(f"{sha(p.read_bytes())}  {p.name}\n" for p in outputs), "utf-8")
    print(json.dumps({"packages": packages, "bundle": str(dist / f"{NAME}.zip")}, indent=2))


if __name__ == "__main__":
    build()
