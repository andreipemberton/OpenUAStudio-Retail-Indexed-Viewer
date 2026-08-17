"""AssetFamily bridge for the optional legacy indexed renderer.

The regular OpenUA preview deliberately converts ILBM textures to RGBA and may
substitute modern ``HI/ALPHA`` effect images.  That is useful for the modern
preview, but it loses palette-index identity before SHADERMP/TRACYRMP are
applied.  This module keeps the legacy path separate: it resolves the raw ILBM
bytes and structural AMESH flags, while :mod:`indexed_renderer` owns all array
and raster operations.

Source discovery is intentionally bounded.  Only the selected palette's set
root and explicitly supplied family roots (plus their direct ``PALETTE`` and
``REMAP`` children) are inspected.  No recursive directory index is used, so a
different set's remap tables cannot win merely because it appears first in a
broad filesystem scan.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import operator
from pathlib import Path
from typing import Any, Iterable

from ilbm_parser import parse_ilbm_file, parse_pal_file


_PALETTE_NAMES = ("NORMAL.PAL", "STANDARD.PAL")
_SHADER_NAMES = ("SHADERMP.ILB", "SHADERMP.ILBM", "SHADERMP.LBM")
_TRACY_NAMES = ("TRACYRMP.ILB", "TRACYRMP.ILBM", "TRACYRMP.LBM")
_SET_CHILD_NAMES = {
    "objects", "object", "palette", "remap", "skeleton", "rsrcpool",
}

# ``amesh_ADEM_PUBLISH`` discards the scanline, texture-present, and Tracy
# lighten bits before selecting one of ten implemented polygon routines.  Any
# other remaining combination returns without publishing the polygon.  Keep
# that source dispatch table explicit so exact indexed mode never invents a
# rendering interpretation for the nominal flat/line/mapped-TRACY bit space.
_RETAIL_IGNORED_POLFLAG_BITS = 0x001 | 0x008 | 0x100
_RETAIL_NNN_CODE = 0x00
_RETAIL_LINEAR_CODES = frozenset((0x02, 0x32, 0x42, 0x72, 0x82))
_RETAIL_DEPTH_CODES = frozenset((0x06, 0x36, 0x46, 0x76))
_RETAIL_GRADIENT_CODES = frozenset((0x32, 0x36, 0x72, 0x76))
_RETAIL_CLEAR_CODES = frozenset((0x42, 0x46, 0x72, 0x76))
_RETAIL_FLAT_TRACY_CODE = 0x82
_RETAIL_MATERIAL_CODES = frozenset(
    (_RETAIL_NNN_CODE,)
    + tuple(_RETAIL_LINEAR_CODES)
    + tuple(_RETAIL_DEPTH_CODES)
)


class IndexedFamilyAdapterError(RuntimeError):
    """Base class for controlled indexed-family diagnostics."""


class IndexedFamilyUnavailableError(IndexedFamilyAdapterError):
    """The optional backend or one of its required source files is absent."""


class UnsupportedIndexedMaterialError(IndexedFamilyAdapterError):
    """A material uses a legacy mode which this indexed path cannot emulate."""


@dataclass(frozen=True)
class _CachedTexture:
    pixels: bytes
    width: int
    height: int
    chroma_by_index: tuple[bool, ...]


def _absolute(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _path_key(path: Path) -> str:
    return str(_absolute(path)).casefold()


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _absolute(Path(path))
        key = _path_key(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _directory_seed(value: Any) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = _absolute(Path(value))
    if path.is_file() or (not path.exists() and path.suffix):
        path = path.parent
    return path if path.is_dir() else None


def _direct_child_directory(parent: Path, name: str) -> Path | None:
    """Return one case-insensitive direct child directory, never recursively."""

    if not parent.is_dir():
        return None
    wanted = name.casefold()
    try:
        children = sorted(parent.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return None
    return next(
        (child for child in children
         if child.is_dir() and child.name.casefold() == wanted),
        None,
    )


def _direct_named_file(directory: Path, names: Iterable[str]) -> Path | None:
    """Find a named direct child using deterministic extension precedence."""

    if not directory.is_dir():
        return None
    try:
        files = {
            child.name.casefold(): child
            for child in sorted(
                directory.iterdir(), key=lambda item: item.name.casefold()
            )
            if child.is_file()
        }
    except OSError:
        return None
    for name in names:
        candidate = files.get(name.casefold())
        if candidate is not None:
            return _absolute(candidate)
    return None


def _palette_set_root(palette_path: Path) -> Path:
    parent = palette_path.parent
    return parent.parent if parent.name.casefold() == "palette" else parent


def _seed_directories(family: Any) -> list[Path]:
    values: list[Any] = []
    base_path = getattr(family, "base_path", None)
    if base_path is not None:
        values.append(base_path)
    search_root = getattr(family, "search_root", "")
    if search_root:
        values.append(search_root)
    values.extend(getattr(family, "search_roots", ()) or ())
    return _unique_paths(
        seed for value in values
        if (seed := _directory_seed(value)) is not None
    )


def _palette_directories(family: Any) -> list[Path]:
    candidates: list[Path] = []
    for seed in _seed_directories(family):
        candidates.append(seed)
        palette_child = _direct_child_directory(seed, "PALETTE")
        if palette_child is not None:
            candidates.append(palette_child)
        if seed.name.casefold() in _SET_CHILD_NAMES:
            candidates.append(seed.parent)
            sibling = _direct_child_directory(seed.parent, "PALETTE")
            if sibling is not None:
                candidates.append(sibling)
    return _unique_paths(candidates)


def _remap_directories(family: Any, palette_path: Path | None) -> list[Path]:
    candidates: list[Path] = []

    # A palette-selected set is authoritative over generic search roots.
    if palette_path is not None:
        set_root = _palette_set_root(palette_path)
        remap = _direct_child_directory(set_root, "REMAP")
        if remap is not None:
            candidates.append(remap)
        if set_root.name.casefold() == "remap":
            candidates.append(set_root)
        candidates.append(set_root)

    for seed in _seed_directories(family):
        if seed.name.casefold() == "remap":
            candidates.append(seed)
        remap = _direct_child_directory(seed, "REMAP")
        if remap is not None:
            candidates.append(remap)
        candidates.append(seed)
        if seed.name.casefold() in _SET_CHILD_NAMES:
            sibling = _direct_child_directory(seed.parent, "REMAP")
            if sibling is not None:
                candidates.append(sibling)
            candidates.append(seed.parent)
    return _unique_paths(candidates)


def _validated_palette(palette: Any, source: str) -> tuple[tuple[int, int, int], ...]:
    if palette is None or len(palette) != 256:
        raise IndexedFamilyUnavailableError(
            f"{source} did not decode to exactly 256 palette entries"
        )
    decoded: list[tuple[int, int, int]] = []
    for index, color in enumerate(palette):
        if len(color) != 3:
            raise IndexedFamilyUnavailableError(
                f"{source} palette entry {index} is not RGB"
            )
        rgb = tuple(int(channel) for channel in color)
        if any(channel < 0 or channel > 255 for channel in rgb):
            raise IndexedFamilyUnavailableError(
                f"{source} palette entry {index} is outside 0..255"
            )
        decoded.append(rgb)
    return tuple(decoded)


def _discover_palette(
        family: Any) -> tuple[Path | None, tuple[tuple[int, int, int], ...]]:
    configured = getattr(family, "external_palette_path", None)
    if configured is not None:
        path = _absolute(Path(configured))
        if not path.is_file():
            raise IndexedFamilyUnavailableError(
                f"selected external palette is missing: {path}"
            )
        return path, _validated_palette(parse_pal_file(path), str(path))

    for directory in _palette_directories(family):
        path = _direct_named_file(directory, _PALETTE_NAMES)
        if path is not None:
            return path, _validated_palette(parse_pal_file(path), str(path))

    in_memory = getattr(family, "external_palette", None)
    if in_memory is not None:
        return None, _validated_palette(in_memory, "in-memory external palette")

    searched = ", ".join(str(path) for path in _palette_directories(family))
    suffix = f"; bounded directories: {searched}" if searched else ""
    raise IndexedFamilyUnavailableError(
        "no external NORMAL.PAL or STANDARD.PAL was selected or found" + suffix
    )


def _discover_remaps(family: Any, palette_path: Path | None) -> tuple[Path, Path]:
    directories = _remap_directories(family, palette_path)
    partial: list[str] = []
    for directory in directories:
        shader = _direct_named_file(directory, _SHADER_NAMES)
        tracy = _direct_named_file(directory, _TRACY_NAMES)
        if shader is not None and tracy is not None:
            return shader, tracy
        if shader is not None or tracy is not None:
            partial.append(
                f"{directory} ({'SHADERMP only' if shader else 'TRACYRMP only'})"
            )
    searched = ", ".join(str(path) for path in directories) or "<none>"
    detail = f"; incomplete pairs: {', '.join(partial)}" if partial else ""
    raise IndexedFamilyUnavailableError(
        "SHADERMP and TRACYRMP were not found together in the palette set "
        f"or bounded search roots (searched: {searched}){detail}"
    )


def _equivalent_ambiguous_set_profile(
        family: Any) -> tuple[bool, str]:
    """Allow palette ambiguity only when every exact SET profile is equal.

    Palette bytes alone are insufficient: several game SETs share the same
    STANDARD.PAL but carry different SHADERMP tables.  Every unique resolver
    candidate must still exist and carry its own complete sibling REMAP pair;
    otherwise the ambiguity cannot be proven equivalent.  The resolver-selected
    palette must itself be one of those fully audited SETs.
    """

    selected = getattr(family, "external_palette_path", None)
    candidates = _unique_paths(
        Path(value)
        for value in (
            getattr(family, "external_palette_candidates", ()) or ())
    )
    if not candidates:
        return False, "the resolver reported ambiguity without any candidates"

    if selected is None:
        return False, "the resolver-selected palette path is missing"
    selected_key = _path_key(Path(selected))
    candidate_keys = {_path_key(path) for path in candidates}
    if selected_key not in candidate_keys:
        return False, (
            "the resolver-selected palette is not present among the ambiguous "
            "candidates")

    profiles: dict[tuple[str, str, str], list[Path]] = {}
    selected_profile = None
    audit_failures: list[str] = []
    for palette in candidates:
        if not palette.is_file():
            audit_failures.append(f"{palette} (palette is missing)")
            continue
        set_root = _palette_set_root(palette)
        remap = _direct_child_directory(set_root, "REMAP")
        if remap is None:
            audit_failures.append(
                f"{palette} (sibling REMAP directory is missing)")
            continue
        shader = _direct_named_file(remap, _SHADER_NAMES)
        tracy = _direct_named_file(remap, _TRACY_NAMES)
        if shader is None or tracy is None:
            missing = []
            if shader is None:
                missing.append("SHADERMP")
            if tracy is None:
                missing.append("TRACYRMP")
            audit_failures.append(
                f"{palette} ({' and '.join(missing)} is missing from {remap})")
            continue
        profile = (
            _sha256_file(palette),
            _sha256_file(shader),
            _sha256_file(tracy),
        )
        profiles.setdefault(profile, []).append(set_root)
        if _path_key(palette) == selected_key:
            selected_profile = profile

    if audit_failures:
        summary = "; ".join(audit_failures[:4])
        if len(audit_failures) > 4:
            summary += f"; ... +{len(audit_failures) - 4} more"
        return False, (
            f"{len(audit_failures)} of {len(candidates)} unique ambiguous "
            f"palette candidate(s) could not be fully audited: {summary}")

    if selected_profile is None:
        return False, (
            "the resolver-selected palette has no complete sibling REMAP "
            "profile")
    if len(profiles) != 1:
        return False, (
            f"{len(profiles)} distinct complete palette/SHADERMP/TRACYRMP "
            "profiles were found")
    return True, (
        "ambiguous palette candidates collapse to one identical complete "
        f"SET profile across {len(candidates)} unique candidate root(s)")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _palette_bytes(palette: Iterable[Iterable[int]]) -> bytes:
    return bytes(channel for color in palette for channel in color)


def _load_backend():
    try:
        return importlib.import_module("indexed_renderer")
    except Exception as exc:
        raise IndexedFamilyUnavailableError(
            "legacy indexed backend is unavailable; install its optional "
            f"array dependency or inspect indexed_renderer.py ({exc})"
        ) from exc


def _logical_key(name: Any) -> str:
    return str(name or "").replace("\\", "/").replace(":", "/").strip("/").casefold()


def _as_byte(value: Any, label: str) -> int:
    try:
        decoded = operator.index(value)
    except TypeError as exc:
        raise IndexedFamilyAdapterError(f"{label} is not an integer: {value!r}") from exc
    if not 0 <= decoded <= 255:
        raise IndexedFamilyAdapterError(f"{label} is outside 0..255: {decoded}")
    return decoded


def _retail_material_code(polflags: Any) -> int:
    """Return the exact source-dispatch code after its three ignored bits."""

    return int(polflags) & ~_RETAIL_IGNORED_POLFLAG_BITS


def _retail_constant_shade_row(raw_shade: int) -> int:
    """Convert an authored byte to the SHADERMP row used by retail spans.

    The caller handles the source renderer's all-white and all-black polygon
    optimizations at the two ends of the byte range.  For the remaining
    constant-brightness range this is the high byte of
    ``raw_shade * 253 + 0x180``.
    """

    return (253 * raw_shade + 0x180) >> 8


class IndexedFamilyAdapter:
    """Resolve one :class:`AssetFamily` into auditable indexed surfaces."""

    def __init__(
        self,
        family: Any,
        backend: Any,
        tables: Any,
        palette: tuple[tuple[int, int, int], ...],
        palette_path: Path | None,
        shader_path: Path,
        tracy_path: Path,
        shader_image: Any,
        tracy_image: Any,
    ) -> None:
        self.family = family
        self._backend = backend
        self.tables = tables
        self.palette = palette
        self.palette_path = palette_path
        self.shader_path = shader_path
        self.tracy_path = tracy_path
        self.available = True
        self.availability_reason = ""
        self.reason = ""
        self.diagnostics: list[str] = []

        self.source_paths: dict[str, Path | None] = {
            "palette": palette_path,
            "shader": shader_path,
            "tracy": tracy_path,
        }
        self.source_hashes: dict[str, str] = {
            "palette": (
                _sha256_file(palette_path)
                if palette_path is not None
                else hashlib.sha256(_palette_bytes(palette)).hexdigest()
            ),
            "shader": _sha256_file(shader_path),
            "tracy": _sha256_file(tracy_path),
        }
        self.remap_source_paths = {
            "shader": shader_path,
            "tracy": tracy_path,
        }
        self.remap_source_hashes = {
            "shader": self.source_hashes["shader"],
            "tracy": self.source_hashes["tracy"],
        }
        ignored_overrides = sorted(
            str(_absolute(Path(path)))
            for path in (getattr(family, "effect_override_paths", {}) or {}).values()
        )
        set_root = _palette_set_root(
            palette_path) if palette_path is not None else shader_path.parent.parent
        self.source_info: dict[str, Any] = {
            "set": {
                "root": str(_absolute(set_root)),
                "name": set_root.name,
                "palette_resolution_status": str(
                    getattr(family, "external_palette_status", "") or "bounded"
                ),
                "palette_candidates": list(
                    getattr(family, "external_palette_candidates", ()) or ()
                ),
            },
            "palette": {
                "path": str(palette_path) if palette_path is not None else None,
                "sha256": self.source_hashes["palette"],
                "hash_scope": "file" if palette_path is not None else "decoded_rgb",
                "decoded_rgb_sha256": hashlib.sha256(
                    _palette_bytes(palette)
                ).hexdigest(),
                "entries": len(palette),
            },
            "shader": {
                "path": str(shader_path),
                "sha256": self.source_hashes["shader"],
                "decoded_body_sha256": hashlib.sha256(
                    shader_image.pixels
                ).hexdigest(),
                "width": int(shader_image.width),
                "height": int(shader_image.height),
            },
            "tracy": {
                "path": str(tracy_path),
                "sha256": self.source_hashes["tracy"],
                "decoded_body_sha256": hashlib.sha256(
                    tracy_image.pixels
                ).hexdigest(),
                "width": int(tracy_image.width),
                "height": int(tracy_image.height),
            },
            "effect_override_paths_ignored": ignored_overrides,
        }

        self.block_map: dict[tuple[str, int], Any] = {}
        self.atts_by_polygon: dict[tuple[str, int], dict[int, Any]] = {}
        unsupported: list[str] = []
        unsupported_codes: list[dict[str, Any]] = []
        depth_fade_blocks: list[str] = []
        depth_fade_eligible_blocks: list[str] = []
        for family_object in self._all_objects():
            owner = str(getattr(family_object, "owner_path", "root"))
            # ViewFace.block_index is assigned from this exact material-group
            # sequence.  Do not re-enumerate BASE ADES independently: nested
            # visual blocks and future material expansion could otherwise
            # make a viewer face resolve to the wrong structural block.
            groups = list(getattr(family_object, "materials", ()) or ())
            blocks = [getattr(group, "block", None) for group in groups]
            if not groups:
                # Compatibility for deliberately minimal/manual families
                # which have not run asset_family._build_materials().
                base_object = getattr(family_object, "base_object", None)
                blocks = list(
                    getattr(base_object, "ades", ()) if base_object else ()
                )
            for block_index, block in enumerate(blocks):
                if block is None:
                    continue
                key = (owner, block_index)
                self.block_map[key] = block
                indexed_atts: dict[int, Any] = {}
                for entry in getattr(block, "atts", ()) or ():
                    poly_id = int(entry.poly_id)
                    indexed_atts.setdefault(poly_id, entry)
                self.atts_by_polygon[key] = indexed_atts
                if self._tracy_mode(block) == "mapped":
                    unsupported.append(f"{owner} block {block_index}")
                material_code = self._material_code(block)
                if material_code not in _RETAIL_MATERIAL_CODES:
                    unsupported_codes.append({
                        "block": f"{owner} block {block_index}",
                        "raw_polflags": int(getattr(block, "polflags", 0)),
                        "retail_dispatch_code": material_code,
                    })
                if bool(getattr(block, "depth_fade", False)):
                    label = f"{owner} block {block_index}"
                    depth_fade_blocks.append(label)
                    if material_code in _RETAIL_GRADIENT_CODES:
                        depth_fade_eligible_blocks.append(label)
        self.unsupported_mapped_tracy = tuple(unsupported)
        self.source_info["unsupported_mapped_tracy"] = list(unsupported)
        self.source_info["unsupported_material_codes"] = unsupported_codes
        self.source_info["shade_model"] = {
            "constant_shade_rule": (
                "raw 0..2 bypass SHADERMP; raw 3..253 uses row "
                "(253 * raw + 0x180) >> 8; raw 254..255 becomes opaque index 0"
            ),
            "depth_fade_supported": True,
            "depth_fade_emulated": True,
            "depth_fade_default_enabled": False,
            "asset_stores_depth_fade_flag": bool(depth_fade_blocks),
            "asset_stores_distance_profile": False,
            "depth_fade_blocks": depth_fade_blocks,
            "depth_fade_eligible_blocks": depth_fade_eligible_blocks,
            "available_viewer_profile": {
                "name": "retail_gameplay_near_1400_600",
                "visibility_limit": 1400.0,
                "fade_length": 600.0,
                "fade_start": 800.0,
                "profile_origin": "original_normal_gameplay_near_runtime",
                "camera_context": (
                    "current auto-fit asset-viewer camera distance; not a "
                    "recovered mission/world placement"
                ),
            },
            "depth_fade_profile": {
                "name": "retail_gameplay_near_1400_600",
                "visibility_limit": 1400.0,
                "fade_length": 600.0,
                "fade_start": 800.0,
                "profile_origin": "original_normal_gameplay_near_runtime",
                "deprecated_alias_for": "available_viewer_profile",
            },
            "depth_fade_formula": (
                "b=clamp(raw_shade/256 + max(0, "
                "(radial_source_distance-fade_start)/fade_length), 0, 1); "
                "vertex_fixed=rint(b*64768)+0x180"
            ),
            "depth_fade_scope": (
                "Opt-in for AREA_FLAG_DPTHFADE blocks using supported retail "
                "gradient mapped or clear routines; flat LNF and non-gradient "
                "routines never consume the brightness channel."
            ),
        }

        self._textures_by_name: dict[str, Any] = {}
        basename_candidates: dict[str, Any | None] = {}
        for name, image in sorted(
                (getattr(family, "textures", {}) or {}).items(),
                key=lambda item: _logical_key(item[0])):
            key = _logical_key(name)
            if key:
                self._textures_by_name.setdefault(key, image)
                basename = key.rsplit("/", 1)[-1]
                if basename not in basename_candidates:
                    basename_candidates[basename] = image
                elif basename_candidates[basename] is not image:
                    basename_candidates[basename] = None
        for basename, image in basename_candidates.items():
            if image is not None:
                self._textures_by_name.setdefault(basename, image)

        self._animations_by_name = {
            _logical_key(name): animation
            for name, animation in (getattr(family, "animations", {}) or {}).items()
        }
        self.raw_texture_cache: dict[int, _CachedTexture] = {}

    @classmethod
    def try_create(cls, family: Any) -> tuple["IndexedFamilyAdapter | None", str]:
        """Create the bridge, returning a controlled UI-safe failure reason."""

        try:
            if family is None:
                raise IndexedFamilyUnavailableError("no AssetFamily is loaded")
            palette_status = str(
                getattr(family, "external_palette_status", "") or ""
            ).casefold()
            palette_ambiguity_note = ""
            if palette_status == "ambiguous":
                equivalent, palette_ambiguity_note = (
                    _equivalent_ambiguous_set_profile(family))
                if not equivalent:
                    candidates = list(
                        getattr(
                            family, "external_palette_candidates", ()) or ()
                    )
                    summary = ", ".join(candidates[:4])
                    if len(candidates) > 4:
                        summary += f", ... +{len(candidates) - 4} more"
                    raise IndexedFamilyUnavailableError(
                        "external palette/set origin is ambiguous; select a "
                        "set-specific source root or explicit palette before "
                        "using retail indexed rendering "
                        f"({palette_ambiguity_note})"
                        + (f"; candidates: {summary}" if summary else "")
                    )
            backend = _load_backend()
            palette_path, palette = _discover_palette(family)
            shader_path, tracy_path = _discover_remaps(family, palette_path)
            shader_image = parse_ilbm_file(shader_path)
            tracy_image = parse_ilbm_file(tracy_path)
            cls._validate_remap_image(shader_image, "SHADERMP", shader_path)
            cls._validate_remap_image(tracy_image, "TRACYRMP", tracy_path)
            tables = backend.IndexedTables.from_decoded(
                palette, shader_image.pixels, tracy_image.pixels
            )
            adapter = cls(
                family, backend, tables, palette, palette_path,
                shader_path, tracy_path, shader_image, tracy_image,
            )
            if palette_ambiguity_note:
                adapter.source_info["set"][
                    "palette_ambiguity_resolution"
                ] = palette_ambiguity_note
            return adapter, ""
        except IndexedFamilyAdapterError as exc:
            return None, str(exc)
        except Exception as exc:
            return None, f"legacy indexed family initialization failed: {exc}"

    @staticmethod
    def _validate_remap_image(image: Any, label: str, path: Path) -> None:
        if (
            image.pixels is None
            or int(image.width) != 256
            or int(image.height) != 256
            or len(image.pixels) != 256 * 256
        ):
            raise IndexedFamilyUnavailableError(
                f"{label} must decode to a 256 x 256 indexed BODY: {path}"
            )

    def _all_objects(self) -> list[Any]:
        getter = getattr(self.family, "all_objects", None)
        if callable(getter):
            return list(getter())
        root = getattr(self.family, "root_object", None)
        if root is None:
            return []
        iterator = getattr(root, "iter_tree", None)
        return list(iterator()) if callable(iterator) else [root]

    @staticmethod
    def _material_code(block: Any) -> int:
        return _retail_material_code(getattr(block, "polflags", 0))

    @staticmethod
    def _tracy_mode(block: Any) -> str:
        bits = int(getattr(block, "polflags", 0)) & 0xC0
        return {0x00: "none", 0x40: "clear", 0x80: "flat", 0xC0: "mapped"}[bits]

    @staticmethod
    def _frame_values(material: Any, frame_index: int) -> tuple[int, int, int]:
        try:
            index = operator.index(frame_index)
        except TypeError as exc:
            raise IndexedFamilyAdapterError(
                f"animation frame index is not an integer: {frame_index!r}"
            ) from exc
        frames = list(getattr(material, "anim_frames", ()) or ())
        if not 0 <= index < len(frames):
            raise IndexedFamilyAdapterError(
                f"animation frame index {index} is outside 0..{len(frames) - 1}"
            )
        frame = frames[index]
        if hasattr(frame, "frame_id"):
            return (
                int(getattr(frame, "frame_time", 0)),
                int(frame.frame_id),
                int(frame.texcoords_id),
            )
        if len(frame) < 3:
            raise IndexedFamilyAdapterError(
                f"animation frame {index} does not contain duration/image/UV IDs"
            )
        return int(frame[0]), int(frame[1]), int(frame[2])

    def active_texture_name(self, material: Any, frame_index: int) -> str:
        """Return the raw VANM bitmap selected by this material's frame."""

        frames = getattr(material, "anim_frames", ()) or ()
        if not frames:
            return str(getattr(material, "label", "") or "")
        _duration, image_id, _uv_id = self._frame_values(material, frame_index)
        names = list(getattr(material, "anim_bitmap_names", ()) or ())
        if not 0 <= image_id < len(names):
            raise IndexedFamilyAdapterError(
                f"animation image ID {image_id} is outside 0..{len(names) - 1}"
            )
        return str(names[image_id])

    def active_uvs(
            self, material: Any, frame_index: int) -> tuple[tuple[int, int], ...]:
        """Return the supplied structural material's active VANM UV group."""

        _duration, _image_id, uv_id = self._frame_values(material, frame_index)
        groups = list(getattr(material, "anim_uv_groups", ()) or ())
        if not 0 <= uv_id < len(groups):
            raise IndexedFamilyAdapterError(
                f"animation UV ID {uv_id} is outside 0..{len(groups) - 1}"
            )
        return tuple((int(u), int(v)) for u, v in groups[uv_id])

    def block_for(self, owner: str, block_index: int) -> Any | None:
        return self.block_map.get((str(owner), int(block_index)))

    def _fallback_animation_name(
            self, block: Any, frame_index: int) -> str:
        texture = getattr(block, "texture", None)
        animation_name = str(getattr(texture, "name", "") or "")
        animation = self._animations_by_name.get(_logical_key(animation_name))
        if animation is None:
            return ""
        frames = list(getattr(animation, "frames", ()) or ())
        try:
            index = operator.index(frame_index)
        except TypeError as exc:
            raise IndexedFamilyAdapterError(
                f"animation frame index is not an integer: {frame_index!r}"
            ) from exc
        if not 0 <= index < len(frames):
            raise IndexedFamilyAdapterError(
                f"animation frame index {index} is outside 0..{len(frames) - 1}"
            )
        image_id = int(frames[index].frame_id)
        names = list(getattr(animation, "bitmap_names", ()) or ())
        if not 0 <= image_id < len(names):
            raise IndexedFamilyAdapterError(
                f"animation image ID {image_id} is outside 0..{len(names) - 1}"
            )
        return str(names[image_id])

    def _texture_name(
            self, block: Any, material: Any, frame_index: int) -> str:
        frames = getattr(material, "anim_frames", ()) or ()
        if frames:
            return self.active_texture_name(material, frame_index)
        texture = getattr(block, "texture", None)
        kind = str(getattr(texture, "kind", "") or "").casefold()
        if kind == "bmpanim":
            fallback = self._fallback_animation_name(block, frame_index)
            if fallback:
                return fallback
        block_name = str(getattr(texture, "name", "") or "")
        return block_name or str(getattr(material, "label", "") or "")

    def _find_texture(self, name: str) -> Any | None:
        key = _logical_key(name)
        return (
            self._textures_by_name.get(key)
            or self._textures_by_name.get(key.rsplit("/", 1)[-1])
        )

    def _cached_texture(self, image: Any, name: str) -> _CachedTexture:
        cache_key = id(image)
        cached = self.raw_texture_cache.get(cache_key)
        if cached is not None:
            return cached

        width = int(getattr(image, "width", 0))
        height = int(getattr(image, "height", 0))
        pixels = getattr(image, "pixels", None)
        if pixels is None or width <= 0 or height <= 0:
            raise IndexedFamilyAdapterError(
                f"raw indexed texture has no decoded BODY: {name!r}"
            )
        try:
            raw = pixels if isinstance(pixels, bytes) else bytes(pixels)
        except (TypeError, ValueError) as exc:
            raise IndexedFamilyAdapterError(
                f"raw indexed texture bytes are invalid: {name!r}"
            ) from exc
        if len(raw) != width * height:
            raise IndexedFamilyAdapterError(
                f"raw indexed texture {name!r} has {len(raw)} pixels; "
                f"expected {width * height}"
            )

        # An embedded CMAP is texture-authoritative for source chroma.  Only
        # textures without a CMAP inherit the selected external set palette.
        source_palette = getattr(image, "palette", None) or self.palette
        chroma = tuple(
            index < len(source_palette)
            and tuple(int(value) for value in source_palette[index])
            == (255, 255, 0)
            for index in range(256)
        )
        cached = _CachedTexture(raw, width, height, chroma)
        self.raw_texture_cache[cache_key] = cached
        return cached

    def _diagnose_unsupported(self, message: str) -> None:
        if message not in self.diagnostics:
            self.diagnostics.append(message)

    def resolve_surface(
            self, face: Any, material: Any, frame_index: int) -> Any:
        """Resolve a viewer face to a raw indexed texture or solid index."""

        owner = str(getattr(face, "owner", "root"))
        block_index = int(getattr(face, "block_index", -1))
        block = self.block_map.get((owner, block_index))
        if block is None:
            raise IndexedFamilyAdapterError(
                f"no structural material block for {owner} block {block_index}"
            )
        poly_id = int(getattr(face, "poly_id", -1))
        entry = self.atts_by_polygon.get((owner, block_index), {}).get(poly_id)

        polflags = int(getattr(block, "polflags", 0))
        material_code = self._material_code(block)
        decoded_tracy_mode = self._tracy_mode(block)
        if decoded_tracy_mode == "mapped":
            message = (
                f"mapped TRACY is not supported in legacy indexed mode "
                f"({owner} block {block_index}, polygon {poly_id})"
            )
            self._diagnose_unsupported(message)
            raise UnsupportedIndexedMaterialError(message)
        if material_code not in _RETAIL_MATERIAL_CODES:
            message = (
                f"unsupported retail polygon flags 0x{polflags:x} "
                f"(dispatch code 0x{material_code:x}); the source AMESH "
                f"dispatcher does not publish this combination "
                f"({owner} block {block_index}, polygon {poly_id})"
            )
            self._diagnose_unsupported(message)
            raise UnsupportedIndexedMaterialError(message)

        # These modes come from the retail source's dispatch table after it
        # masks scanline, texture-present, and Tracy-lighten.  In particular,
        # NNN is the sole nonmapped routine and LNF is the sole flat-TRACY one.
        textured = material_code != _RETAIL_NNN_CODE
        map_mode = (
            "depth" if material_code in _RETAIL_DEPTH_CODES else "linear")
        tracy_mode = (
            "flat" if material_code == _RETAIL_FLAT_TRACY_CODE
            else "clear" if material_code in _RETAIL_CLEAR_CODES
            else "none"
        )

        if not textured:
            # NNN dispatches ZeroSpan: ATTS color/shade fields and any attached
            # bitmap are ignored, and numeric palette index zero is written as
            # an ordinary opaque polygon.
            return self._backend.IndexedSurface(
                name="palette-index-0",
                kind="solid",
                indices=None,
                width=0,
                height=0,
                chroma_by_index=None,
                solid_index=0,
                shade_mode="none",
                shade_value=0,
                tracy_mode="none",
                map_mode="linear",
            )

        shade_source = getattr(face, "shade", None)
        if shade_source is None:
            shade_source = (
                getattr(entry, "shade_val", None)
                if entry is not None else getattr(block, "shade_val", 0)
            )
        raw_shade = _as_byte(shade_source, "shade value")
        authored_depth_fade = bool(getattr(block, "depth_fade", False))
        distance_fade_eligible = (
            authored_depth_fade
            and material_code in _RETAIL_GRADIENT_CODES
        )
        shade_mode = (
            "gradient" if material_code in _RETAIL_GRADIENT_CODES else "none")
        shade_value = 0
        if shade_mode == "gradient" and raw_shade <= 2:
            # b < 0.01 at every vertex: retail clears its shade flag and uses
            # the ordinary no-SHADERMP span.
            shade_mode = "none"
        elif shade_mode == "gradient" and raw_shade >= 254:
            # b > 0.99 at every vertex: retail replaces all raster flags with
            # zero, selecting the opaque ZeroSpan regardless of clear TRACY.
            return self._backend.IndexedSurface(
                name="palette-index-0-black-shade",
                kind="solid",
                indices=None,
                width=0,
                height=0,
                chroma_by_index=None,
                solid_index=0,
                shade_mode="none",
                shade_value=0,
                tracy_mode="none",
                map_mode="linear",
                authored_shade_value=raw_shade,
                authored_depth_fade_flag=authored_depth_fade,
                distance_fade_eligible=False,
            )
        elif shade_mode == "gradient":
            shade_value = _retail_constant_shade_row(raw_shade)

        name = self._texture_name(block, material, frame_index)
        image = self._find_texture(name)
        if not name or image is None:
            raise IndexedFamilyAdapterError(
                f"raw indexed texture unavailable for {name or '<unnamed>'!r} "
                f"({owner} block {block_index}, polygon {poly_id})"
            )
        cached = self._cached_texture(image, name)
        return self._backend.IndexedSurface(
            name=name,
            kind="texture",
            indices=cached.pixels,
            width=cached.width,
            height=cached.height,
            chroma_by_index=cached.chroma_by_index,
            solid_index=None,
            shade_mode=shade_mode,
            shade_value=shade_value,
            tracy_mode=tracy_mode,
            map_mode=map_mode,
            authored_shade_value=raw_shade,
            authored_depth_fade_flag=authored_depth_fade,
            distance_fade_eligible=distance_fade_eligible,
        )


__all__ = [
    "IndexedFamilyAdapter",
    "IndexedFamilyAdapterError",
    "IndexedFamilyUnavailableError",
    "UnsupportedIndexedMaterialError",
]
