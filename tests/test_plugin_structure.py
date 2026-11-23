import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "rimworld-moddev"
MARKETPLACES = (
    ROOT / "marketplace.json",
    ROOT / ".agents/plugins/marketplace.json",
    ROOT / ".claude-plugin/marketplace.json",
    ROOT / ".cursor-plugin/marketplace.json",
)
PLATFORM_MANIFESTS = (
    PACKAGE / "plugin.json",
    PACKAGE / ".antigravity-plugin/plugin.json",
    PACKAGE / ".claude-plugin/plugin.json",
    PACKAGE / ".codex-plugin/plugin.json",
    PACKAGE / ".cursor-plugin/plugin.json",
    PACKAGE / ".zcode-plugin/plugin.json",
)


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def source_path(source):
    if isinstance(source, dict):
        return source["path"]
    return source


class PluginStructureTest(unittest.TestCase):
    def test_cli_uses_canonical_bundled_skills(self):
        module_path = ROOT / "tools/mod_cli.py"
        spec = importlib.util.spec_from_file_location("mod_cli", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        skills = ROOT / "rimworld-moddev/skills"
        self.assertEqual(skills, module.bundled_skills())
        self.assertTrue(
            (skills / "rimworld-debug/scripts/content_load.py").is_file()
        )
        self.assertTrue(
            (skills / "rimworld-mod/scripts/source_cache.py").is_file()
        )

    def test_only_canonical_package_exists(self):
        self.assertTrue(PACKAGE.is_dir())
        self.assertFalse((ROOT / "basic").exists())
        self.assertFalse((ROOT / "image").exists())

    def test_every_marketplace_has_one_unified_entry(self):
        for path in MARKETPLACES:
            with self.subTest(path=path):
                plugins = read_json(path)["plugins"]
                self.assertEqual(1, len(plugins))
                self.assertEqual("rimworld-moddev", plugins[0]["name"])
                self.assertEqual(
                    "rimworld-moddev",
                    source_path(plugins[0]["source"]).removeprefix("./"),
                )

    def test_platform_manifests_use_unified_identity(self):
        for path in PLATFORM_MANIFESTS:
            with self.subTest(path=path):
                manifest = read_json(path)
                self.assertEqual("rimworld-moddev", manifest["name"])
                self.assertEqual("2.0.0", manifest["version"])

    def test_catalog_matches_skill_directories(self):
        catalog = read_json(PACKAGE / "skills/catalog.json")
        catalog_names = {skill["name"] for skill in catalog["skills"]}
        skill_names = {
            child.name
            for child in (PACKAGE / "skills").iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }
        self.assertEqual(skill_names, catalog_names)
        self.assertEqual(12, len(catalog_names))
        self.assertIn("rimworld-art", catalog_names)
        self.assertEqual(
            {
                "rimworld-16-debug",
                "rimworld-16-mod",
                "rimworld-17-debug",
                "rimworld-17-migrate",
                "rimworld-17-mod",
                "rimworld-debug",
                "rimworld-help",
                "rimworld-mod",
                "rimworld-publish",
                "rimworld-release",
                "rimworld-setup",
                "rimworld-art",
            },
            catalog_names,
        )
        self.assertNotIn("imageOnly", catalog)

    def test_relative_markdown_links_resolve(self):
        pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for markdown in (PACKAGE / "skills").rglob("*.md"):
            text = markdown.read_text(encoding="utf-8")
            for target in pattern.findall(text):
                if "://" in target or target.startswith("#"):
                    continue
                relative = target.split("#", 1)[0]
                with self.subTest(source=markdown, target=target):
                    self.assertTrue(
                        (markdown.parent / relative).resolve().exists(),
                        f"{markdown}: unresolved link {target}",
                    )

    def test_art_skill_has_required_specializations(self):
        art = PACKAGE / "skills/rimworld-art"
        expected = {
            "apparel-alignment.md",
            "render-space.md",
            "image-sources.md",
            "gfx-batch.md",
            "visual-acceptance.md",
        }
        self.assertEqual(
            expected,
            {path.name for path in (art / "references").glob("*.md")},
        )
        skill = (art / "SKILL.md").read_text(encoding="utf-8")
        apparel = (art / "references/apparel-alignment.md").read_text(
            encoding="utf-8"
        )
        render = (art / "references/render-space.md").read_text(
            encoding="utf-8"
        )
        for phrase in ("name: rimworld-art", "rimworld-mod", "apparel"):
            self.assertIn(phrase, skill)
        for phrase in ("body", "canvas", "alpha", "South", "North", "trim"):
            self.assertIn(phrase, apparel)
        for phrase in ("坐标轴", "图层", "着色器", "无人机", "三者"):
            self.assertIn(phrase, render)


if __name__ == "__main__":
    unittest.main()
