#!/usr/bin/env python3
"""vslocator - a small, open-source, dependency-free reimplementation of vswhere.

How it works
------------
vswhere.exe talks to the Visual Studio Setup Configuration COM API.  That API is
itself backed by the per-instance state that the VS installer writes here:

    %ProgramData%\\Microsoft\\VisualStudio\\Packages\\_Instances\\<instanceId>\\
        state.json           - identity, paths, catalog, localized names, ...
        state.packages.json  - every package installed in the instance

Reading those files directly gives the same data without COM, without a
registration-free manifest, and without any third-party packages.  Only the two
small JSON files are read (state.json is ~18 KB); the huge catalog.json is not
touched.

The command line mirrors the useful subset of vswhere:

    vslocator -latest -property installationPath
    vslocator -all -products * -format json
    vslocator -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64
    vslocator -version "[17.0,18.0)" -property installationVersion
    vslocator -find "Common7\\IDE\\devenv.exe"
    vslocator -path "C:\\src\\myproject" -property installationPath

Exit codes: 0 on success, 2 for command-line syntax errors, 87 (the Win32
ERROR_INVALID_PARAMETER that vswhere uses) for invalid option values.

Only Visual Studio 2017 and newer expose this state.  Visual Studio 2015 and
older (the vswhere -legacy option) are intentionally not supported.

License: MIT.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import fnmatch
import json
import locale
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import total_ordering
from pathlib import Path
from typing import Any, Iterable, Sequence

__version__ = "0.1.0"

# Instance state reported by ISetupInstance2::GetState for a healthy instance.
STATE_COMPLETE = 0xFFFFFFFF

# vswhere's default product filter (BuildTools requires -products *).
DEFAULT_PRODUCTS = (
    "Microsoft.VisualStudio.Product.Community",
    "Microsoft.VisualStudio.Product.Professional",
    "Microsoft.VisualStudio.Product.Enterprise",
)

_MISSING = object()


# ---------------------------------------------------------------------------
# Version numbers and NuGet-style version ranges
# ---------------------------------------------------------------------------

@total_ordering
class Version:
    """A dotted Visual Studio version such as 17.14.37827.9."""

    __slots__ = ("parts",)

    _PATTERN = re.compile(r"^\d+(?:\.\d+){0,3}$")

    def __init__(self, text: str):
        text = str(text).strip()
        if not self._PATTERN.match(text):
            raise ValueError(f"invalid version: {text!r}")
        parts = [int(part) for part in text.split(".")]
        parts.extend([0] * (4 - len(parts)))
        self.parts = tuple(parts)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.parts == other.parts

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.parts < other.parts

    def __hash__(self) -> int:
        return hash(self.parts)

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.parts)


@dataclass(frozen=True)
class VersionRange:
    """A NuGet-style version range, e.g. ``[15.0,16.0)`` or ``17.14``.

    A bare version means "at least this version" (matching vswhere).
    """

    minimum: Version | None = None
    maximum: Version | None = None
    min_inclusive: bool = True
    max_inclusive: bool = False
    exact: bool = False

    def contains(self, version: Version) -> bool:
        if self.exact:
            return version == self.minimum
        if self.minimum is not None:
            if version < self.minimum:
                return False
            if version == self.minimum and not self.min_inclusive:
                return False
        if self.maximum is not None:
            if version > self.maximum:
                return False
            if version == self.maximum and not self.max_inclusive:
                return False
        return True

    @classmethod
    def parse(cls, text: str) -> "VersionRange":
        text = text.strip()
        if not text:
            raise ValueError("empty version range")
        if text[0] not in "[(":
            # Bare version: minimum, inclusive.
            return cls(minimum=Version(text), min_inclusive=True)
        if text[-1] not in "])" or len(text) < 2:
            raise ValueError(f"invalid version range: {text!r}")

        lower_inclusive = text[0] == "["
        upper_inclusive = text[-1] == "]"
        body = text[1:-1]

        if "," not in body:
            # Only [x] is valid: an exact version.
            if not (lower_inclusive and upper_inclusive):
                raise ValueError(f"invalid version range: {text!r}")
            version = Version(body.strip())
            return cls(
                minimum=version,
                maximum=version,
                min_inclusive=True,
                max_inclusive=True,
                exact=True,
            )

        lower_text, upper_text = (part.strip() for part in body.split(",", 1))
        if not lower_text and not upper_text:
            raise ValueError(f"invalid version range: {text!r}")

        minimum = Version(lower_text) if lower_text else None
        maximum = Version(upper_text) if upper_text else None
        return cls(
            minimum=minimum,
            maximum=maximum,
            min_inclusive=lower_inclusive,
            max_inclusive=upper_inclusive,
        )


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

@dataclass
class Instance:
    """One Visual Studio installation, modelled on vswhere's JSON shape."""

    instance_id: str
    install_date: str
    installation_name: str
    installation_path: str
    installation_version: str
    product_id: str
    product_path: str | None
    state: int
    is_complete: bool
    is_launchable: bool
    is_prerelease: bool
    is_reboot_required: bool
    display_name: str
    description: str
    channel_id: str
    channel_uri: str
    engine_path: str
    installed_channel_id: str
    installed_channel_uri: str
    release_notes: str
    resolved_installation_path: str
    third_party_notices: str
    update_date: str
    catalog: dict[str, Any]
    properties: dict[str, Any]
    expiration_date: str | None = None
    retirement_date: str | None = None
    directory: Path | None = field(default=None, repr=False, compare=False)
    _packages: list[dict[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )

    # -- construction -----------------------------------------------------

    @classmethod
    def from_directory(cls, directory: Path, user_locale: str | None = None) -> "Instance | None":
        state_file = directory / "state.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict):
            return None

        installation_path = state.get("installationPath") or ""
        launch = state.get("launchParams") or {}
        relative_product = launch.get("fileName") or launch.get("path") or ""
        product_path = None
        if installation_path and relative_product:
            product_path = os.path.normpath(
                os.path.join(installation_path, relative_product)
            )

        product = state.get("product") or {}
        catalog = state.get("catalogInfo") or {}
        channel_id = state.get("channelId") or ""
        reboot_required = _reboot_required()
        launchable = bool(product_path and os.path.isfile(product_path))
        installed = bool(product.get("installed", True))
        complete = bool(
            installed
            and installation_path
            and os.path.isdir(installation_path)
            and launchable
            and not reboot_required
        )

        resources = state.get("localizedResources")
        if not isinstance(resources, list):
            resources = []
        resources = [item for item in resources if isinstance(item, dict)]
        resource = _pick_localized(resources, user_locale)
        display_name = resource.get("title") or _fallback_display_name(state)
        description = resource.get("description") or ""
        properties = state.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        return cls(
            instance_id=directory.name,
            install_date=_normalise_utc(state.get("installDate") or ""),
            installation_name=state.get("installationName") or "",
            installation_path=installation_path,
            installation_version=state.get("installationVersion") or "",
            product_id=product.get("id") or "",
            product_path=product_path,
            state=STATE_COMPLETE if complete else 0,
            is_complete=complete,
            is_launchable=launchable,
            is_prerelease=_is_prerelease(catalog, channel_id),
            is_reboot_required=reboot_required,
            display_name=display_name,
            description=description,
            channel_id=channel_id,
            channel_uri=state.get("channelUri") or "",
            engine_path=state.get("enginePath") or "",
            installed_channel_id=state.get("installedChannelId") or channel_id,
            installed_channel_uri=state.get("installedChannelUri")
            or state.get("channelUri")
            or "",
            release_notes=state.get("releaseNotes") or "",
            resolved_installation_path=state.get("resolvedInstallationPath")
            or installation_path,
            third_party_notices=state.get("thirdPartyNotices") or "",
            update_date=state.get("updateDate") or "",
            catalog=catalog if isinstance(catalog, dict) else {},
            properties=properties,
            expiration_date=state.get("expirationDate"),
            retirement_date=state.get("retirementDate"),
            directory=directory,
        )

    # -- packages ---------------------------------------------------------

    def packages(self) -> list[dict[str, Any]]:
        """Installed packages, loaded lazily from state.packages.json."""
        if self._packages is None:
            packages: list[dict[str, Any]] = []
            if self.directory is not None:
                file = self.directory / "state.packages.json"
                try:
                    data = json.loads(file.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    data = {}
                raw = data.get("packages") if isinstance(data, dict) else None
                if isinstance(raw, list):
                    packages = [item for item in raw if isinstance(item, dict)]
            self._packages = packages
        return self._packages

    # -- serialisation ----------------------------------------------------

    def to_json(self, include_packages: bool = False) -> dict[str, Any]:
        """Return the object vswhere emits for this instance.

        Key order follows vswhere so that text/XML output is familiar.
        """
        result: dict[str, Any] = {
            "instanceId": self.instance_id,
            "installDate": self.install_date,
            "installationName": self.installation_name,
            "installationPath": self.installation_path,
            "installationVersion": self.installation_version,
            "productId": self.product_id,
            "productPath": self.product_path,
            "state": self.state,
            "isComplete": self.is_complete,
            "isLaunchable": self.is_launchable,
            "isPrerelease": self.is_prerelease,
            "isRebootRequired": self.is_reboot_required,
            "displayName": self.display_name,
            "description": self.description,
            "channelId": self.channel_id,
            "channelUri": self.channel_uri,
            "enginePath": self.engine_path,
        }
        if self.expiration_date is not None:
            result["expirationDate"] = self.expiration_date
        result.update(
            {
                "installedChannelId": self.installed_channel_id,
                "installedChannelUri": self.installed_channel_uri,
                "releaseNotes": self.release_notes,
                "resolvedInstallationPath": self.resolved_installation_path,
            }
        )
        if self.retirement_date is not None:
            result["retirementDate"] = self.retirement_date
        result["thirdPartyNotices"] = self.third_party_notices
        result["updateDate"] = self.update_date
        result["catalog"] = dict(self.catalog)
        result["properties"] = dict(self.properties)
        if include_packages:
            result["packages"] = [_package_to_json(pkg) for pkg in self.packages()]
        return result

    def get_property(
        self, name: str, include_packages: bool = False
    ) -> Any:
        """Look up ``catalog.productDisplayVersion``-style property names.

        vswhere accepts ``.``, ``/`` and ``_`` as separators.  Returns the
        module-level ``_MISSING`` sentinel when the property does not exist.
        """
        current: Any = self.to_json(include_packages=include_packages)
        for part in _property_parts(name):
            if not isinstance(current, dict):
                return _MISSING
            if part in current:
                current = current[part]
                continue
            folded = {str(key).casefold(): key for key in current}
            if part.casefold() in folded:
                current = current[folded[part.casefold()]]
                continue
            return _MISSING
        return current

    def to_text_items(self) -> list[tuple[str, Any]]:
        """Flatten an instance the way vswhere's text format does."""
        items: list[tuple[str, Any]] = []
        for key, value in self.to_json().items():
            if key in ("catalog", "properties"):
                continue
            if key == "installDate":
                value = _format_local_date(value)
            elif isinstance(value, bool):
                value = int(value)
            items.append((key, value))
        for key in sorted(self.catalog):
            items.append((f"catalog_{key}", self.catalog[key]))
        for key in sorted(self.properties):
            items.append((f"properties_{key}", self.properties[key]))
        return items


# ---------------------------------------------------------------------------
# Reading instances from disk
# ---------------------------------------------------------------------------

def default_instances_root() -> Path:
    """Return the directory where the VS installer keeps instance state."""
    override = os.environ.get("VSLOCATOR_INSTANCES_ROOT")
    if override:
        return Path(override)
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(program_data) / "Microsoft" / "VisualStudio" / "Packages" / "_Instances"


def load_instance(
    directory: Path | str, user_locale: str | None = None
) -> Instance | None:
    return Instance.from_directory(Path(directory), user_locale)


def load_instances(
    root: Path | str | None = None, user_locale: str | None = None
) -> list[Instance]:
    """Load every instance under *root*, oldest version first."""
    directory = Path(root) if root is not None else default_instances_root()
    if not directory.is_dir():
        return []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    instances: list[Instance] = []
    for child in children:
        if not child.is_dir():
            continue
        instance = load_instance(child, user_locale)
        if instance is not None:
            instances.append(instance)
    instances.sort(key=_default_sort_key)
    return instances


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_instances(
    instances: Sequence[Instance],
    *,
    include_all: bool = False,
    include_prerelease: bool = False,
    products: Sequence[str] | None = None,
    version_range: VersionRange | None = None,
    requires: Sequence[str] = (),
    requires_any: bool = False,
    latest: bool = False,
    sort: bool = False,
) -> list[Instance]:
    """Apply vswhere's filters and ordering."""
    selected = [inst for inst in instances if include_all or inst.is_complete]
    if not include_prerelease:
        selected = [inst for inst in selected if not inst.is_prerelease]
    selected = [inst for inst in selected if _matches_products(inst, products)]
    if version_range is not None:
        in_range = []
        for inst in selected:
            version = _safe_version(inst.installation_version)
            if version is not None and version_range.contains(version):
                in_range.append(inst)
        selected = in_range
    if requires:
        selected = [
            inst
            for inst in selected
            if _matches_requires(inst, requires, requires_any)
        ]

    if latest and selected:
        selected = [max(selected, key=_default_sort_key)]
    elif sort:
        selected = sorted(selected, key=_default_sort_key, reverse=True)
    return selected


def instance_for_path(
    instances: Iterable[Instance], path: str | os.PathLike[str]
) -> Instance | None:
    """Return the most specific instance whose installation contains *path*."""
    target = os.path.normcase(os.path.abspath(os.fspath(path)))
    best: tuple[int, Instance] | None = None
    for instance in instances:
        roots = {
            instance.installation_path,
            instance.resolved_installation_path,
        }
        for root in roots:
            if not root:
                continue
            normalised = os.path.normcase(os.path.abspath(root))
            if target == normalised or target.startswith(normalised + os.sep):
                if best is None or len(normalised) > best[0]:
                    best = (len(normalised), instance)
    return best[1] if best else None


def find_files(instance: Instance, pattern: str) -> list[str]:
    """Expand a vswhere -find pattern (``*``, ``?``, ``**``) under an instance."""
    relative = pattern.replace("/", "\\").strip().lstrip("\\")
    if not relative or ".." in relative.split("\\"):
        return []
    base = instance.installation_path
    if not base or not os.path.isdir(base):
        return []
    glob_pattern = relative.replace("\\", "/")
    try:
        matches = Path(base).glob(glob_pattern)
        return sorted(
            str(path) for path in matches if path.is_file()
        )
    except (OSError, ValueError, NotImplementedError):
        return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def emit_json(payload: Any) -> None:
    _write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def emit_text(instances: Sequence[Instance]) -> None:
    blocks = []
    for instance in instances:
        lines = [
            f"{key}: {_text_value(value)}"
            for key, value in instance.to_text_items()
        ]
        blocks.append("\n".join(lines))
    if blocks:
        _write("\n\n".join(blocks) + "\n")


def emit_value(values: Sequence[Any]) -> None:
    for value in values:
        _write(f"{_text_value(value)}\n")


def emit_xml(payload: Sequence[dict[str, Any]]) -> None:
    root = ET.Element("instances")
    for item in payload:
        instance_node = ET.SubElement(root, "instance")
        _append_xml(instance_node, item)
    ET.indent(root, space="  ")
    _write('<?xml version="1.0"?>\n')
    _write(
        ET.tostring(root, encoding="unicode", short_empty_elements=False) + "\n"
    )


def _append_xml(
    parent: ET.Element, data: Any, *, sort_keys: bool = False
) -> None:
    if isinstance(data, dict):
        keys = sorted(data) if sort_keys else data.keys()
        for key in keys:
            child = ET.SubElement(parent, str(key))
            # vswhere emits catalog and property bags alphabetically, but
            # keeps package fields in their natural order.
            _append_xml(child, data[key], sort_keys=key in ("catalog", "properties"))
    elif isinstance(data, list):
        for item in data:
            child = ET.SubElement(parent, "package" if isinstance(item, dict) else "path")
            _append_xml(child, item)
    elif data is not None:
        parent.text = "1" if data is True else "0" if data is False else str(data)


def _text_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_property_value(name: str, value: Any) -> str:
    """Format a single property the way vswhere's text/value formats do."""
    parts = _property_parts(name)
    if parts and parts[-1] == "installDate" and isinstance(value, str):
        return _format_local_date(value)
    return _text_value(value)


def _package_to_json(package: dict[str, Any]) -> dict[str, Any]:
    """Map a state.packages.json entry to vswhere's package object."""
    result: dict[str, Any] = {}
    for source, target in (
        ("id", "id"),
        ("version", "version"),
        ("chip", "chip"),
        ("language", "language"),
        ("type", "type"),
    ):
        value = package.get(source)
        if value is not None:
            result[target] = value
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches_products(
    instance: Instance, products: Sequence[str] | None
) -> bool:
    wanted = DEFAULT_PRODUCTS if products is None else products
    if "*" in wanted:
        return True
    folded = {product.casefold() for product in wanted}
    return instance.product_id.casefold() in folded


def _matches_requires(
    instance: Instance, patterns: Sequence[str], any_mode: bool
) -> bool:
    package_ids = [
        str(package.get("id", "")).casefold()
        for package in instance.packages()
    ]

    def matches(pattern: str) -> bool:
        pattern = pattern.casefold()
        return any(fnmatch.fnmatchcase(package_id, pattern) for package_id in package_ids)

    if any_mode:
        return any(matches(pattern) for pattern in patterns)
    return all(matches(pattern) for pattern in patterns)


def _default_sort_key(instance: Instance) -> tuple[Version, str]:
    return (_safe_version(instance.installation_version) or Version("0"), instance.install_date)


def _safe_version(text: str) -> Version | None:
    try:
        return Version(text)
    except ValueError:
        return None


def _property_parts(name: str) -> list[str]:
    return [part for part in re.split(r"[./_]", name) if part]


def _is_prerelease(catalog: dict[str, Any], channel_id: str) -> bool:
    milestone = str(catalog.get("productMilestoneIsPreRelease", "")).casefold()
    channel = channel_id.casefold()
    return milestone == "true" or any(
        marker in channel for marker in ("preview", "insiders", "prerelease")
    )


def _fallback_display_name(state: dict[str, Any]) -> str:
    catalog = state.get("catalogInfo")
    if not isinstance(catalog, dict):
        catalog = {}
    product = state.get("product")
    if not isinstance(product, dict):
        product = {}
    name = catalog.get("productName") or ""
    line = catalog.get("productLineVersion") or ""
    product_id = product.get("id") or ""
    edition = product_id.rsplit(".", 1)[-1] if product_id else ""
    return " ".join(part for part in (name, edition, line) if part)


def _pick_localized(
    resources: Sequence[dict[str, Any]], user_locale: str | None
) -> dict[str, Any]:
    if not resources:
        return {}
    wanted = (user_locale or "en-US").replace("_", "-").casefold()
    for resource in resources:
        if str(resource.get("language", "")).replace("_", "-").casefold() == wanted:
            return resource
    language = wanted.split("-", 1)[0]
    for resource in resources:
        if str(resource.get("language", "")).replace("_", "-").casefold().split("-", 1)[0] == language:
            return resource
    for resource in resources:
        if str(resource.get("language", "")).replace("_", "-").casefold() == "en-us":
            return resource
    return resources[0]


def user_locale() -> str:
    """Best-effort Windows user UI locale, e.g. ``zh-CN``."""
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            buffer = ctypes.create_unicode_buffer(85)
            if kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                return buffer.value
        except (AttributeError, OSError):
            pass
    try:
        return locale.getlocale()[0] or "en-US"
    except (ValueError, TypeError):
        return "en-US"


def _reboot_required() -> bool:
    """Check the installer's machine-wide pending-reboot flag, if present."""
    override = os.environ.get("VSLOCATOR_REBOOT_REQUIRED")
    if override is not None:
        return override.strip().casefold() not in ("", "0", "false", "no")
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:
        return False
    for subkey in (
        r"SOFTWARE\Microsoft\VisualStudio\Setup",
        r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\Setup",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "RebootRequired")
                if str(value).strip().casefold() not in ("", "0", "false", "no"):
                    return True
        except OSError:
            continue
    return False


_ISO_PATTERN = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.\d+)?"
    r"(?P<zone>Z|[+-]\d{2}:?\d{2})$"
)


def _normalise_utc(text: str) -> str:
    """Turn an ISO timestamp into vswhere's second-precision UTC form."""
    match = _ISO_PATTERN.match(text)
    if not match:
        return text
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    try:
        value = _dt.datetime.fromisoformat(match.group("base") + zone)
        value = value.astimezone(_dt.timezone.utc)
    except ValueError:
        return text
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_local_date(text: str) -> str:
    try:
        value = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        value = value.astimezone()
    except ValueError:
        return text
    return f"{value.year}/{value.month}/{value.day} {value:%H:%M:%S}"


def _write(text: str) -> None:
    sys.stdout.write(text)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vslocator",
        description="Locate Visual Studio instances (an open-source vswhere).",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-all", action="store_true", dest="include_all",
                        help="include incomplete instances")
    parser.add_argument("-prerelease", action="store_true", dest="prerelease",
                        help="include prerelease instances")
    parser.add_argument("-products", nargs="+", metavar="ID",
                        help="product IDs to include, or * for all")
    parser.add_argument("-requires", nargs="+", metavar="ID",
                        help="required package IDs (wildcards allowed)")
    parser.add_argument("-requiresAny", action="store_true", dest="requires_any",
                        help="match any -requires ID instead of all")
    parser.add_argument("-version", metavar="RANGE",
                        help="version range, e.g. [17.0,18.0)")
    parser.add_argument("-latest", action="store_true",
                        help="return only the newest instance")
    parser.add_argument("-sort", action="store_true",
                        help="sort newest first")
    parser.add_argument("-path", metavar="PATH",
                        help="find the instance containing PATH")
    parser.add_argument("-format", choices=("json", "text", "value", "xml"),
                        help="output format (default: text, or value with -property/-find)")
    parser.add_argument("-property", metavar="NAME",
                        help="return only this property")
    parser.add_argument("-include", nargs="+", metavar="NAME",
                        help="extra properties to include (packages)")
    parser.add_argument("-find", metavar="PATTERN",
                        help="find files under each installation path")
    parser.add_argument("-legacy", action="store_true",
                        help="unsupported: Visual Studio 2015 and older")
    parser.add_argument("-nologo", action="store_true", dest="nologo",
                        help="accepted for compatibility (no logo is printed)")
    parser.add_argument("-nocolor", action="store_true", dest="nocolor",
                        help="accepted for compatibility")
    parser.add_argument("-utf8", action="store_true", dest="utf8",
                        help="write UTF-8 output")
    parser.add_argument("-?", "-h", "-help", action="help",
                        help="show this help message and exit")
    return parser


