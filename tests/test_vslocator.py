#!/usr/bin/env python3
"""Tests for vslocator.

Run from the repository root:

    python -m unittest discover -s tests -p "test_vslocator.py" -v

The tests have two layers:

* Synthetic fixtures under a temporary directory exercise parsing, filtering,
  ordering, formatting and the CLI without needing Visual Studio installed.
* A real-machine layer auto-detects vswhere.exe and the local Visual Studio
  instances, then compares vslocator's output with vswhere's.  Those tests are
  skipped when either is unavailable.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vslocator  # noqa: E402


COMMUNITY = "Microsoft.VisualStudio.Product.Community"
BUILD_TOOLS = "Microsoft.VisualStudio.Product.BuildTools"
VC_TOOLS = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
ATLMFC = "Microsoft.VisualStudio.Component.VC.ATLMFC"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_instance(
    root: Path,
    instance_id: str,
    *,
    version: str = "17.14.37827.9",
    product: str = COMMUNITY,
    install_date: str = "2025-06-20T14:55:40.4245834Z",
    channel: str = "VisualStudio.17.Release",
    prerelease: bool = False,
    resources: list[dict[str, str]] | None = None,
    packages: list[dict[str, object]] | None = None,
    create_product: bool = True,
    installed: bool = True,
    expiration_date: str | None = None,
    retirement_date: str | None = None,
    files: dict[str, str] | None = None,
    extra_state: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """Create a fake _Instances/<id> directory and return (dir, install_dir)."""
    directory = root / instance_id
    install = directory / "install"
    install.mkdir(parents=True)
    product_relative = Path("Common7") / "IDE" / "devenv.exe"
    if create_product:
        product_file = install / product_relative
        product_file.parent.mkdir(parents=True)
        product_file.write_text("", encoding="utf-8")
    for relative, content in (files or {}).items():
        target = install / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if resources is None:
        resources = [
            {
                "language": "en-us",
                "title": "Visual Studio Test EN",
                "description": "English description",
            },
            {
                "language": "zh-cn",
                "title": "Visual Studio Test ZH",
                "description": "中文描述",
            },
        ]

    if packages is None:
        packages = [
            {"id": product, "version": version, "chip": "x64", "type": "Product"},
            {"id": VC_TOOLS, "version": version, "chip": "x64", "type": "Component"},
            {
                "id": "Microsoft.VisualStudio.Workload.NativeDesktop",
                "version": version,
                "type": "Workload",
            },
            {
                "id": "Microsoft.VisualStudio.Branding.Community",
                "version": version,
                "language": "zh-CN",
                "type": "Vsix",
                "productArch": "x64",
            },
        ]

    state: dict[str, object] = {
        "icon": {"mimeType": "image/svg+xml", "fileName": "product.svg"},
        "updateDate": "2026-11-04T13:11:41.4923586Z",
        "installDate": install_date,
        "enginePath": r"C:\Engine",
        "installationName": f"VisualStudio/{version}",
        "catalogInfo": {
            "id": f"VisualStudio/{version}",
            "productDisplayVersion": ".".join(version.split(".")[:3]),
            "productMilestoneIsPreRelease": "True" if prerelease else "False",
            "productName": "Visual Studio",
            "productLineVersion": version.split(".")[0],
        },
        "installationPath": str(install),
        "resolvedInstallationPath": str(install),
        "launchParams": {"fileName": "Common7\\IDE\\devenv.exe"},
        "installationVersion": version,
        "channelId": channel,
        "installedChannelId": channel,
        "channelUri": "https://example.invalid/channel",
        "installedChannelUri": "https://example.invalid/channel",
        "localizedResources": resources,
        "product": {"id": product, "version": version, "installed": installed},
        "properties": {
            "campaignId": "",
            "channelManifestId": "manifest",
            "nickname": "",
        },
        "releaseNotes": "https://example.invalid/notes",
        "thirdPartyNotices": "https://example.invalid/notices",
    }
    if expiration_date is not None:
        state["expirationDate"] = expiration_date
    if retirement_date is not None:
        state["retirementDate"] = retirement_date
    if extra_state:
        state.update(extra_state)

    (directory / "state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "state.packages.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    return directory, install


class FixtureTestCase(unittest.TestCase):
    """Base class that creates a temporary instance root and disables reboot."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._reboot_patch = mock.patch(
            "vslocator._reboot_required", return_value=False
        )
        self._reboot_patch.start()

    def tearDown(self) -> None:
        self._reboot_patch.stop()
        self._temporary.cleanup()

    def load(self, instance_id: str, user_locale: str = "en-US") -> vslocator.Instance:
        instance = vslocator.load_instance(self.root / instance_id, user_locale)
        self.assertIsNotNone(instance)
        return instance


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

