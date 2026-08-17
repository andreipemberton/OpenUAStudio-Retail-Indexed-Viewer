"""Pure indexed-colour raster backend for the OpenUA model viewer.

The retail renderer performs its colour work on palette indices.  Converting
textures to RGB before shading or TRACY composition loses information because
several palette entries may display as the same colour.  This module therefore
keeps a byte-sized framebuffer until the final :meth:`IndexedRenderResult.to_rgba`
call.

The module deliberately has no Qt dependency.  NumPy is used when available
for the interactive/high-resolution path, but all public operations retain a
small pure-Python fallback so importing the viewer does not make NumPy a hard
runtime requirement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
import operator
from typing import Hashable, Iterable, Sequence

try:  # NumPy is an acceleration, not part of the public contract.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised on minimal installations.
    _np = None


ACCELERATION_BACKEND = "numpy" if _np is not None else "python"
# The indexed path carries multiple full-frame buffers.  Keep direct exports
# within a bounded, tested allocation envelope instead of discovering an
# unsafe request after the raster pass has begun.  A 4096 x 4096 frame is the
# intended high-resolution ceiling for the accelerated backend; the portable
# Python path is deliberately limited to roughly one megapixel.
MAX_SAFE_PIXELS = 4096 * 4096 if _np is not None else 1024 * 1024


_TABLE_SIDE = 256
_TABLE_PIXELS = _TABLE_SIDE * _TABLE_SIDE
_CAMERA_DISTANCE = 4.0
_MINIMUM_DEPTH = 0.2
_INSIDE_EPSILON = 1e-10

RGB = tuple[int, int, int]
Point2 = tuple[float, float]
Point3 = tuple[float, float, float]


@dataclass(frozen=True)
class IndexedDistanceFadeProfile:
    """Validated opt-in AREA distance-fade parameters.

    Camera vertices used by :class:`IndexedPiece` are in the viewer's
    normalized camera space, whose eye is ``(0, 0, 4)``.  The caller must
    therefore provide ``source_distance_scale=1/camera_scale`` so radial
    distances are converted back to source-model units before the retail
    visibility profile is applied.

    The defaults are the near-gameplay values recovered from the retail
    runtime.  Merely constructing the default profile does not enable the
    effect; callers must opt in explicitly.
    """

    enabled: bool = False
    visibility_limit: float = 1400.0
    fade_length: float = 600.0
    source_distance_scale: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("distance-fade enabled must be a boolean")
        values: dict[str, float] = {}
        for field_name in (
                "visibility_limit", "fade_length", "source_distance_scale"):
            raw = getattr(self, field_name)
            if isinstance(raw, bool):
                raise TypeError(f"distance-fade {field_name} must be a number")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"distance-fade {field_name} must be a number") from exc
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"distance-fade {field_name} must be finite and positive")
            values[field_name] = value
        if values["fade_length"] > values["visibility_limit"]:
            raise ValueError(
                "distance-fade fade_length cannot exceed visibility_limit")
        for field_name, value in values.items():
            object.__setattr__(self, field_name, value)

    @property
    def fade_start(self) -> float:
        return self.visibility_limit - self.fade_length

    @property
    def profile_name(self) -> str:
        if (self.visibility_limit == 1400.0
                and self.fade_length == 600.0):
            return "retail_gameplay_near_1400_600"
        return "custom"

    @property
    def channel_signature(self) -> tuple[float, float, float]:
        """Bind precomputed vertex brightness to its exact source profile."""

        return (
            self.visibility_limit,
            self.fade_length,
            self.source_distance_scale,
        )

    def to_metadata(self) -> dict[str, object]:
        return {
            "name": self.profile_name,
            "enabled": self.enabled,
            "visibility_limit": self.visibility_limit,
            "fade_length": self.fade_length,
            "fade_start": self.fade_start,
            "source_distance_scale": self.source_distance_scale,
        }


def _u8(value, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must contain integers, not booleans")
    try:
        converted = operator.index(value)
    except TypeError as exc:
        raise TypeError(f"{label} must contain integers") from exc
    if not 0 <= converted <= 255:
        raise ValueError(f"{label} values must be in the range 0..255")
    return converted


def _decoded_pixels(values, label: str, width: int, height: int):
    """Unwrap an already-decoded ILBM-like object and verify its dimensions."""

    if hasattr(values, "pixels"):
        decoded_width = getattr(values, "width", width)
        decoded_height = getattr(values, "height", height)
        if (decoded_width, decoded_height) != (width, height):
            raise ValueError(
                f"{label} must be {width}x{height}, got "
                f"{decoded_width}x{decoded_height}"
            )
        values = values.pixels
    return values


def _normalise_u8_grid(
        values, width: int, height: int, label: str) -> bytes:
    """Return a checked row-major byte grid without requiring NumPy."""

    values = _decoded_pixels(values, label, width, height)
    expected = width * height

    if isinstance(values, bytes):
        if len(values) != expected:
            raise ValueError(
                f"{label} needs {expected} pixels, got {len(values)}")
        return values
    if isinstance(values, (bytearray, memoryview)):
        raw = bytes(values)
        if len(raw) != expected:
            raise ValueError(
                f"{label} needs {expected} pixels, got {len(raw)}")
        return raw

    if _np is not None and isinstance(values, _np.ndarray):
        array = values
        if array.ndim == 2:
            if tuple(array.shape) != (height, width):
                raise ValueError(
                    f"{label} must have shape ({height}, {width}), got "
                    f"{tuple(array.shape)}")
        elif array.ndim == 1:
            if array.size != expected:
                raise ValueError(
                    f"{label} needs {expected} pixels, got {array.size}")
        else:
            raise ValueError(f"{label} must be a flat or two-dimensional grid")
        if array.dtype.kind not in "uib":
            raise TypeError(f"{label} must contain integers")
        if array.size and (
                int(array.min()) < 0 or int(array.max()) > 255):
            raise ValueError(f"{label} values must be in the range 0..255")
        return _np.asarray(array, dtype=_np.uint8).reshape(-1).tobytes()

    try:
        outer = list(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable pixel grid") from exc

    # Preserve the table's declared row/column orientation.  A nested grid is
    # accepted only at its exact dimensions; flat data remains row-major.
    nested = len(outer) == height and any(
        not isinstance(item, int) for item in outer)
    if nested:
        flattened: list[int] = []
        for row_number, row in enumerate(outer):
            try:
                row_values = list(row)
            except TypeError as exc:
                raise TypeError(
                    f"{label} row {row_number} must be iterable") from exc
            if len(row_values) != width:
                raise ValueError(
                    f"{label} row {row_number} needs {width} pixels, got "
                    f"{len(row_values)}")
            flattened.extend(
                _u8(value, label) for value in row_values)
    else:
        if len(outer) != expected:
            raise ValueError(
                f"{label} needs {expected} pixels, got {len(outer)}")
        flattened = [_u8(value, label) for value in outer]
    return bytes(flattened)


def _normalise_palette(palette) -> tuple[RGB, ...]:
    try:
        entries = list(palette)
    except TypeError as exc:
        raise TypeError("palette must be an iterable of RGB triples") from exc
    if len(entries) != 256:
        raise ValueError(f"palette needs 256 entries, got {len(entries)}")

    result: list[RGB] = []
    for index, entry in enumerate(entries):
        try:
            components = list(entry)
        except TypeError as exc:
            raise TypeError(f"palette entry {index} must be an RGB triple") from exc
        if len(components) != 3:
            raise ValueError(f"palette entry {index} must have three components")
        result.append(tuple(
            _u8(component, f"palette entry {index}")
            for component in components
        ))
    return tuple(result)


def _normalise_chroma(values) -> tuple[bool, ...]:
    if _np is not None and isinstance(values, _np.ndarray):
        items = values.reshape(-1).tolist()
    else:
        try:
            items = list(values)
        except TypeError as exc:
            raise TypeError(
                "chroma_by_index must be an iterable of 256 flags") from exc
    if len(items) != 256:
        raise ValueError(
            f"chroma_by_index needs 256 flags, got {len(items)}")
    result: list[bool] = []
    for value in items:
        if isinstance(value, bool):
            result.append(value)
            continue
        try:
            converted = operator.index(value)
        except TypeError as exc:
            raise TypeError("chroma_by_index must contain boolean flags") from exc
        if converted not in (0, 1):
            raise ValueError("chroma_by_index flags must be boolean or 0/1")
        result.append(bool(converted))
    return tuple(result)


def _enum(value, label: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be one of {', '.join(choices)}")
    normalised = value.casefold()
    if normalised not in choices:
        raise ValueError(
            f"unsupported {label} {value!r}; expected one of "
            f"{', '.join(choices)}")
    return normalised


def _points(values, dimensions: int, label: str):
    try:
        source = list(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of points") from exc
    result = []
    for index, point in enumerate(source):
        try:
            coordinates = tuple(float(value) for value in point)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{label}[{index}] must contain numbers") from exc
        if len(coordinates) != dimensions:
            raise ValueError(
                f"{label}[{index}] must have {dimensions} coordinates")
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError(f"{label}[{index}] coordinates must be finite")
        result.append(coordinates)
    return tuple(result)


@dataclass(frozen=True)
class IndexedTables:
    """Display palette plus the two 256x256 retail remap tables.

    Both tables are stored as row-major bytes.  Their axes are intentionally
    exposed through named lookup methods:

    ``shader[shade_value][source_index]`` and
    ``tracy[background_index][source_index]``.

    The latter axis order is not an inference from table shape.  The original
    x86 and 68k ``span_lnf`` implementations form the byte offset as
    ``(background << 8) | raw_texture_texel``.
    """

    palette: tuple[RGB, ...]
    shader_pixels: bytes = field(repr=False)
    tracy_pixels: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "palette", _normalise_palette(self.palette))
        object.__setattr__(self, "shader_pixels", _normalise_u8_grid(
            self.shader_pixels, 256, 256, "SHADERMP"))
        object.__setattr__(self, "tracy_pixels", _normalise_u8_grid(
            self.tracy_pixels, 256, 256, "TRACYRMP"))

    @classmethod
    def from_decoded(
            cls, palette, shader_pixels, tracy_pixels) -> "IndexedTables":
        """Build tables and fail closed unless they match the UA profile.

        Direct construction performs format validation only, which is useful
        for synthetic tables and unit tests.  Production decoded resources go
        through this constructor so a wrong remap set or transposed TRACY table
        cannot produce a plausible-looking but structurally incorrect render.
        """

        return cls(palette, shader_pixels, tracy_pixels).validate_ua_profile()

    def validate_ua_profile(self) -> "IndexedTables":
        """Verify fixed orientation/content tripwires in the UA remap tables.

        These checks are intentionally independent of NumPy and avoid the
        data-set-specific full-table asymmetry count used by the forensic
        renderer.  They still pin both axes, special source/background
        behaviour, known asymmetric cells, and non-commutative layer order.
        """

        shader = self.shader_pixels
        tracy = self.tracy_pixels

        # All inspected retail SET profiles map every source to black at the
        # terminal shade and keep source index zero black at every shade.  The
        # two independent axes reject a transposed or unrelated SHADERMP while
        # allowing the authored SET-specific shade curves to differ.
        if any(shader[255 * 256 + source] != 0 for source in range(256)):
            raise ValueError(
                "SHADERMP UA profile failed: terminal shade row 255 must "
                "map every source to index zero")
        if any(shader[shade * 256] != 0 for shade in range(256)):
            raise ValueError(
                "SHADERMP UA profile failed: source index column 0 must "
                "remain index zero")
        for source, expected in ((1, 1), (5, 5), (6, 0), (255, 255)):
            actual = shader[source]
            if actual != expected:
                raise ValueError(
                    "SHADERMP UA profile failed at unshaded row 0, source "
                    f"{source}: {actual} != {expected}")

        if any(tracy[13 * 256 + source] != source
               for source in range(256)):
            raise ValueError(
                "TRACYRMP UA profile failed: background row 13 must copy "
                "the source index")

        for source in (8, 10, 11, 12, 14, 15):
            if any(tracy[background * 256 + source] != source
                   for background in range(256)):
                raise ValueError(
                    "TRACYRMP UA profile failed: opaque source column "
                    f"{source} changed")

        for background in range(256):
            expected = 0 if background == 13 else background
            actual = tracy[background * 256]
            if actual != expected:
                raise ValueError(
                    "TRACYRMP UA profile failed: transparent source column 0 "
                    f"over background {background} is {actual}, expected "
                    f"{expected}")

        known_cells = {
            (31, 220): 212,
            (220, 31): 200,
            (13, 255): 255,
            (255, 13): 177,
            (156, 185): 54,
            (159, 185): 106,
        }
        for (background, source), expected in known_cells.items():
            actual = tracy[background * 256 + source]
            if actual != expected:
                raise ValueError(
                    "TRACYRMP UA profile failed at "
                    f"[{background}][{source}]: {actual} != {expected}")

        first_156 = tracy[156]
        first_159 = tracy[159]
        layer_156_then_159 = tracy[first_156 * 256 + 159]
        layer_159_then_156 = tracy[first_159 * 256 + 156]
        if (layer_156_then_159, layer_159_then_156) != (207, 191):
            raise ValueError(
                "TRACYRMP UA profile failed: non-commutative 156/159 "
                "layer-order tripwire did not match (207, 191)")
        return self

    @property
    def shader(self) -> bytes:
        """Alias for callers that use the shorter table name."""

        return self.shader_pixels

    @property
    def tracy(self) -> bytes:
        """Alias for callers that use the shorter table name."""

        return self.tracy_pixels

    @property
    def display_palette(self) -> tuple[RGB, ...]:
        """Palette used by the framebuffer; renderer index zero is black."""

        return ((0, 0, 0),) + self.palette[1:]

    def shade_index(self, shade_value: int, source_index: int) -> int:
        shade = _u8(shade_value, "shade value")
        source = _u8(source_index, "source index")
        return self.shader_pixels[shade * 256 + source]

    def tracy_index(self, background_index: int, source_index: int) -> int:
        background = _u8(background_index, "TRACY background index")
        source = _u8(source_index, "TRACY source index")
        return self.tracy_pixels[background * 256 + source]


@dataclass(frozen=True)
class IndexedSurface:
    """One immutable indexed source and its structural render modes."""

    name: str
    kind: str
    indices: bytes | Sequence[int] | None = field(repr=False)
    width: int
    height: int
    chroma_by_index: tuple[bool, ...] | Sequence[bool] | None = field(
        repr=False)
    solid_index: int | None
    shade_mode: str
    shade_value: int
    tracy_mode: str
    map_mode: str
    authored_shade_value: int | None = None
    authored_depth_fade_flag: bool = False
    distance_fade_eligible: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("surface name must be a string")
        kind = _enum(self.kind, "surface kind", ("texture", "solid"))
        shade_mode = _enum(
            self.shade_mode, "shade mode", ("none", "flat", "line", "gradient"))
        tracy_mode = _enum(
            self.tracy_mode, "TRACY mode", ("none", "clear", "flat"))
        map_mode = _enum(self.map_mode, "map mode", ("linear", "depth"))
        shade_value = _u8(self.shade_value, "shade value")
        authored_shade_value = (
            None
            if self.authored_shade_value is None
            else _u8(self.authored_shade_value, "authored shade value")
        )
        if not isinstance(self.authored_depth_fade_flag, bool):
            raise TypeError("authored_depth_fade_flag must be a boolean")
        if not isinstance(self.distance_fade_eligible, bool):
            raise TypeError("distance_fade_eligible must be a boolean")

        try:
            width = operator.index(self.width)
            height = operator.index(self.height)
        except TypeError as exc:
            raise TypeError("surface dimensions must be integers") from exc

        if kind == "texture":
            if width <= 0 or height <= 0:
                raise ValueError("texture dimensions must be positive")
            if self.indices is None:
                raise ValueError("texture surface needs indexed pixels")
            indices = _normalise_u8_grid(
                self.indices, width, height, f"surface {self.name!r}")
            if self.chroma_by_index is None:
                raise ValueError("texture surface needs chroma_by_index")
            chroma = _normalise_chroma(self.chroma_by_index)
            if self.solid_index is not None:
                raise ValueError("texture surface cannot also have a solid index")
            solid_index = None
        else:
            if width < 0 or height < 0:
                raise ValueError("solid surface dimensions cannot be negative")
            if self.solid_index is None:
                raise ValueError("solid surface needs solid_index")
            solid_index = _u8(self.solid_index, "solid index")
            indices = None
            chroma = None

        if self.distance_fade_eligible:
            if not self.authored_depth_fade_flag:
                raise ValueError(
                    "distance-fade eligibility requires the authored AREA flag")
            if authored_shade_value is None:
                raise ValueError(
                    "distance-fade eligibility requires an authored shade value")
            if kind != "texture":
                raise ValueError(
                    "distance-fade eligibility requires a mapped texture")
            if tracy_mode == "flat":
                raise ValueError("flat TRACY/LNF cannot use distance fade")
            if shade_mode not in ("none", "gradient"):
                raise ValueError(
                    "distance-fade eligibility requires a retail gradient "
                    "shade routine")

        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "indices", indices)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "chroma_by_index", chroma)
        object.__setattr__(self, "solid_index", solid_index)
        object.__setattr__(self, "shade_mode", shade_mode)
        object.__setattr__(self, "shade_value", shade_value)
        object.__setattr__(self, "tracy_mode", tracy_mode)
        object.__setattr__(self, "map_mode", map_mode)
        object.__setattr__(self, "authored_shade_value", authored_shade_value)


@dataclass(frozen=True)
class IndexedPiece:
    """One already-ordered convex BSP fragment ready for rasterisation."""

    source_face_id: Hashable
    polygon_id: int
    screen: tuple[Point2, ...]
    uvs: tuple[Point2, ...]
    camera_vertices: tuple[Point3, ...]
    surface: IndexedSurface
    source_order: int = 0
    sort_depth: float = 0.0
    # Optional source-derived AREA brightness, evaluated after the viewer's
    # 3-D near clip and carried through any viewer-only BSP split. Supplying
    # both channels prevents radial distance from being recomputed at a BSP
    # intersection that did not exist in the retail pipeline.
    vertex_brightness: tuple[float, ...] = ()
    vertex_shade_fixed: tuple[float, ...] = ()
    vertex_distance_fade_signature: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        try:
            hash(self.source_face_id)
        except TypeError as exc:
            raise TypeError("source_face_id must be hashable") from exc
        try:
            polygon_id = operator.index(self.polygon_id)
        except TypeError as exc:
            raise TypeError("polygon_id must be an integer") from exc
        try:
            source_order = operator.index(self.source_order)
        except TypeError as exc:
            raise TypeError("source_order must be an integer") from exc
        try:
            sort_depth = float(self.sort_depth)
        except (TypeError, ValueError) as exc:
            raise TypeError("sort_depth must be a finite number") from exc
        if not math.isfinite(sort_depth):
            raise ValueError("sort_depth must be a finite number")
        if not isinstance(self.surface, IndexedSurface):
            raise TypeError("surface must be an IndexedSurface")

        screen = _points(self.screen, 2, "screen")
        if len(screen) < 3:
            raise ValueError("an indexed piece needs at least three screen points")
        camera = _points(self.camera_vertices, 3, "camera_vertices")
        if len(camera) != len(screen):
            raise ValueError("camera_vertices must match the screen polygon")
        uvs = _points(self.uvs, 2, "uvs")
        if self.surface.kind == "texture" and len(uvs) != len(screen):
            raise ValueError("texture UVs must match the screen polygon")
        if self.surface.kind == "solid" and len(uvs) not in (0, len(screen)):
            raise ValueError("solid UVs must be empty or match the screen polygon")
        try:
            vertex_brightness = tuple(
                float(value) for value in self.vertex_brightness)
            vertex_shade_fixed = tuple(
                float(value) for value in self.vertex_shade_fixed)
            vertex_distance_fade_signature = tuple(
                float(value)
                for value in self.vertex_distance_fade_signature)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "distance-fade vertex channels must contain numbers") from exc
        if bool(vertex_brightness) != bool(vertex_shade_fixed):
            raise ValueError(
                "distance-fade brightness and fixed channels must be supplied "
                "together")
        if bool(vertex_brightness) != bool(vertex_distance_fade_signature):
            raise ValueError(
                "precomputed distance-fade channels require their profile "
                "signature")
        if vertex_distance_fade_signature \
                and len(vertex_distance_fade_signature) != 3:
            raise ValueError(
                "distance-fade profile signature must contain visibility "
                "limit, fade length, and source-distance scale")
        if vertex_brightness and len(vertex_brightness) != len(screen):
            raise ValueError(
                "distance-fade vertex channels must match the screen polygon")
        if vertex_shade_fixed and len(vertex_shade_fixed) != len(screen):
            raise ValueError(
                "distance-fade vertex channels must match the screen polygon")
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0
               for value in vertex_brightness):
            raise ValueError(
                "distance-fade vertex brightness must be finite in [0, 1]")
        if any(not math.isfinite(value) or not 0x180 <= value <= 0xFE80
               for value in vertex_shade_fixed):
            raise ValueError(
                "distance-fade fixed vertex values must be finite and in "
                "the source range 0x180..0xFE80")
        if any(not math.isfinite(value) or value <= 0.0
               for value in vertex_distance_fade_signature):
            raise ValueError(
                "distance-fade profile signature values must be finite and "
                "positive")

        object.__setattr__(self, "polygon_id", polygon_id)
        object.__setattr__(self, "source_order", source_order)
        object.__setattr__(self, "sort_depth", sort_depth)
        object.__setattr__(self, "screen", screen)
        object.__setattr__(self, "uvs", uvs)
        object.__setattr__(self, "camera_vertices", camera)
        object.__setattr__(self, "vertex_brightness", vertex_brightness)
        object.__setattr__(self, "vertex_shade_fixed", vertex_shade_fixed)
        object.__setattr__(
            self, "vertex_distance_fade_signature",
            vertex_distance_fade_signature)


@dataclass(frozen=True)
class _PieceDistanceFade:
    """Prepared per-piece brightness state for one validated profile."""

    mode: str
    vertex_shade_fixed: tuple[float, ...] = ()


INDEXED_DISTANCE_FADE_FORMULA = (
    "b=clamp(authored_shade/256 + max(0, "
    "(radial_source_distance-fade_start)/fade_length), 0, 1); "
    "vertex_fixed=rint(b*64768)+0x180; SHADERMP row is the "
    "screen-linear interpolated fixed value's high byte"
)


def _prepare_distance_fade(
        pieces: Sequence[IndexedPiece],
        profile: IndexedDistanceFadeProfile) -> tuple[_PieceDistanceFade, ...]:
    # BSP may split one authored polygon into several pieces. Retail performs
    # its all-white/all-black shortcut once for the complete published source
    # polygon, so all fragments carrying the same source_face_id must share the
    # shortcut decision. Classifying fragments independently can otherwise
    # turn only the far side of a clear polygon into an opaque black seam.
    brightness_by_piece: list[tuple[float, ...] | None] = []
    fixed_by_piece: list[tuple[float, ...] | None] = []
    brightness_by_face: dict[Hashable, list[float]] = {}
    for piece in pieces:
        surface = piece.surface
        if not profile.enabled or not surface.distance_fade_eligible:
            brightness_by_piece.append(None)
            fixed_by_piece.append(None)
            continue
        if piece.vertex_brightness:
            if piece.vertex_distance_fade_signature \
                    != profile.channel_signature:
                raise ValueError(
                    "precomputed distance-fade vertex channels do not match "
                    "the active profile")
            values = piece.vertex_brightness
            fixed_values = piece.vertex_shade_fixed
        else:
            assert surface.authored_shade_value is not None
            authored = surface.authored_shade_value / 256.0
            brightness: list[float] = []
            for x, y, z in piece.camera_vertices:
                distance = math.sqrt(
                    x * x + y * y + (_CAMERA_DISTANCE - z) ** 2
                ) * profile.source_distance_scale
                fade = max(
                    0.0,
                    (distance - profile.fade_start) / profile.fade_length)
                brightness.append(min(1.0, max(0.0, authored + fade)))
            values = tuple(brightness)
            fixed_values = tuple(
                float(int(round(value * 64768.0)) + 0x180)
                for value in values)
        brightness_by_piece.append(values)
        fixed_by_piece.append(fixed_values)
        brightness_by_face.setdefault(piece.source_face_id, []).extend(values)

    mode_by_face: dict[Hashable, str] = {}
    for source_face_id, values in brightness_by_face.items():
        if all(value < 0.01 for value in values):
            mode_by_face[source_face_id] = "bypass"
        elif all(value > 0.99 for value in values):
            mode_by_face[source_face_id] = "black"
        else:
            mode_by_face[source_face_id] = "dynamic"

    prepared: list[_PieceDistanceFade] = []
    for piece, brightness, fixed_values in zip(
            pieces, brightness_by_piece, fixed_by_piece):
        if brightness is None:
            prepared.append(_PieceDistanceFade("inactive"))
            continue
        mode = mode_by_face[piece.source_face_id]
        if mode != "dynamic":
            prepared.append(_PieceDistanceFade(mode))
            continue
        assert fixed_values is not None
        prepared.append(_PieceDistanceFade("dynamic", fixed_values))
    return tuple(prepared)


def _distance_fade_stats(
        profile: IndexedDistanceFadeProfile,
        pieces: Sequence[IndexedPiece],
        prepared: Sequence[_PieceDistanceFade]) -> dict[str, object]:
    flagged = sum(
        1 for piece in pieces if piece.surface.authored_depth_fade_flag)
    eligible = sum(
        1 for piece in pieces if piece.surface.distance_fade_eligible)
    mode_counts = Counter(state.mode for state in prepared)
    effective = int(
        mode_counts.get("bypass", 0)
        + mode_counts.get("black", 0)
        + mode_counts.get("dynamic", 0)
    )
    return {
        "distance_fade_requested": profile.enabled,
        "distance_fade_effective": bool(profile.enabled and effective),
        "distance_fade_profile": profile.to_metadata(),
        "distance_fade_formula": INDEXED_DISTANCE_FADE_FORMULA,
        "distance_fade_flagged_piece_count": flagged,
        "distance_fade_authored_flagged_piece_count": flagged,
        "distance_fade_eligible_piece_count": eligible,
        "distance_fade_effective_piece_count": effective,
        "distance_fade_bypass_piece_count": int(
            mode_counts.get("bypass", 0)),
        "distance_fade_dynamic_piece_count": int(
            mode_counts.get("dynamic", 0)),
        "distance_fade_black_piece_count": int(
            mode_counts.get("black", 0)),
        "distance_fade_whole_polygon_scope": (
            "all BSP fragments sharing one source_face_id are classified "
            "together; viewer fan/near clipping remains a bounded "
            "reconstruction of the retail whole-polygon clipper"
        ),
        "distance_fade_changed_sample_scope": (
            "dynamic SHADERMP samples compared with the disabled constant "
            "path; full-black ZeroSpan samples are excluded"
        ),
        "distance_fade_vertex_channel_piece_count": sum(
            1 for piece in pieces if piece.vertex_brightness),
        "distance_fade_vertex_channel_space": (
            "brightness evaluated after viewer near clipping and carried "
            "through viewer BSP interpolation; raster brightness remains "
            "screen-linear"),
    }


@dataclass(frozen=True)
class IndexedRenderResult:
    """A completed index buffer and the metadata needed to display it."""

    width: int
    height: int
    indices: object = field(repr=False)
    coverage: object = field(repr=False)
    polygon_owner: object = field(repr=False)
    stats: dict[str, object]

    def to_rgba(
            self, tables: IndexedTables,
            transparent_background: bool = True) -> bytes:
        """Return packed row-major RGBA bytes.

        Alpha comes from coverage rather than the framebuffer value.  Thus an
        opaque surface that legitimately maps to index zero remains opaque,
        while an untouched index-zero background can stay transparent.
        """

        if not isinstance(tables, IndexedTables):
            raise TypeError("tables must be IndexedTables")
        display_palette = tables.display_palette

        if _np is not None:
            indices = _np.asarray(self.indices, dtype=_np.uint8)
            coverage = _np.asarray(self.coverage, dtype=bool)
            if indices.shape != (self.height, self.width):
                raise ValueError("index buffer shape does not match the result")
            if coverage.shape != indices.shape:
                raise ValueError("coverage shape does not match the index buffer")
            palette = _np.asarray(display_palette, dtype=_np.uint8)
            rgba = _np.empty((self.height, self.width, 4), dtype=_np.uint8)
            rgba[:, :, :3] = palette[indices]
            if transparent_background:
                rgba[:, :, 3] = coverage.astype(_np.uint8) * 255
            else:
                rgba[:, :, 3] = 255
            return rgba.tobytes()

        output = bytearray(self.width * self.height * 4)
        offset = 0
        for y in range(self.height):
            for x in range(self.width):
                index = self.indices[y][x]
                red, green, blue = display_palette[index]
                output[offset:offset + 4] = bytes((
                    red, green, blue,
                    255 if (not transparent_background
                            or self.coverage[y][x]) else 0,
                ))
                offset += 4
        return bytes(output)


_COUNTER_KEYS = (
    "ordered_pieces",
    "fan_triangles",
    "degenerate_triangles",
    "offscreen_triangles",
    "solid_color_triangles",
    "linear_mapped_triangles",
    "depth_mapped_triangles",
    "solid_color_samples",
    "linear_mapped_samples",
    "depth_mapped_samples",
    "source_chroma_skipped",
    "clear_source_zero_skipped",
    "flat_tracy_samples",
    "flat_tracy_source_zero_samples",
    "flat_tracy_changed_samples",
    "flat_tracy_duplicate_samples_skipped",
    "transparent_samples_occluded",
    "opaque_or_clear_samples",
    "distance_fade_samples",
    "distance_fade_dynamic_changed_samples",
)


def _finish_stats(
        counters: Counter, width: int, height: int, pieces,
        framebuffer, coverage, polygon_owner, initial_framebuffer_index: int,
        flat_tracy_destination_override: int | None) -> dict[str, object]:
    stats: dict[str, object] = {
        key: int(counters.get(key, 0)) for key in _COUNTER_KEYS
    }
    source_names = sorted({piece.surface.name for piece in pieces})
    flat_faces = {piece.source_face_id for piece in pieces
                  if piece.surface.tracy_mode == "flat"}

    if _np is not None and isinstance(framebuffer, _np.ndarray):
        covered_pixels = int(_np.count_nonzero(coverage))
        zero = framebuffer == 0
        index_zero_pixels = int(_np.count_nonzero(zero))
        covered_zero_pixels = int(_np.count_nonzero(zero & coverage))
        unique_indices = int(_np.unique(framebuffer).size)
        raw = framebuffer.tobytes()
        owners = {}
        for owner in _np.unique(polygon_owner):
            owner_value = int(owner)
            if owner_value < 0:
                continue
            ys, xs = _np.nonzero(polygon_owner == owner)
            owners[str(owner_value)] = {
                "pixels": int(xs.size),
                "bbox_xyxy": [
                    int(xs.min()), int(ys.min()),
                    int(xs.max()), int(ys.max()),
                ],
            }
    else:
        covered_pixels = 0
        index_zero_pixels = 0
        covered_zero_pixels = 0
        unique = set()
        raw_values: list[int] = []
        owner_points: dict[int, list[tuple[int, int]]] = {}
        for y in range(height):
            for x in range(width):
                value = framebuffer[y][x]
                raw_values.append(value)
                unique.add(value)
                if value == 0:
                    index_zero_pixels += 1
                    if coverage[y][x]:
                        covered_zero_pixels += 1
                if coverage[y][x]:
                    covered_pixels += 1
                owner = polygon_owner[y][x]
                if owner >= 0:
                    owner_points.setdefault(owner, []).append((x, y))
        unique_indices = len(unique)
        raw = bytes(raw_values)
        owners = {}
        for owner, points in sorted(owner_points.items()):
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            owners[str(owner)] = {
                "pixels": len(points),
                "bbox_xyxy": [min(xs), min(ys), max(xs), max(ys)],
            }

    stats.update({
        "ordered_piece_count": len(pieces),
        "flat_tracy_source_face_count": len(flat_faces),
        "source_surfaces": source_names,
        "covered_pixels": covered_pixels,
        "uncovered_pixels": width * height - covered_pixels,
        "unique_framebuffer_indices": unique_indices,
        "index_zero_pixels": index_zero_pixels,
        "covered_index_zero_pixels": covered_zero_pixels,
        "index_buffer_sha256": hashlib.sha256(raw).hexdigest(),
        "palette_exact_by_construction": True,
        "initial_framebuffer_index": int(initial_framebuffer_index),
        "flat_tracy_forced_destination_index": (
            None if flat_tracy_destination_override is None
            else int(flat_tracy_destination_override)
        ),
        "flat_tracy_destination_mode": (
            "live_framebuffer"
            if flat_tracy_destination_override is None
            else "forced_diagnostic"
        ),
        "source_faithful_frame_clear": initial_framebuffer_index == 0,
        "source_faithful_flat_destination_reads": (
            flat_tracy_destination_override is None),
        "canonical_retail_destination_policy": (
            initial_framebuffer_index == 0
            and flat_tracy_destination_override is None),
        "shader_lookup_axis_order": "SHADERMP[shade][source]",
        "tracy_lookup_axis_order": "TRACYRMP[background][raw_source]",
        "final_visible_polygon_owners": owners,
    })
    stats["distance_fade_changed_samples"] = stats[
        "distance_fade_dynamic_changed_samples"]
    return stats


def _triangle_bbox(screen, width: int, height: int):
    xs = [point[0] for point in screen]
    ys = [point[1] for point in screen]
    left = max(0, math.floor(min(xs)))
    right = min(width - 1, math.ceil(max(xs)) - 1)
    top = max(0, math.floor(min(ys)))
    bottom = min(height - 1, math.ceil(max(ys)) - 1)
    return left, right, top, bottom


def _denominator(screen) -> float:
    x0, y0 = screen[0]
    x1, y1 = screen[1]
    x2, y2 = screen[2]
    return ((y1 - y2) * (x0 - x2)
            + (x2 - x1) * (y0 - y2))


def _sample_coordinate(value: float, size: int) -> int:
    # The retail UV domain is 0..256.  int() deliberately truncates toward
    # zero, matching NumPy's conversion used by the accelerated path.
    coordinate = int(value / 256.0 * size)
    return min(size - 1, max(0, coordinate))


def _transparent_replay_metadata(
        schedule, distance_fade_states=None) -> dict[str, object]:
    seen: set[Hashable] = set()
    faces: list[dict[str, object]] = []
    for input_order, piece in schedule:
        fade_black = (
            distance_fade_states is not None
            and distance_fade_states[input_order].mode == "black"
        )
        if (fade_black or piece.surface.tracy_mode == "none"
                or piece.source_face_id in seen):
            continue
        seen.add(piece.source_face_id)
        faces.append({
            "polygon_id": int(piece.polygon_id),
            "tracy_mode": piece.surface.tracy_mode,
            "publish_depth": float(piece.sort_depth),
            "source_order_tiebreak": int(piece.source_order),
        })
    return {
        "transparent_replay_rule": (
            "opaque BSP pass, then source-derived publish-depth descending "
            "LIFO; "
            "source order is a deterministic equal-depth tie-break"),
        "transparent_replay_faces": faces,
    }


class IndexedRasterizer:
    """Stable ordered-piece rasterizer for the retail indexed colour path."""

    acceleration_backend = ACCELERATION_BACKEND
    max_safe_pixels = MAX_SAFE_PIXELS

    @staticmethod
    def _retail_pass_order(pieces, distance_fade_states=None):
        """Replay transparent source faces after opaque BSP pieces.

        The retail software spangine pushes clear/LUM-TRACY spans while the
        front-to-back publish pass runs, then pops that glass stack LIFO at
        ``End3D``.  BSP fragments therefore must not be interleaved as if they
        were ordinary opaque painter pieces. ``input_order`` is retained for
        opaque-occlusion gating; ``sort_depth`` reconstructs the whole-face
        LIFO order which BSP splitting would otherwise destroy, while
        ``source_order`` is only a deterministic equal-depth tie-break.
        """

        indexed = list(enumerate(pieces))
        states = (
            distance_fade_states
            if distance_fade_states is not None
            else (_PieceDistanceFade("inactive"),) * len(indexed)
        )

        def is_effectively_opaque(item) -> bool:
            input_order, piece = item
            return (states[input_order].mode == "black"
                    or piece.surface.tracy_mode == "none")

        opaque = [item for item in indexed
                  if is_effectively_opaque(item)]
        transparent = [item for item in indexed
                       if not is_effectively_opaque(item)]
        transparent.sort(
            key=lambda item: (
                item[1].sort_depth, item[1].source_order, item[0]),
            reverse=True)
        return opaque + transparent

    @staticmethod
    def render(
            width: int, height: int, pieces: Iterable[IndexedPiece],
            tables: IndexedTables, background_index: int = 0,
            flat_tracy_destination_override: int | None = None,
            distance_fade_profile: IndexedDistanceFadeProfile | None = None,
            ) -> IndexedRenderResult:
        """Render an indexed frame, optionally forcing only flat-TRACY reads.

        ``background_index`` initializes the framebuffer.  The optional
        override is intentionally separate: it substitutes the TRACYRMP row
        for every flat/LUM-TRACY lookup while output, coverage, and occlusion
        continue to operate on the real framebuffer.  That override is a
        diagnostic primitive, not retail live-destination compositing.  An
        enabled ``distance_fade_profile`` applies recovered per-vertex AREA
        brightness only to surfaces the structural adapter marked eligible.
        """

        try:
            width = operator.index(width)
            height = operator.index(height)
        except TypeError as exc:
            raise TypeError("render dimensions must be integers") from exc
        if width <= 0 or height <= 0:
            raise ValueError("render dimensions must be positive")
        if not isinstance(tables, IndexedTables):
            raise TypeError("tables must be IndexedTables")
        background = _u8(background_index, "background index")
        forced_destination = (
            None
            if flat_tracy_destination_override is None
            else _u8(
                flat_tracy_destination_override,
                "forced TRACY destination index",
            )
        )
        ordered = tuple(pieces)
        if not all(isinstance(piece, IndexedPiece) for piece in ordered):
            raise TypeError("pieces must contain only IndexedPiece objects")
        if distance_fade_profile is None:
            distance_fade_profile = IndexedDistanceFadeProfile()
        elif not isinstance(
                distance_fade_profile, IndexedDistanceFadeProfile):
            raise TypeError(
                "distance_fade_profile must be an "
                "IndexedDistanceFadeProfile or None")
        distance_fade_states = _prepare_distance_fade(
            ordered, distance_fade_profile)

        if _np is not None:
            return IndexedRasterizer._render_numpy(
                width, height, ordered, tables, background,
                forced_destination, distance_fade_profile,
                distance_fade_states)
        return IndexedRasterizer._render_python(
            width, height, ordered, tables, background,
            forced_destination, distance_fade_profile,
            distance_fade_states)

    @staticmethod
    def _render_numpy(
            width, height, pieces, tables, background,
            forced_tracy_destination, distance_fade_profile,
            distance_fade_states):
        framebuffer = _np.full(
            (height, width), background, dtype=_np.uint8)
        coverage = _np.zeros((height, width), dtype=bool)
        polygon_owner = _np.full((height, width), -1, dtype=_np.int64)
        opaque_order = _np.full((height, width), -1, dtype=_np.int64)
        counters = Counter()
        schedule = IndexedRasterizer._retail_pass_order(
            pieces, distance_fade_states)

        flat_face_ids = []
        flat_face_set = set()
        for _input_order, piece in schedule:
            if (piece.surface.tracy_mode == "flat"
                    and piece.source_face_id not in flat_face_set):
                flat_face_set.add(piece.source_face_id)
                flat_face_ids.append(piece.source_face_id)

        # A compact per-pixel face bitset is fast for normal assets.  More
        # than 64 effect faces remain supported through a sparse fallback.
        if not flat_face_ids:
            flat_seen = None
            flat_bits = {}
            sparse_seen = None
        elif len(flat_face_ids) <= 64:
            dtype = (
                _np.uint8 if len(flat_face_ids) <= 8 else
                _np.uint16 if len(flat_face_ids) <= 16 else
                _np.uint32 if len(flat_face_ids) <= 32 else _np.uint64
            )
            flat_seen = _np.zeros((height, width), dtype=dtype)
            flat_bits = {
                face_id: dtype(1 << index)
                for index, face_id in enumerate(flat_face_ids)
            }
            sparse_seen = None
        else:
            flat_seen = None
            flat_bits = {}
            sparse_seen = {face_id: set() for face_id in flat_face_ids}

        shader = _np.frombuffer(
            tables.shader_pixels, dtype=_np.uint8).reshape(256, 256)
        tracy = _np.frombuffer(
            tables.tracy_pixels, dtype=_np.uint8).reshape(256, 256)

        for input_order, piece in schedule:
            counters["ordered_pieces"] += 1
            for index in range(2, len(piece.screen)):
                # Match the existing viewer's fan orientation.  Barycentric
                # coverage is orientation-independent, while this order keeps
                # edge behaviour stable against the audited reference pass.
                tri_indices = (0, index, index - 1)
                tri_screen = tuple(piece.screen[item] for item in tri_indices)
                tri_uvs = (
                    tuple(piece.uvs[item] for item in tri_indices)
                    if piece.surface.kind == "texture" else ())
                tri_camera = tuple(
                    piece.camera_vertices[item] for item in tri_indices)
                counters["fan_triangles"] += 1
                IndexedRasterizer._triangle_numpy(
                    framebuffer, coverage, polygon_owner, opaque_order,
                    flat_seen, flat_bits.get(piece.source_face_id),
                    None if sparse_seen is None
                    else sparse_seen[piece.source_face_id]
                    if piece.surface.tracy_mode == "flat" else None,
                    piece, input_order, tri_screen, tri_uvs, tri_camera,
                    distance_fade_states[input_order], tri_indices,
                    shader, tracy, counters, forced_tracy_destination,
                )

        stats = _finish_stats(
            counters, width, height, pieces,
            framebuffer, coverage, polygon_owner, background,
            forced_tracy_destination)
        stats.update(_transparent_replay_metadata(
            schedule, distance_fade_states))
        stats.update(_distance_fade_stats(
            distance_fade_profile, pieces, distance_fade_states))
        return IndexedRenderResult(
            width, height, framebuffer, coverage, polygon_owner, stats)

    @staticmethod
    def _triangle_numpy(
            framebuffer, coverage, polygon_owner, opaque_order,
            flat_seen, flat_face_bit, sparse_seen,
            piece, input_order, screen, uvs, camera_vertices,
            distance_fade_state, triangle_indices,
            shader, tracy, counters, forced_tracy_destination):
        height, width = framebuffer.shape
        left, right, top, bottom = _triangle_bbox(screen, width, height)
        if left > right or top > bottom:
            counters["offscreen_triangles"] += 1
            return
        denominator = _denominator(screen)
        if abs(denominator) <= 1e-12:
            counters["degenerate_triangles"] += 1
            return

        surface = piece.surface
        fade_black = distance_fade_state.mode == "black"
        fade_bypass = distance_fade_state.mode == "bypass"
        fade_dynamic = distance_fade_state.mode == "dynamic"
        effective_solid = surface.kind == "solid" or fade_black
        effective_tracy_mode = "none" if fade_black else surface.tracy_mode
        if effective_solid:
            counters["solid_color_triangles"] += 1
        elif surface.map_mode == "linear":
            counters["linear_mapped_triangles"] += 1
        else:
            counters["depth_mapped_triangles"] += 1

        x0, y0 = screen[0]
        x1, y1 = screen[1]
        x2, y2 = screen[2]
        grid_x = _np.arange(left, right + 1, dtype=_np.float64)[None, :] + 0.5
        uv_array = (
            _np.asarray(uvs, dtype=_np.float64)
            if surface.kind == "texture" and not fade_black else None)
        reciprocal_depth = None
        if (surface.kind == "texture" and not fade_black
                and surface.map_mode == "depth"):
            reciprocal_depth = _np.asarray([
                1.0 / max(_MINIMUM_DEPTH, _CAMERA_DISTANCE - vertex[2])
                for vertex in camera_vertices
            ], dtype=_np.float64)
        texture = (
            _np.frombuffer(surface.indices, dtype=_np.uint8).reshape(
                surface.height, surface.width)
            if surface.kind == "texture" and not fade_black else None)
        triangle_shade_fixed = (
            _np.asarray([
                distance_fade_state.vertex_shade_fixed[item]
                for item in triangle_indices
            ], dtype=_np.float64)
            if fade_dynamic else None
        )
        for strip_top in range(top, bottom + 1, 64):
            strip_bottom = min(bottom + 1, strip_top + 64)
            grid_y = (
                _np.arange(strip_top, strip_bottom, dtype=_np.float64)[:, None]
                + 0.5)
            weight0 = (
                (y1 - y2) * (grid_x - x2)
                + (x2 - x1) * (grid_y - y2)
            ) / denominator
            weight1 = (
                (y2 - y0) * (grid_x - x2)
                + (x0 - x2) * (grid_y - y2)
            ) / denominator
            weight2 = 1.0 - weight0 - weight1
            inside = (
                (weight0 >= -_INSIDE_EPSILON)
                & (weight1 >= -_INSIDE_EPSILON)
                & (weight2 >= -_INSIDE_EPSILON)
            )

            if flat_face_bit is not None:
                seen_view = flat_seen[
                    strip_top:strip_bottom, left:right + 1]
                duplicate = inside & (
                    _np.bitwise_and(seen_view, flat_face_bit) != 0)
                counters["flat_tracy_duplicate_samples_skipped"] += int(
                    _np.count_nonzero(duplicate))
                inside &= ~duplicate

            row_local, column_local = _np.nonzero(inside)
            if not row_local.size:
                continue
            destination_y = strip_top + row_local
            destination_x = left + column_local

            if sparse_seen is not None:
                linear = destination_y.astype(_np.int64) * width + destination_x
                fresh = _np.fromiter(
                    (int(value) not in sparse_seen for value in linear),
                    dtype=bool, count=linear.size)
                counters["flat_tracy_duplicate_samples_skipped"] += int(
                    _np.count_nonzero(~fresh))
                if not _np.all(fresh):
                    row_local = row_local[fresh]
                    column_local = column_local[fresh]
                    destination_y = destination_y[fresh]
                    destination_x = destination_x[fresh]
                if not row_local.size:
                    continue

            w0 = weight0[row_local, column_local]
            w1 = weight1[row_local, column_local]
            w2 = weight2[row_local, column_local]
            if effective_solid:
                raw_source = _np.full(
                    row_local.size,
                    0 if fade_black else surface.solid_index,
                    dtype=_np.uint8)
                counters["solid_color_samples"] += int(raw_source.size)
            elif surface.map_mode == "linear":
                u = w0 * uv_array[0, 0] + w1 * uv_array[1, 0] \
                    + w2 * uv_array[2, 0]
                v = w0 * uv_array[0, 1] + w1 * uv_array[1, 1] \
                    + w2 * uv_array[2, 1]
                counters["linear_mapped_samples"] += int(u.size)
                source_x = _np.clip(
                    (u / 256.0 * surface.width).astype(_np.int64),
                    0, surface.width - 1)
                source_y = _np.clip(
                    (v / 256.0 * surface.height).astype(_np.int64),
                    0, surface.height - 1)
                raw_source = texture[source_y, source_x]

            else:
                perspective_total = (
                    w0 * reciprocal_depth[0]
                    + w1 * reciprocal_depth[1]
                    + w2 * reciprocal_depth[2])
                valid = perspective_total > 1e-15
                if not _np.all(valid):
                    row_local = row_local[valid]
                    column_local = column_local[valid]
                    destination_y = destination_y[valid]
                    destination_x = destination_x[valid]
                    w0 = w0[valid]
                    w1 = w1[valid]
                    w2 = w2[valid]
                    perspective_total = perspective_total[valid]
                if not row_local.size:
                    continue
                u = (
                    w0 * reciprocal_depth[0] * uv_array[0, 0]
                    + w1 * reciprocal_depth[1] * uv_array[1, 0]
                    + w2 * reciprocal_depth[2] * uv_array[2, 0]
                ) / perspective_total
                v = (
                    w0 * reciprocal_depth[0] * uv_array[0, 1]
                    + w1 * reciprocal_depth[1] * uv_array[1, 1]
                    + w2 * reciprocal_depth[2] * uv_array[2, 1]
                ) / perspective_total
                counters["depth_mapped_samples"] += int(u.size)
                source_x = _np.clip(
                    (u / 256.0 * surface.width).astype(_np.int64),
                    0, surface.width - 1)
                source_y = _np.clip(
                    (v / 256.0 * surface.height).astype(_np.int64),
                    0, surface.height - 1)
                raw_source = texture[source_y, source_x]

            shade_rows = None
            if fade_dynamic:
                shade_fixed = (
                    w0 * triangle_shade_fixed[0]
                    + w1 * triangle_shade_fixed[1]
                    + w2 * triangle_shade_fixed[2]
                )
                shade_rows = _np.clip(
                    _np.floor(shade_fixed / 256.0).astype(_np.int64),
                    0, 255,
                )

            # The original ordinary mapped span writes every raw texel.  Its
            # clear-TRACY sibling skips only numeric source index zero before
            # SHADERMP; it does not search the palette for a chroma RGB.  The
            # flat LNF span passes zero through TRACYRMP instead.
            if (not fade_black and surface.kind == "texture"
                    and effective_tracy_mode == "clear"):
                visible = raw_source != 0
                if not _np.all(visible):
                    skipped = int(_np.count_nonzero(~visible))
                    # Retain the old key for manifest compatibility while the
                    # precise source-derived name records the real predicate.
                    counters["source_chroma_skipped"] += skipped
                    counters["clear_source_zero_skipped"] += skipped
                    destination_y = destination_y[visible]
                    destination_x = destination_x[visible]
                    raw_source = raw_source[visible]
                    if shade_rows is not None:
                        shade_rows = shade_rows[visible]
            if not raw_source.size:
                continue

            if effective_tracy_mode != "none":
                visible = (
                    opaque_order[destination_y, destination_x] <= input_order)
                if not _np.all(visible):
                    counters["transparent_samples_occluded"] += int(
                        _np.count_nonzero(~visible))
                    destination_y = destination_y[visible]
                    destination_x = destination_x[visible]
                    raw_source = raw_source[visible]
                    if shade_rows is not None:
                        shade_rows = shade_rows[visible]
            if not raw_source.size:
                continue

            if effective_tracy_mode == "flat":
                background = framebuffer[destination_y, destination_x]
                # Original span_lnf is explicitly "linear mapped, no shade,
                # remap Tracy": raw texels bypass SHADERMP and index the low
                # byte of (background << 8) | source.
                lookup_background = (
                    background
                    if forced_tracy_destination is None
                    else forced_tracy_destination
                )
                output = tracy[lookup_background, raw_source]
                changed = output != background
                counters["flat_tracy_samples"] += int(output.size)
                counters["flat_tracy_source_zero_samples"] += int(
                    _np.count_nonzero(raw_source == 0))
                counters["flat_tracy_changed_samples"] += int(
                    _np.count_nonzero(changed))
                framebuffer[destination_y, destination_x] = output
                # Flat composition preserves the destination's coverage on a
                # no-op and creates visible coverage only when it changes it.
                coverage[destination_y, destination_x] |= changed
                if flat_face_bit is not None:
                    flat_seen[destination_y, destination_x] |= flat_face_bit
                else:
                    sparse_seen.update(
                        (destination_y.astype(_np.int64) * width
                         + destination_x).tolist())
            else:  # none and clear are both ordinary shaded overwrite modes.
                if fade_black:
                    mapped_source = raw_source
                    counters["distance_fade_samples"] += int(
                        mapped_source.size)
                elif fade_bypass:
                    # The source clears the shade flag for an all-white
                    # depth-faded polygon, even if the structural dispatch was
                    # authored as gradient-shaded.
                    mapped_source = raw_source
                elif fade_dynamic:
                    mapped_source = shader[shade_rows, raw_source]
                    baseline = (
                        shader[surface.shade_value, raw_source]
                        if surface.shade_mode != "none" else raw_source
                    )
                    counters["distance_fade_samples"] += int(
                        mapped_source.size)
                    counters["distance_fade_dynamic_changed_samples"] += int(
                        _np.count_nonzero(mapped_source != baseline))
                else:
                    mapped_source = (
                        shader[surface.shade_value, raw_source]
                        if surface.shade_mode != "none" else raw_source)
                counters["opaque_or_clear_samples"] += int(mapped_source.size)
                framebuffer[destination_y, destination_x] = mapped_source
                coverage[destination_y, destination_x] = True
                polygon_owner[destination_y, destination_x] = piece.polygon_id
                if effective_tracy_mode == "none":
                    opaque_order[destination_y, destination_x] = input_order

    @staticmethod
    def _render_python(
            width, height, pieces, tables, background,
            forced_tracy_destination, distance_fade_profile,
            distance_fade_states):
        framebuffer = [[background for _ in range(width)] for _ in range(height)]
        coverage = [[False for _ in range(width)] for _ in range(height)]
        polygon_owner = [[-1 for _ in range(width)] for _ in range(height)]
        opaque_order = [[-1 for _ in range(width)] for _ in range(height)]
        flat_seen: dict[Hashable, set[int]] = {}
        counters = Counter()
        schedule = IndexedRasterizer._retail_pass_order(
            pieces, distance_fade_states)

        for input_order, piece in schedule:
            counters["ordered_pieces"] += 1
            if piece.surface.tracy_mode == "flat":
                flat_seen.setdefault(piece.source_face_id, set())
            for index in range(2, len(piece.screen)):
                tri_indices = (0, index, index - 1)
                screen = tuple(piece.screen[item] for item in tri_indices)
                uvs = (
                    tuple(piece.uvs[item] for item in tri_indices)
                    if piece.surface.kind == "texture" else ())
                camera = tuple(
                    piece.camera_vertices[item] for item in tri_indices)
                counters["fan_triangles"] += 1
                IndexedRasterizer._triangle_python(
                    framebuffer, coverage, polygon_owner, opaque_order,
                    flat_seen.get(piece.source_face_id), piece,
                    input_order, screen, uvs, camera, tables, counters,
                    forced_tracy_destination,
                    distance_fade_states[input_order], tri_indices)

        stats = _finish_stats(
            counters, width, height, pieces,
            framebuffer, coverage, polygon_owner, background,
            forced_tracy_destination)
        stats.update(_transparent_replay_metadata(
            schedule, distance_fade_states))
        stats.update(_distance_fade_stats(
            distance_fade_profile, pieces, distance_fade_states))
        return IndexedRenderResult(
            width, height, framebuffer, coverage, polygon_owner, stats)

    @staticmethod
    def _triangle_python(
            framebuffer, coverage, polygon_owner, opaque_order, face_seen,
            piece, input_order, screen, uvs, camera_vertices, tables, counters,
            forced_tracy_destination, distance_fade_state, triangle_indices):
        height = len(framebuffer)
        width = len(framebuffer[0])
        left, right, top, bottom = _triangle_bbox(screen, width, height)
        if left > right or top > bottom:
            counters["offscreen_triangles"] += 1
            return
        denominator = _denominator(screen)
        if abs(denominator) <= 1e-12:
            counters["degenerate_triangles"] += 1
            return

        surface = piece.surface
        fade_black = distance_fade_state.mode == "black"
        fade_bypass = distance_fade_state.mode == "bypass"
        fade_dynamic = distance_fade_state.mode == "dynamic"
        effective_solid = surface.kind == "solid" or fade_black
        effective_tracy_mode = "none" if fade_black else surface.tracy_mode
        if effective_solid:
            counters["solid_color_triangles"] += 1
        elif surface.map_mode == "linear":
            counters["linear_mapped_triangles"] += 1
        else:
            counters["depth_mapped_triangles"] += 1

        x0, y0 = screen[0]
        x1, y1 = screen[1]
        x2, y2 = screen[2]
        reciprocal_depth = [
            1.0 / max(_MINIMUM_DEPTH, _CAMERA_DISTANCE - vertex[2])
            for vertex in camera_vertices
        ]

        for y in range(top, bottom + 1):
            sample_y = y + 0.5
            for x in range(left, right + 1):
                sample_x = x + 0.5
                w0 = (
                    (y1 - y2) * (sample_x - x2)
                    + (x2 - x1) * (sample_y - y2)
                ) / denominator
                w1 = (
                    (y2 - y0) * (sample_x - x2)
                    + (x0 - x2) * (sample_y - y2)
                ) / denominator
                w2 = 1.0 - w0 - w1
                if (w0 < -_INSIDE_EPSILON
                        or w1 < -_INSIDE_EPSILON
                        or w2 < -_INSIDE_EPSILON):
                    continue

                pixel_key = y * width + x
                if face_seen is not None and pixel_key in face_seen:
                    counters["flat_tracy_duplicate_samples_skipped"] += 1
                    continue

                if effective_solid:
                    raw_source = 0 if fade_black else surface.solid_index
                    counters["solid_color_samples"] += 1
                else:
                    if surface.map_mode == "linear":
                        u = w0 * uvs[0][0] + w1 * uvs[1][0] + w2 * uvs[2][0]
                        v = w0 * uvs[0][1] + w1 * uvs[1][1] + w2 * uvs[2][1]
                        counters["linear_mapped_samples"] += 1
                    else:
                        total = (
                            w0 * reciprocal_depth[0]
                            + w1 * reciprocal_depth[1]
                            + w2 * reciprocal_depth[2])
                        if total <= 1e-15:
                            continue
                        u = (
                            w0 * reciprocal_depth[0] * uvs[0][0]
                            + w1 * reciprocal_depth[1] * uvs[1][0]
                            + w2 * reciprocal_depth[2] * uvs[2][0]
                        ) / total
                        v = (
                            w0 * reciprocal_depth[0] * uvs[0][1]
                            + w1 * reciprocal_depth[1] * uvs[1][1]
                            + w2 * reciprocal_depth[2] * uvs[2][1]
                        ) / total
                        counters["depth_mapped_samples"] += 1
                    source_x = _sample_coordinate(u, surface.width)
                    source_y = _sample_coordinate(v, surface.height)
                    raw_source = surface.indices[
                        source_y * surface.width + source_x]
                    if effective_tracy_mode == "clear" and raw_source == 0:
                        counters["source_chroma_skipped"] += 1
                        counters["clear_source_zero_skipped"] += 1
                        continue

                if (effective_tracy_mode != "none"
                        and opaque_order[y][x] > input_order):
                    counters["transparent_samples_occluded"] += 1
                    continue

                if effective_tracy_mode == "flat":
                    background = framebuffer[y][x]
                    lookup_background = (
                        background
                        if forced_tracy_destination is None
                        else forced_tracy_destination
                    )
                    output = tables.tracy_index(
                        lookup_background, raw_source)
                    counters["flat_tracy_samples"] += 1
                    if raw_source == 0:
                        counters["flat_tracy_source_zero_samples"] += 1
                    if output != background:
                        counters["flat_tracy_changed_samples"] += 1
                        coverage[y][x] = True
                    framebuffer[y][x] = output
                    face_seen.add(pixel_key)
                else:
                    if fade_black:
                        mapped_source = raw_source
                        counters["distance_fade_samples"] += 1
                    elif fade_bypass:
                        # All-white AREA depth fade selects the unshaded span.
                        mapped_source = raw_source
                    elif fade_dynamic:
                        fixed = sum(
                            weight * distance_fade_state.vertex_shade_fixed[item]
                            for weight, item in zip(
                                (w0, w1, w2), triangle_indices)
                        )
                        shade_row = min(255, max(0, int(fixed // 256)))
                        mapped_source = tables.shade_index(
                            shade_row, raw_source)
                        baseline = (
                            tables.shade_index(
                                surface.shade_value, raw_source)
                            if surface.shade_mode != "none" else raw_source
                        )
                        counters["distance_fade_samples"] += 1
                        if mapped_source != baseline:
                            counters[
                                "distance_fade_dynamic_changed_samples"] += 1
                    else:
                        mapped_source = (
                            tables.shade_index(surface.shade_value, raw_source)
                            if surface.shade_mode != "none" else raw_source)
                    counters["opaque_or_clear_samples"] += 1
                    framebuffer[y][x] = mapped_source
                    coverage[y][x] = True
                    polygon_owner[y][x] = piece.polygon_id
                    if effective_tracy_mode == "none":
                        opaque_order[y][x] = input_order


__all__ = [
    "ACCELERATION_BACKEND",
    "MAX_SAFE_PIXELS",
    "IndexedDistanceFadeProfile",
    "IndexedTables",
    "IndexedSurface",
    "IndexedPiece",
    "IndexedRenderResult",
    "IndexedRasterizer",
]