def _fail(message: str) -> int:
    print(f"vslocator: {message}", file=sys.stderr)
    return 87


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.utf8:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.legacy:
        return _fail("-legacy (Visual Studio 2015 and older) is not supported")

    if args.path and (
        args.include_all
        or args.products
        or args.requires
        or args.version
        or args.latest
        or args.sort
    ):
        return _fail("-path cannot be combined with other selection options")

    if args.path and args.find:
        return _fail("-path cannot be combined with -find")

    if args.find and args.property:
        return _fail('the "property" parameter cannot be specified with "find"')

    if args.version is not None:
        try:
            version_range = VersionRange.parse(args.version)
        except ValueError as error:
            return _fail(str(error))
    else:
        version_range = None

    instances = load_instances(user_locale=user_locale())

    # -path is a lookup, not a filter.
    if args.path:
        instance = instance_for_path(instances, args.path)
        selected = [instance] if instance else []
        fmt = args.format or ("value" if args.property else "text")
        return _emit(selected, args, fmt)

    selected = select_instances(
        instances,
        include_all=args.include_all,
        include_prerelease=args.prerelease,
        products=args.products,
        version_range=version_range,
        requires=args.requires or (),
        requires_any=args.requires_any,
        latest=args.latest,
        sort=args.sort,
    )

    if args.find:
        paths: list[str] = []
        for instance in selected:
            # find_files() sorts within an instance; selected is already
            # ordered the way vswhere orders instances.
            paths.extend(find_files(instance, args.find))
        fmt = args.format or "value"
        if fmt == "json":
            emit_json(paths)
        elif fmt == "value":
            emit_value(paths)
        else:
            return _fail(f"unsupported format for -find: {fmt}")
        return 0

    fmt = args.format or ("value" if args.property else "text")
    return _emit(selected, args, fmt)