class VersionTests(unittest.TestCase):
    def test_two_part_version_equals_padded_version(self) -> None:
        self.assertEqual(vslocator.Version("17.14"), vslocator.Version("17.14.0.0"))
        self.assertEqual(vslocator.Version("18"), vslocator.Version("18.0.0.0"))

    def test_ordering(self) -> None:
        self.assertLess(
            vslocator.Version("17.14.37827.9"),
            vslocator.Version("18.11.12328.437"),
        )
        self.assertLess(vslocator.Version("18.0"), vslocator.Version("18.0.0.1"))
        self.assertLess(vslocator.Version("17.9"), vslocator.Version("17.14"))

    def test_invalid(self) -> None:
        for text in ("", "17.", "17.14.1.2.3", "abc", "17.x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                vslocator.Version(text)

    def test_bare_range_is_minimum(self) -> None:
        version_range = vslocator.VersionRange.parse("17.0")
        self.assertTrue(version_range.contains(vslocator.Version("17.14.1")))
        self.assertTrue(version_range.contains(vslocator.Version("18.11.1")))
        self.assertFalse(version_range.contains(vslocator.Version("16.9")))

    def test_exclusive_upper_bound(self) -> None:
        version_range = vslocator.VersionRange.parse("[17.0,18.0)")
        self.assertTrue(version_range.contains(vslocator.Version("17.14.37827.9")))
        self.assertFalse(version_range.contains(vslocator.Version("18.0")))
        self.assertFalse(version_range.contains(vslocator.Version("18.11.1")))

    def test_inclusive_upper_bound(self) -> None:
        version_range = vslocator.VersionRange.parse("[17.0,18.0]")
        self.assertTrue(version_range.contains(vslocator.Version("18.0")))
        self.assertFalse(version_range.contains(vslocator.Version("18.0.0.1")))

    def test_exclusive_lower_bound(self) -> None:
        version_range = vslocator.VersionRange.parse("(17.0,)")
        self.assertFalse(version_range.contains(vslocator.Version("17.0")))
        self.assertTrue(version_range.contains(vslocator.Version("17.0.0.1")))

    def test_open_lower_bound(self) -> None:
        version_range = vslocator.VersionRange.parse("(,18.0)")
        self.assertTrue(version_range.contains(vslocator.Version("17.14")))
        self.assertFalse(version_range.contains(vslocator.Version("18.0")))

    def test_exact_version(self) -> None:
        version_range = vslocator.VersionRange.parse("[17.14.37827.9]")
        self.assertTrue(version_range.contains(vslocator.Version("17.14.37827.9")))
        self.assertFalse(version_range.contains(vslocator.Version("17.14.37827.10")))
        self.assertFalse(version_range.contains(vslocator.Version("17.14")))

    def test_invalid_ranges(self) -> None:
        for text in ("", "[]", "(17.0)", "[17.0", "17.0)"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                vslocator.VersionRange.parse(text)


# ---------------------------------------------------------------------------
# Instance parsing
# ---------------------------------------------------------------------------

class InstanceTests(FixtureTestCase):
    def test_core_fields(self) -> None:
        make_instance(self.root, "abc123")
        instance = self.load("abc123")

        self.assertEqual(instance.instance_id, "abc123")
        self.assertEqual(instance.install_date, "2025-06-20T14:55:40Z")
        self.assertEqual(instance.installation_version, "17.14.37827.9")
        self.assertEqual(instance.product_id, COMMUNITY)
        self.assertEqual(instance.state, vslocator.STATE_COMPLETE)
        self.assertTrue(instance.is_complete)
        self.assertTrue(instance.is_launchable)
        self.assertFalse(instance.is_prerelease)
        self.assertFalse(instance.is_reboot_required)
        self.assertEqual(instance.display_name, "Visual Studio Test EN")
        self.assertEqual(instance.description, "English description")
        self.assertTrue(instance.product_path.endswith("devenv.exe"))
        self.assertEqual(instance.catalog["productDisplayVersion"], "17.14.37827")
        self.assertEqual(instance.properties["channelManifestId"], "manifest")

    def test_localized_names(self) -> None:
        make_instance(self.root, "abc123")
        self.assertEqual(
            self.load("abc123", "zh-CN").display_name, "Visual Studio Test ZH"
        )
        self.assertEqual(self.load("abc123", "zh").display_name, "Visual Studio Test ZH")
        # Unknown locale falls back to en-US.
        self.assertEqual(
            self.load("abc123", "de-DE").display_name, "Visual Studio Test EN"
        )
        self.assertEqual(
            self.load("abc123", "fr-FR").display_name, "Visual Studio Test EN"
        )

    def test_packages_and_json_shape(self) -> None:
        make_instance(self.root, "abc123")
        instance = self.load("abc123")
        payload = instance.to_json(include_packages=True)
        packages = {package["id"]: package for package in payload["packages"]}

        self.assertEqual(len(packages), 4)
        self.assertEqual(packages[COMMUNITY]["chip"], "x64")
        branding = packages["Microsoft.VisualStudio.Branding.Community"]
        self.assertEqual(
            branding,
            {
                "id": "Microsoft.VisualStudio.Branding.Community",
                "version": "17.14.37827.9",
                "language": "zh-CN",
                "type": "Vsix",
            },
        )
        self.assertNotIn("productArch", branding)

    def test_optional_dates_and_key_order(self) -> None:
        make_instance(
            self.root,
            "abc123",
            expiration_date="2027-01-26T17:00:00+00:00",
            retirement_date="2027-04-26T17:00:00+00:00",
        )
        payload = self.load("abc123").to_json()
        keys = list(payload)

        self.assertLess(keys.index("enginePath"), keys.index("expirationDate"))
        self.assertLess(keys.index("expirationDate"), keys.index("installedChannelId"))
        self.assertLess(
            keys.index("resolvedInstallationPath"), keys.index("retirementDate")
        )
        self.assertLess(keys.index("retirementDate"), keys.index("thirdPartyNotices"))
        self.assertEqual(payload["expirationDate"], "2027-01-26T17:00:00+00:00")

    def test_incomplete_instance(self) -> None:
        make_instance(self.root, "broken", create_product=False)
        instance = self.load("broken")

        self.assertFalse(instance.is_launchable)
        self.assertFalse(instance.is_complete)
        self.assertEqual(instance.state, 0)
        # Default selection drops it; -all keeps it.
        self.assertEqual(
            vslocator.select_instances([instance]), []
        )
        self.assertEqual(
            vslocator.select_instances([instance], include_all=True), [instance]
        )

    def test_prerelease_detection(self) -> None:
        make_instance(self.root, "preview", channel="VisualStudio.17.Preview")
        make_instance(
            self.root,
            "milestone",
            extra_state={
                "catalogInfo": {
                    "id": "VisualStudio/17.15.0",
                    "productMilestoneIsPreRelease": "True",
                }
            },
        )
        self.assertTrue(self.load("preview").is_prerelease)
        self.assertTrue(self.load("milestone").is_prerelease)

    def test_property_lookup(self) -> None:
        make_instance(self.root, "abc123")
        instance = self.load("abc123")

        for name in (
            "catalog.productDisplayVersion",
            "catalog/productDisplayVersion",
            "catalog_productDisplayVersion",
            "CATALOG.productDisplayVersion",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    instance.get_property(name), "17.14.37827"
                )
        self.assertIs(instance.get_property("isComplete"), True)
        self.assertIs(instance.get_property("does.not.exist"), vslocator._MISSING)
        self.assertIs(instance.get_property("packages"), vslocator._MISSING)

    def test_load_instances_sorted_oldest_first(self) -> None:
        make_instance(self.root, "new", version="18.11.12328.437")
        make_instance(self.root, "old", version="17.14.37827.9")
        instances = vslocator.load_instances(self.root)
        self.assertEqual(
            [instance.instance_id for instance in instances], ["old", "new"]
        )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

class SelectionTests(FixtureTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_instance(self.root, "v17", version="17.14.37827.9")
        make_instance(self.root, "v18", version="18.11.12328.437")
        make_instance(
            self.root,
            "buildtools",
            version="17.9.1000.1",
            product=BUILD_TOOLS,
        )
        make_instance(
            self.root,
            "preview",
            version="18.12.1000.1",
            channel="VisualStudio.18.Preview",
            prerelease=True,
        )
        make_instance(
            self.root,
            "broken",
            version="16.0.0.1",
            create_product=False,
        )
        self.instances = vslocator.load_instances(self.root)

    def ids(self, selected: list[vslocator.Instance]) -> list[str]:
        return [instance.instance_id for instance in selected]

    def test_default_filters(self) -> None:
        self.assertEqual(
            self.ids(vslocator.select_instances(self.instances)),
            ["v17", "v18"],
        )

    def test_all_products_include_build_tools_but_not_prerelease(self) -> None:
        self.assertEqual(
            self.ids(
                vslocator.select_instances(self.instances, products=["*"])
            ),
            ["buildtools", "v17", "v18"],
        )

    def test_prerelease(self) -> None:
        self.assertEqual(
            self.ids(
                vslocator.select_instances(
                    self.instances, include_prerelease=True
                )
            ),
            ["v17", "v18", "preview"],
        )

    def test_include_all(self) -> None:
        self.assertEqual(
            self.ids(
                vslocator.select_instances(
                    self.instances, include_all=True
                )
            ),
            ["broken", "v17", "v18"],
        )

    def test_requires_all(self) -> None:
        make_instance(
            self.root,
            "atlmfc",
            version="18.13.1.1",
            packages=[
                {"id": COMMUNITY, "version": "18.13.1.1", "type": "Product"},
                {"id": VC_TOOLS, "version": "18.13.1.1", "type": "Component"},
                {"id": ATLMFC, "version": "18.13.1.1", "type": "Component"},
            ],
        )
        instances = vslocator.load_instances(self.root)
        selected = vslocator.select_instances(
            instances, requires=[VC_TOOLS, ATLMFC]
        )
        self.assertEqual(self.ids(selected), ["atlmfc"])

    def test_requires_any_and_wildcard(self) -> None:
        make_instance(
            self.root,
            "atlmfc",
            version="18.13.1.1",
            packages=[
                {"id": COMMUNITY, "version": "18.13.1.1", "type": "Product"},
                {"id": ATLMFC, "version": "18.13.1.1", "type": "Component"},
            ],
        )
        instances = vslocator.load_instances(self.root)

        any_selected = vslocator.select_instances(
            instances,
            requires=["Microsoft.VisualStudio.Component.DoesNotExist", ATLMFC],
            requires_any=True,
        )
        self.assertEqual(self.ids(any_selected), ["atlmfc"])

        wildcard = vslocator.select_instances(
            instances, requires=["microsoft.visualstudio.component.vc.tools*"]
        )
        self.assertEqual(self.ids(wildcard), ["v17", "v18"])

        none = vslocator.select_instances(
            instances, requires=["Microsoft.VisualStudio.Component.DoesNotExist"]
        )
        self.assertEqual(none, [])

    def test_version_range(self) -> None:
        selected = vslocator.select_instances(
            self.instances,
            products=["*"],
            version_range=vslocator.VersionRange.parse("[17.0,18.0)"),
        )
        self.assertEqual(self.ids(selected), ["buildtools", "v17"])

    def test_latest_and_sort(self) -> None:
        self.assertEqual(
            self.ids(vslocator.select_instances(self.instances, latest=True)),
            ["v18"],
        )
        self.assertEqual(
            self.ids(vslocator.select_instances(self.instances, sort=True)),
            ["v18", "v17"],
        )

    def test_path_lookup(self) -> None:
        v17 = self.load("v17")
        v18 = self.load("v18")
        nested = Path(v17.installation_path) / "Common7" / "IDE"
        self.assertIs(
            vslocator.instance_for_path([v18, v17], nested), v17
        )
        self.assertIsNone(
            vslocator.instance_for_path(
                [v17], v17.installation_path + "X"
            )
        )
        self.assertIs(
            vslocator.instance_for_path([v18, v17], v17.installation_path),
            v17,
        )

    def test_path_lookup_case_insensitive(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows paths are case-insensitive")
        v17 = self.load("v17")
        self.assertIs(
            vslocator.instance_for_path(
                [v17], v17.installation_path.upper()
            ),
            v17,
        )


class FindTests(FixtureTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_instance(
            self.root,
            "abc123",
            files={
                "Common7/IDE/devenv.exe": "",
                "Common7/IDE/foo.dll": "",
                "VC/bin/cl.exe": "",
                "devenv.exe": "",
            },
        )
        self.instance = self.load("abc123")
        self.install = Path(self.instance.installation_path)

    def test_exact_relative_path(self) -> None:
        found = vslocator.find_files(self.instance, "Common7\\IDE\\devenv.exe")
        self.assertEqual(found, [str(self.install / "Common7" / "IDE" / "devenv.exe")])

    def test_question_mark(self) -> None:
        found = vslocator.find_files(self.instance, "Common7\\IDE\\?evenv.exe")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("devenv.exe"))

    def test_star_does_not_cross_directories(self) -> None:
        self.assertEqual(
            vslocator.find_files(self.instance, "*.exe"),
            [str(self.install / "devenv.exe")],
        )

    def test_double_star(self) -> None:
        found = vslocator.find_files(self.instance, "**\\cl.exe")
        self.assertEqual(found, [str(self.install / "VC" / "bin" / "cl.exe")])

    def test_no_match_and_traversal(self) -> None:
        self.assertEqual(vslocator.find_files(self.instance, "nope.xyz"), [])
        self.assertEqual(vslocator.find_files(self.instance, "..\\secret.txt"), [])


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class FormattingTests(FixtureTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_instance(
            self.root,
            "abc123",
            expiration_date="2027-01-26T17:00:00+00:00",
        )
        self.instance = self.load("abc123")

    def test_text_items_flatten_and_sort(self) -> None:
        items = dict(self.instance.to_text_items())
        self.assertEqual(items["isComplete"], 1)
        self.assertEqual(items["isPrerelease"], 0)
        self.assertIn("catalog_productDisplayVersion", items)
        self.assertIn("properties_channelManifestId", items)
        self.assertNotIn("catalog", items)
        self.assertNotIn("properties", items)
        self.assertRegex(
            items["installDate"], r"^\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2}$"
        )

        catalog_keys = [
            key for key, _ in self.instance.to_text_items()
            if key.startswith("catalog_")
        ]
        self.assertEqual(catalog_keys, sorted(catalog_keys))

    def run_main(self, *args: str) -> str:
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "VSLOCATOR_INSTANCES_ROOT": str(self.root),
                "VSLOCATOR_REBOOT_REQUIRED": "0",
            },
        ), contextlib.redirect_stdout(output):
            status = vslocator.main([*args, "-products", "*"])
        self.assertEqual(status, 0)
        return output.getvalue()

    def test_json_format(self) -> None:
        payload = json.loads(self.run_main("-format", "json"))
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["instanceId"], "abc123")
        self.assertIs(payload[0]["isComplete"], True)
        self.assertEqual(payload[0]["catalog"]["productDisplayVersion"], "17.14.37827")

    def test_json_property_nesting(self) -> None:
        payload = json.loads(
            self.run_main("-property", "catalog.productDisplayVersion", "-format", "json")
        )
        self.assertEqual(
            payload,
            [{"catalog": {"productDisplayVersion": "17.14.37827"}}],
        )

    def test_xml_format(self) -> None:
        xml_text = self.run_main("-format", "xml")
        root = ET.fromstring(xml_text.split("?>", 1)[1])
        self.assertEqual(root.tag, "instances")
        instance = root.find("instance")
        self.assertIsNotNone(instance)
        self.assertEqual(instance.findtext("instanceId"), "abc123")
        self.assertEqual(instance.findtext("isComplete"), "1")
        catalog_tags = [child.tag for child in instance.find("catalog")]
        self.assertEqual(catalog_tags, sorted(catalog_tags))

    def test_text_format(self) -> None:
        text = self.run_main("-format", "text")
        self.assertIn("instanceId: abc123", text)
        self.assertIn("isComplete: 1", text)
        self.assertIn("catalog_productDisplayVersion: 17.14.37827", text)
        self.assertIn("properties_channelManifestId: manifest", text)
        self.assertIn("catalog_productName: Visual Studio", text)
        # No raw dicts in text output.
        self.assertNotIn("catalog: {", text)

    def test_value_format(self) -> None:
        text = self.run_main("-property", "installationVersion")
        self.assertEqual(text.strip(), "17.14.37827.9")

    def test_property_text_format(self) -> None:
        text = self.run_main(
            "-property", "catalog.productDisplayVersion", "-format", "text"
        )
        self.assertEqual(
            text.strip(), "catalog_productDisplayVersion: 17.14.37827"
        )

    def test_property_date_value_format(self) -> None:
        text = self.run_main("-property", "installDate")
        self.assertRegex(
            text.strip(), r"^\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2}$"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTests(FixtureTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_instance(self.root, "v17", version="17.14.37827.9")
        make_instance(self.root, "v18", version="18.11.12328.437")
        self.script = ROOT / "vslocator.py"
        self.env = os.environ.copy()
        self.env["VSLOCATOR_INSTANCES_ROOT"] = str(self.root)
        self.env["VSLOCATOR_REBOOT_REQUIRED"] = "0"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            check=False,
        )

    def test_json(self) -> None:
        result = self.run_cli("-all", "-products", "*", "-format", "json", "-utf8")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [item["instanceId"] for item in payload], ["v17", "v18"]
        )

    def test_latest_property(self) -> None:
        result = self.run_cli("-latest", "-property", "installationVersion")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "18.11.12328.437")

    def test_requires(self) -> None:
        result = self.run_cli(
            "-requires", VC_TOOLS, "-property", "installationVersion"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            ["17.14.37827.9", "18.11.12328.437"],
        )

    def test_version_range(self) -> None:
        result = self.run_cli(
            "-version", "[17.0,18.0)", "-property", "installationVersion"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "17.14.37827.9")

    def test_find(self) -> None:
        result = self.run_cli(
            "-products", "*", "-find", "Common7\\IDE\\devenv.exe", "-utf8"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith("devenv.exe") for line in lines))

    def test_find_json(self) -> None:
        result = self.run_cli(
            "-products", "*", "-find", "Common7\\IDE\\devenv.exe",
            "-format", "json", "-utf8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 2)

    def test_help(self) -> None:
        result = self.run_cli("-?")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: vslocator", result.stdout)

    def test_invalid_version_range(self) -> None:
        result = self.run_cli("-version", "not-a-range")
        self.assertEqual(result.returncode, 87)
        self.assertIn("invalid version", result.stderr)

    def test_property_with_find_is_rejected(self) -> None:
        result = self.run_cli("-find", "*.exe", "-property", "installationPath")
        self.assertEqual(result.returncode, 87)
        self.assertIn("cannot be specified", result.stderr)

    def test_legacy_is_rejected(self) -> None:
        result = self.run_cli("-legacy")
        self.assertEqual(result.returncode, 87)
        self.assertIn("not supported", result.stderr)

    def test_value_without_property_is_rejected(self) -> None:
        result = self.run_cli("-format", "value")
        self.assertEqual(result.returncode, 87)
        self.assertIn("requires", result.stderr)


# ---------------------------------------------------------------------------
# Comparison with the real vswhere and real Visual Studio instances
# ---------------------------------------------------------------------------

def _real_vswhere() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _real_instances_root() -> Path:
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(program_data) / "Microsoft" / "VisualStudio" / "Packages" / "_Instances"


REAL_VSWHERE = _real_vswhere()
REAL_ROOT = _real_instances_root()
HAS_REAL_INSTANCES = REAL_ROOT.is_dir() and any(
    child.is_dir() for child in REAL_ROOT.iterdir()
)


@unittest.skipUnless(
    REAL_VSWHERE is not None and HAS_REAL_INSTANCES,
    "vswhere.exe or real Visual Studio instances are not available",
)
class RealMachineTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = ROOT / "vslocator.py"
        cls.env = os.environ.copy()
        cls.env.pop("VSLOCATOR_INSTANCES_ROOT", None)
        cls.env.pop("VSLOCATOR_REBOOT_REQUIRED", None)

    def run_both(self, *args: str) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        ours = subprocess.run(
            [sys.executable, str(self.script), *args, "-utf8"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            check=False,
        )
        theirs = subprocess.run(
            [str(REAL_VSWHERE), "-nologo", *args, "-utf8"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            check=False,
        )
        return ours, theirs

    def test_full_json_matches(self) -> None:
        ours, theirs = self.run_both("-all", "-products", "*", "-format", "json")
        self.assertEqual(ours.returncode, 0, ours.stderr)
        self.assertEqual(theirs.returncode, 0, theirs.stderr)
        our_data = json.loads(ours.stdout)
        their_data = json.loads(theirs.stdout)
        self.assertEqual(len(our_data), len(their_data))

        by_id = {item["instanceId"]: item for item in their_data}
        for item in our_data:
            with self.subTest(instance=item["instanceId"]):
                expected = by_id[item["instanceId"]]
                self.assertEqual(list(item), list(expected))
                for key in expected:
                    if key == "packages":
                        continue
                    self.assertEqual(item[key], expected[key], key)

    def test_packages_match(self) -> None:
        ours, theirs = self.run_both(
            "-products", "*", "-include", "packages", "-format", "json"
        )
        self.assertEqual(ours.returncode, 0, ours.stderr)
        our_data = json.loads(ours.stdout)
        their_data = json.loads(theirs.stdout)

        def fingerprint(package: dict[str, object]) -> tuple[tuple[str, object], ...]:
            return tuple(sorted(package.items()))

        by_id = {item["instanceId"]: item for item in their_data}
        for item in our_data:
            with self.subTest(instance=item["instanceId"]):
                expected = by_id[item["instanceId"]]["packages"]
                self.assertEqual(
                    {fingerprint(package) for package in item["packages"]},
                    {fingerprint(package) for package in expected},
                )

    def test_text_matches(self) -> None:
        ours, theirs = self.run_both("-all", "-products", "*", "-format", "text")
        self.assertEqual(ours.returncode, 0, ours.stderr)

        def normalise(text: str) -> str:
            # Date formatting is locale-specific; everything else must match.
            return "\n".join(
                "installDate: <date>"
                if line.startswith("installDate: ")
                else line
                for line in text.splitlines()
            )

        self.assertEqual(normalise(ours.stdout), normalise(theirs.stdout))

    def test_xml_matches(self) -> None:
        ours, theirs = self.run_both("-all", "-products", "*", "-format", "xml")
        self.assertEqual(ours.returncode, 0, ours.stderr)
        self.assertEqual(ours.stdout, theirs.stdout)

    def test_selection_outputs_match(self) -> None:
        cases = [
            ("-latest", "-property", "installationVersion"),
            ("-sort", "-property", "installationVersion"),
            ("-version", "[17.0,18.0)", "-property", "installationVersion"),
            ("-requires", VC_TOOLS, "-property", "installationVersion"),
            ("-property", "catalog.productDisplayVersion", "-format", "json"),
            ("-property", "installationPath"),
            ("-find", "Common7\\IDE\\devenv.exe", "-format", "json"),
        ]
        for args in cases:
            with self.subTest(args=args):
                ours, theirs = self.run_both(*args)
                self.assertEqual(ours.returncode, 0, ours.stderr)
                self.assertEqual(ours.stdout, theirs.stdout)

    def test_path_matches(self) -> None:
        instances = vslocator.load_instances(REAL_ROOT)
        self.assertTrue(instances)
        path = str(Path(instances[0].installation_path) / "Common7")
        ours, theirs = self.run_both("-path", path, "-property", "installationPath")
        self.assertEqual(ours.returncode, 0, ours.stderr)
        self.assertEqual(ours.stdout, theirs.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
