"""Verify release contents with AstrBot's real ZIP reader in an isolated directory."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    core, output = args.core.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ASTRBOT_ROOT"] = str(output / "astrbot-root")
    sys.path.insert(0, str(core))
    if os.environ.get("MEMORY_FRAMEWORK_EXTRA_DEPS"):
        sys.path.insert(0, os.environ["MEMORY_FRAMEWORK_EXTRA_DEPS"])
    from install import inspect_packages
    from build_release import runtime_files
    packages = inspect_packages(ROOT / "dist")
    import astrbot.api
    from astrbot.core.star.updater import _PluginUpdater
    from packaging.requirements import Requirement

    manifest = json.loads((ROOT / "source-manifest.json").read_text("utf-8"))
    for row in manifest["files"]:
        assert hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest() == row["release_sha256"], row["path"]
    results = []
    for name, archive in packages:
        metadata = _PluginUpdater.inspect_plugin_archive(str(archive))["metadata"]
        assert metadata["name"] == name
        assert metadata.get("repo") == ""
        with tempfile.TemporaryDirectory(prefix=name + "-", dir=output) as stage:
            # AstrBot consumes/deletes the uploaded ZIP after extraction.
            # Always pass a disposable copy, never the distributable artifact.
            upload = Path(stage) / "upload.zip"
            shutil.copyfile(archive, upload)
            extracted = Path(stage) / "extracted"
            _PluginUpdater()._extract_plugin_archive(str(upload), str(extracted))
            count = 0
            for src in runtime_files(ROOT / "plugins" / name):
                if not src.is_file():
                    continue
                relative = src.relative_to(ROOT / "plugins" / name)
                assert (extracted / relative).read_bytes() == src.read_bytes(), str(relative)
                if src.suffix == ".py":
                    ast.parse(src.read_text("utf-8-sig"), filename=str(relative))
                assert not set(relative.parts) & {"__pycache__", "node_modules", ".astrbot-plugin", ".git", "data", "plugin_data"}
                assert src.suffix not in {".db", ".sqlite", ".sqlite3", ".pyc", ".log", ".index"}
                count += 1
        requirements = []
        for line in (ROOT / "plugins" / name / "requirements.txt").read_text("utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            req = Requirement(line)
            version = importlib.metadata.version(req.name)
            assert req.specifier.contains(version), f"Dependency version mismatch: {req.name} {version}"
            requirements.append({"name": req.name, "version": version})
        schema = json.loads((ROOT / "plugins" / name / "_conf_schema.json").read_text("utf-8"))
        def check_secrets(fields):
            for value in fields.values():
                if not isinstance(value, dict):
                    continue
                if value.get("_secret"):
                    assert not value.get("default"), "Nonempty secret schema default"
                if value.get("type") == "object":
                    check_secrets(value.get("items", {}))
        check_secrets(schema)
        results.append({"plugin": name, "version": metadata["version"], "extracted_files": count,
                        "real_astrbot_extractor": True, "dependencies": requirements})
    frontend = ROOT / "plugins/astrbot_plugin_livingmemory/learning/web_res/static/dashboard"
    html = (frontend / "index.html").read_text("utf-8")
    assets = re.findall(r'["\']/static/dashboard/assets/([^"\']+)["\']', html)
    assert assets and all((frontend / "assets" / a).is_file() for a in assets)
    report = {"source_files_verified": len(manifest["files"]), "plugins": results,
              "dashboard_assets_verified": assets, "production_data_packaged": False}
    (output / "result.json").write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