def _emit(
    selected: Sequence[Instance],
    args: argparse.Namespace,
    fmt: str,
) -> int:
    include_packages = bool(args.include and "packages" in args.include)

    if args.property:
        payload = []
        values: list[Any] = []
        for instance in selected:
            value = instance.get_property(
                args.property, include_packages=include_packages
            )
            if value is _MISSING:
                continue
            payload.append(_nest_property(args.property, value))
            values.append(value)
        if fmt == "json":
            emit_json(payload)
        elif fmt == "xml":
            emit_xml(payload)
        elif fmt == "text":
            key = "_".join(_property_parts(args.property))
            blocks = [
                f"{key}: {_format_property_value(args.property, value)}"
                for value in values
            ]
            if blocks:
                _write("\n\n".join(blocks) + "\n")
        elif fmt == "value":
            emit_value(
                [_format_property_value(args.property, value) for value in values]
            )
        else:
            return _fail(f"unsupported format: {fmt}")
        return 0

    if fmt == "json":
        emit_json([inst.to_json(include_packages) for inst in selected])
    elif fmt == "text":
        emit_text(selected)
    elif fmt == "xml":
        emit_xml([inst.to_json(include_packages) for inst in selected])
    elif fmt == "value":
        return _fail("-format value requires -property or -find")
    else:
        return _fail(f"unsupported format: {fmt}")
    return 0


def _nest_property(name: str, value: Any) -> dict[str, Any]:
    parts = _property_parts(name)
    if not parts:
        return {}
    result: Any = value
    for part in reversed(parts):
        result = {part: result}
    return result


if __name__ == "__main__":
    raise SystemExit(main())
