"""Release/installer invariants; no production application or external network."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "tools/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-framework-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_writes_nothing(self):
        result = installer.install(self.data)
        self.assertFalse(result["written"])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_installs_single_plugin_and_preserves_unrelated_data(self):
        keep = self.data / "keep.db"
        keep.write_bytes(b"unrelated")
        installer.install(self.data, apply=True)
        self.assertEqual(len(installer.PLUGINS), 1)
        self.assertEqual(len(list((self.data / "plugins").iterdir())), 1)
        for name in installer.PLUGINS:
            self.assertTrue((self.data / "plugins" / name / "main.py").is_file())
        self.assertEqual(keep.read_bytes(), b"unrelated")
        self.assertFalse((self.data / "plugin_data").exists())
        self.assertFalse((self.data / "config").exists())

    def test_existing_component_refuses_entire_install(self):
        p = self.data / "plugins" / "astrbot_plugin_self_learning"
        p.mkdir(parents=True)
        (p / "user.py").write_text("original")
        with self.assertRaises(FileExistsError):
            installer.install(self.data, apply=True)
        self.assertFalse((self.data / "plugins" / installer.PLUGINS[0]).exists())
        self.assertEqual((p / "user.py").read_text(), "original")

    def test_existing_configuration_is_not_replaced(self):
        config = self.data / "config" / f"{installer.PLUGINS[0]}_config.json"
        config.parent.mkdir()
        config.write_text('{"custom":true}')
        with self.assertRaises(FileExistsError):
            installer.install(self.data, apply=True)
        self.assertFalse((self.data / "plugins").exists())

    def test_corrupted_package_refuses_before_writing(self):
        dist = self.root / "dist"
        shutil.copytree(ROOT / "dist", dist, ignore=shutil.ignore_patterns("astrbot-memory-framework-*.zip"))
        row = json.loads((dist / "packages.json").read_text())["packages"][0]
        with (dist / row["file"]).open("ab") as f:
            f.write(b"corrupted")
        with self.assertRaisesRegex(ValueError, "Checksum"):
            installer.install(self.data, apply=True, dist=dist)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_unsafe_path_rejected_even_if_checksum_updated(self):
        dist = self.root / "dist"
        shutil.copytree(ROOT / "dist", dist, ignore=shutil.ignore_patterns("astrbot-memory-framework-*.zip"))
        manifest = json.loads((dist / "packages.json").read_text())
        row = manifest["packages"][0]
        path = dist / row["file"]
        with zipfile.ZipFile(path, "a") as z:
            z.writestr(f'{row["plugin"]}/../../escape.py', "bad")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        row["file_count"] += 1
        (dist / "packages.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            installer.install(self.data, apply=True, dist=dist)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_presets_match_install_defaults(self):
        for name in installer.PLUGINS:
            schema = json.loads((ROOT / "plugins" / name / "_conf_schema.json").read_text("utf-8"))
            preset = json.loads((ROOT / "presets" / f"{name}.merge.json").read_text("utf-8"))
            for group, values in preset.items():
                for key, value in values.items():
                    self.assertEqual(schema[group]["items"][key]["default"], value)


if __name__ == "__main__":
    unittest.main()
