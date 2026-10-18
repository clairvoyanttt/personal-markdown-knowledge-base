import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class CollectionCommandTests(unittest.TestCase):
    def test_create_and_open_use_real_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); collection = root / "collection"; collection.mkdir()
            for action in ["create", "open"]:
                result = subprocess.run([sys.executable, "scripts/collection.py", action, str(collection), "--config", str(root/"config.json")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["status"], "OK")
                self.assertEqual(data["value"]["stage"], "ACTIVATED")
            self.assertTrue((collection/"knowledge.sqlite").is_file())

    def test_nonempty_target_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); collection = root/"collection"; collection.mkdir()
            f = collection/"keep.txt"; f.write_text("keep")
            result = subprocess.run([sys.executable, "scripts/collection.py", "create", str(collection), "--config", str(root/"config.json")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "TARGET_NOT_EMPTY")
            self.assertEqual(f.read_text(), "keep")

    def test_configuration_cannot_be_created_inside_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = subprocess.run([sys.executable, "scripts/collection.py", "create", str(root), "--config", str(root/"config.json")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(list(root.iterdir()), [])
