"""Bounded CPU raster helper for legacy native PSW/PSV previews.

This module is deliberately narrower than a PlayStation GPU emulator.  It
provides the one missing operation needed to preview the older version-1
PSW/PSV faces without asking Qt to invent their per-corner colour behavior:
affine screen-space material-local UV and grayscale-shade interpolation
followed by the evidenced PSX textured-modulation equation.  The UV inputs
are the bounded descriptor-relative quotients produced by
``psw_material_local_uv_quotient``; they are direct coordinates in the
decoded 128 x 128 material and are not byte-scaled.

The helper does *not* claim recovered GTE projection, GPU fixed-point edge
walking, ordering-table behavior, dithering, or semi-transparency.  A resolved
CLUT word of zero leaves the output transparent.  Every other word is emitted
opaque, including an STP-set word, because the legacy primitive's TPage/ABR
state remains unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math

try:  # NumPy accelerates the raster but is not part of its public contract.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised on minimal installations.
    _np = None

from psx_gpu_colors import (
    PsxGpuColorError,
    psx_texture_modulate_bgr555,
)
from psx_native_textures import (
    CLUT_ENTRY_COUNT,
    INDEXED_PIXEL_BYTES,
    TEXTURE_HEIGHT,
    TEXTURE_WIDTH,
    PsxNativeMaterial,
)


PSX_LEGACY_PSW_RASTER_PROFILE_ID = (
    "psx_legacy_psw_material_local_affine_modulation_preview_v2")
PSX_NATIVE_RASTER_ACCELERATION_BACKEND = (
    "numpy" if _np is not None else "python")

# These are safety boundaries, not hardware limits.  The patch-area boundary
# caps both the pure-Python work and the largest temporary RGBA allocation.
# Clipping is performed before this limit is evaluated, so enormous but wholly
# offscreen geometry never creates an enormous buffer.
MAX_TARGET_DIMENSION = 65536
MAX_SCREEN_COORDINATE_ABS = float(1 << 30)
MAX_PATCH_PIXELS = 4096 * 4096
MIN_DOUBLE_AREA = 1.0e-9
# Bound vector temporaries independently of the existing full-patch ceiling.
# A wide candidate may exceed this strip size by one complete row; target
# width is itself capped above, so that case remains small and explicit.
NUMPY_STRIP_PIXELS = 512 * 1024
# Python 3.12+ uses compensated summation for ``sum`` of floats.  NumPy's
# elementwise additions can consequently land one ULP across an exact texel or
# shade half-up boundary.  Recheck only candidates this close to a boundary
# through the scalar reference sequence.
SCALAR_QUANTIZATION_RECHECK_EPSILON = 1.0e-10


class PsxNativeRasterError(ValueError):
    """Raised when a legacy preview raster request is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class PsxNativeRasterPatch:
    """One tightly cropped RGBA patch in target coordinates.

    ``coverage_count`` counts samples owned by the triangle's top-left rule,
    including samples whose resolved CLUT word is zero.  Uncovered samples and
    zero-word samples are both transparent in ``rgba``; a caller can therefore
    composite the patch directly without a second mask.
    """

    origin_x: int
    origin_y: int
    width: int
    height: int
    rgba: bytes
    coverage_count: int

    def __post_init__(self) -> None:
        values = (
            ("origin_x", self.origin_x),
            ("origin_y", self.origin_y),
            ("width", self.width),
            ("height", self.height),
            ("coverage_count", self.coverage_count),
        )
        for field, value in values:
            if type(value) is not int:
                raise PsxNativeRasterError(f"{field} must be an integer")
            if value < 0:
                raise PsxNativeRasterError(f"{field} may not be negative")
        if not isinstance(self.rgba, bytes):
            raise PsxNativeRasterError("patch RGBA payload must be bytes")
        if len(self.rgba) != self.width * self.height * 4:
            raise PsxNativeRasterError(
                "patch RGBA length does not match width and height")
        if (self.width == 0) != (self.height == 0):
            raise PsxNativeRasterError(
                "an empty patch must have both dimensions set to zero")
        if self.width == 0:
            if self.rgba or self.coverage_count:
                raise PsxNativeRasterError(
                    "an empty patch may not contain pixels or coverage")
        elif self.coverage_count > self.width * self.height:
            raise PsxNativeRasterError(
                "patch coverage exceeds its pixel area")

    @property
    def target_origin(self) -> tuple[int, int]:
        """Return the patch's upper-left coordinate in the target."""

        return self.origin_x, self.origin_y

    @property
    def is_empty(self) -> bool:
        return self.width == 0


def _plain_bounded_int(
        value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise PsxNativeRasterError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise PsxNativeRasterError(
            f"{field} {value!r} is outside {minimum}..{maximum}")
    return value


def _screen_triangle(
        value: object) -> tuple[
            tuple[float, float], tuple[float, float], tuple[float, float]]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise PsxNativeRasterError(
            "screen triangle must contain exactly three points")
    points: list[tuple[float, float]] = []
    for point_index, point in enumerate(value):
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise PsxNativeRasterError(
                f"screen point {point_index} must contain x and y")
        coordinates: list[float] = []
        for axis, coordinate in zip(("x", "y"), point):
            if isinstance(coordinate, bool) or not isinstance(
                    coordinate, (int, float)):
                raise PsxNativeRasterError(
                    f"screen point {point_index} {axis} must be numeric")
            number = float(coordinate)
            if not math.isfinite(number):
                raise PsxNativeRasterError(
                    f"screen point {point_index} {axis} must be finite")
            if abs(number) > MAX_SCREEN_COORDINATE_ABS:
                raise PsxNativeRasterError(
                    f"screen point {point_index} {axis} exceeds the safe "
                    "coordinate boundary")
            coordinates.append(number)
        points.append((coordinates[0], coordinates[1]))
    return points[0], points[1], points[2]


def _three_uvs(
        value: object) -> tuple[
            tuple[float, float], tuple[float, float], tuple[float, float]]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise PsxNativeRasterError(
            "authored UVs must contain exactly three pairs")
    result: list[tuple[float, float]] = []
    for corner, uv in enumerate(value):
        if not isinstance(uv, (tuple, list)) or len(uv) != 2:
            raise PsxNativeRasterError(
                f"authored UV {corner} must contain u and v")
        components = []
        for axis, component in zip(("u", "v"), uv):
            if isinstance(component, bool) or not isinstance(
                    component, (int, float)):
                raise PsxNativeRasterError(
                    f"authored UV {corner} {axis} must be numeric")
            number = float(component)
            if not math.isfinite(number) or not 0.0 <= number <= 127.0:
                raise PsxNativeRasterError(
                    f"authored UV {corner} {axis} {component!r} is outside "
                    "the material-local range 0..127")
            components.append(number)
        result.append((components[0], components[1]))
    return result[0], result[1], result[2]


def _three_shades(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise PsxNativeRasterError(
            "corner shades must contain exactly three grayscale values")
    shades = []
    for corner, shade in enumerate(value):
        if isinstance(shade, bool) or not isinstance(shade, (int, float)):
            raise PsxNativeRasterError(
                f"corner shade {corner} must be numeric")
        number = float(shade)
        if not math.isfinite(number) or not 0.0 <= number <= 0xFF:
            raise PsxNativeRasterError(
                f"corner shade {corner} {shade!r} is outside 0..255")
        shades.append(number)
    return shades[0], shades[1], shades[2]


def _validate_material(material: object) -> PsxNativeMaterial:
    if not isinstance(material, PsxNativeMaterial):
        raise PsxNativeRasterError(
            "legacy raster input must be a resolved PsxNativeMaterial")
    if type(material.width) is not int or type(material.height) is not int \
            or material.width != TEXTURE_WIDTH \
            or material.height != TEXTURE_HEIGHT:
        raise PsxNativeRasterError(
            "legacy raster material must be the native 128 x 128 texture")
    if not isinstance(material.indices, bytes) \
            or len(material.indices) != INDEXED_PIXEL_BYTES:
        raise PsxNativeRasterError(
            "legacy raster material has an invalid indexed-pixel payload")
    if not isinstance(material.palette_words, tuple) \
            or len(material.palette_words) != CLUT_ENTRY_COUNT:
        raise PsxNativeRasterError(
            "legacy raster material must contain exactly 16 CLUT words")
    for index, word in enumerate(material.palette_words):
        _plain_bounded_int(
            word, field=f"material CLUT word {index}",
            minimum=0, maximum=0xFFFF)
    return material


def _edge(
        start: tuple[float, float], end: tuple[float, float],
        x: float, y: float) -> float:
    return ((end[0] - start[0]) * (y - start[1])
            - (end[1] - start[1]) * (x - start[0]))


def _is_top_left(
        start: tuple[float, float], end: tuple[float, float]) -> bool:
    """Classify an oriented edge for pixel coordinates whose Y grows down."""

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    return dy < 0.0 or (dy == 0.0 and dx > 0.0)


def _edge_owns_sample(value: float, top_left: bool) -> bool:
    return value > 0.0 or (value == 0.0 and top_left)


def _nearest_material_local_texel(value: float, extent: int) -> int:
    """Sample a descriptor-relative material-local coordinate directly.

    Nearest sampling uses deterministic half-up ownership instead of Python's
    banker's ``round``.  Clamping is defensive for interpolated floating-point
    attributes at an exact material edge; authored inputs are already bounded
    to 0..127.
    """

    local = max(0.0, min(float(extent - 1), value))
    return max(0, min(extent - 1, int(math.floor(local + 0.5))))


def _nearest_u8(value: float) -> int:
    return max(0, min(0xFF, int(math.floor(value + 0.5))))


def _empty_patch() -> PsxNativeRasterPatch:
    return PsxNativeRasterPatch(
        origin_x=0,
        origin_y=0,
        width=0,
        height=0,
        rgba=b"",
        coverage_count=0,
    )


@lru_cache(maxsize=128)
def _modulation_rgba_lut(palette_words: tuple[int, ...]) -> bytes:
    """Build the exact shade-by-CLUT RGBA table through shared GPU math.

    The table is backend-neutral and immutable.  Using it from both paths
    keeps NumPy an acceleration of the same color contract rather than a
    second implementation of the modulation equation.
    """

    output = bytearray(CLUT_ENTRY_COUNT * 256 * 4)
    for palette_index, source_word in enumerate(palette_words):
        if source_word == 0:
            continue
        for shade in range(256):
            try:
                modulated = psx_texture_modulate_bgr555(
                    source_word, (shade, shade, shade))
            except PsxGpuColorError as exc:  # Defensive after validation.
                raise PsxNativeRasterError(
                    f"PSX texture modulation failed: {exc}") from exc
            # The exact modulation helper intentionally returns the GPU's
            # wide pre-dither intermediate (0..494).  This RGBA preview has no
            # ABR or dither stage, so its conservative opaque presentation
            # saturates that intermediate to the byte domain only here.
            destination = (palette_index * 256 + shade) * 4
            output[destination:destination + 4] = bytes((
                min(0xFF, modulated[0]),
                min(0xFF, modulated[1]),
                min(0xFF, modulated[2]),
                0xFF,
            ))
    return bytes(output)


def _edge_contract(
        points: list[tuple[float, float]]) -> tuple[
            tuple[tuple[tuple[float, float], tuple[float, float]], ...],
            tuple[bool, bool, bool]]:
    edge_pairs = (
        (points[1], points[2]),
        (points[2], points[0]),
        (points[0], points[1]),
    )
    top_left = tuple(_is_top_left(start, end)
                     for start, end in edge_pairs)
    return edge_pairs, top_left  # type: ignore[return-value]


def _rasterize_python(
        *,
        points: list[tuple[float, float]],
        uvs: list[tuple[float, float]],
        shades: list[float],
        native_material: PsxNativeMaterial,
        double_area: float,
        left: int,
        right: int,
        top: int,
        bottom: int) -> PsxNativeRasterPatch:
    """Reference scalar backend retained for installations without NumPy."""

    candidate_width = right - left + 1
    candidate_height = bottom - top + 1
    output = bytearray(candidate_width * candidate_height * 4)
    edge_pairs, top_left = _edge_contract(points)
    modulation_lut = _modulation_rgba_lut(
        native_material.palette_words)
    coverage_count = 0
    covered_left = right + 1
    covered_right = left - 1
    covered_top = bottom + 1
    covered_bottom = top - 1

    for target_y in range(top, bottom + 1):
        sample_y = target_y + 0.5
        for target_x in range(left, right + 1):
            sample_x = target_x + 0.5
            edge_values = tuple(
                _edge(start, end, sample_x, sample_y)
                for start, end in edge_pairs)
            if not all(
                    _edge_owns_sample(value, owns_edge)
                    for value, owns_edge in zip(edge_values, top_left)):
                continue

            coverage_count += 1
            covered_left = min(covered_left, target_x)
            covered_right = max(covered_right, target_x)
            covered_top = min(covered_top, target_y)
            covered_bottom = max(covered_bottom, target_y)

            weights = tuple(value / double_area for value in edge_values)
            interpolated_u = sum(
                weight * uv[0] for weight, uv in zip(weights, uvs))
            interpolated_v = sum(
                weight * uv[1] for weight, uv in zip(weights, uvs))
            shade = _nearest_u8(sum(
                weight * corner_shade
                for weight, corner_shade in zip(weights, shades)))
            source_x = _nearest_material_local_texel(
                interpolated_u, native_material.width)
            source_y = _nearest_material_local_texel(
                interpolated_v, native_material.height)
            palette_index = native_material.indices[
                source_y * native_material.width + source_x]
            if palette_index >= len(native_material.palette_words):
                raise PsxNativeRasterError(
                    f"material texel ({source_x}, {source_y}) selects "
                    f"out-of-range CLUT index {palette_index}")

            destination = (
                ((target_y - top) * candidate_width + target_x - left) * 4)
            lookup = (palette_index * 256 + shade) * 4
            output[destination:destination + 4] = modulation_lut[
                lookup:lookup + 4]

    if coverage_count == 0:
        return _empty_patch()

    patch_width = covered_right - covered_left + 1
    patch_height = covered_bottom - covered_top + 1
    if (covered_left, covered_top, patch_width, patch_height) == (
            left, top, candidate_width, candidate_height):
        rgba = bytes(output)
    else:
        cropped = bytearray(patch_width * patch_height * 4)
        for patch_y, target_y in enumerate(
                range(covered_top, covered_bottom + 1)):
            source = (
                ((target_y - top) * candidate_width + covered_left - left)
                * 4)
            destination = patch_y * patch_width * 4
            cropped[destination:destination + patch_width * 4] = output[
                source:source + patch_width * 4]
        rgba = bytes(cropped)

    return PsxNativeRasterPatch(
        origin_x=covered_left,
        origin_y=covered_top,
        width=patch_width,
        height=patch_height,
        rgba=rgba,
        coverage_count=coverage_count,
    )


def _numpy_edge_grid(
        start: tuple[float, float],
        end: tuple[float, float],
        sample_x: object,
        sample_y: object,
        shape: tuple[int, int]) -> object:
    """Evaluate one scalar-ordered edge expression on a NumPy strip."""

    assert _np is not None
    values = ((end[0] - start[0]) * (sample_y - start[1])
              - (end[1] - start[1]) * (sample_x - start[0]))
    return _np.broadcast_to(values, shape)


def _rasterize_numpy(
        *,
        points: list[tuple[float, float]],
        uvs: list[tuple[float, float]],
        shades: list[float],
        native_material: PsxNativeMaterial,
        double_area: float,
        left: int,
        right: int,
        top: int,
        bottom: int) -> PsxNativeRasterPatch:
    """Vectorized backend with scalar-identical edge and rounding order."""

    assert _np is not None
    np = _np
    candidate_width = right - left + 1
    candidate_height = bottom - top + 1
    output = np.zeros(
        (candidate_height, candidate_width, 4), dtype=np.uint8)
    material_indices = np.frombuffer(
        native_material.indices, dtype=np.uint8).reshape(
            native_material.height, native_material.width)
    modulation_lut = np.frombuffer(
        _modulation_rgba_lut(native_material.palette_words),
        dtype=np.uint8).reshape(CLUT_ENTRY_COUNT, 256, 4)
    edge_pairs, top_left = _edge_contract(points)
    sample_x = (
        np.arange(left, right + 1, dtype=np.float64)[None, :] + 0.5)
    strip_rows = max(1, NUMPY_STRIP_PIXELS // candidate_width)
    coverage_count = 0
    covered_left = right + 1
    covered_right = left - 1
    covered_top = bottom + 1
    covered_bottom = top - 1

    for strip_start in range(0, candidate_height, strip_rows):
        strip_stop = min(candidate_height, strip_start + strip_rows)
        row_count = strip_stop - strip_start
        sample_y = (
            np.arange(
                top + strip_start, top + strip_stop,
                dtype=np.float64)[:, None]
            + 0.5)
        shape = (row_count, candidate_width)
        edge_values = tuple(
            _numpy_edge_grid(start, end, sample_x, sample_y, shape)
            for start, end in edge_pairs)
        inside = (
            edge_values[0] >= 0.0
            if top_left[0] else edge_values[0] > 0.0)
        for values, owns_edge in zip(edge_values[1:], top_left[1:]):
            inside &= (
                values >= 0.0 if owns_edge else values > 0.0)

        local_y, local_x = np.nonzero(inside)
        strip_coverage = int(local_y.size)
        if strip_coverage == 0:
            continue
        coverage_count += strip_coverage
        target_x_min = left + int(local_x.min())
        target_x_max = left + int(local_x.max())
        target_y_min = top + strip_start + int(local_y.min())
        target_y_max = top + strip_start + int(local_y.max())
        covered_left = min(covered_left, target_x_min)
        covered_right = max(covered_right, target_x_max)
        covered_top = min(covered_top, target_y_min)
        covered_bottom = max(covered_bottom, target_y_max)

        # Preserve the three scalar divides.  Vector addition supplies the
        # common fast case; exact quantization boundaries are corrected below
        # through Python's runtime ``sum`` implementation.
        weight0 = edge_values[0][local_y, local_x] / double_area
        weight1 = edge_values[1][local_y, local_x] / double_area
        weight2 = edge_values[2][local_y, local_x] / double_area
        interpolated_u = (
            weight0 * uvs[0][0] + weight1 * uvs[1][0]
        ) + weight2 * uvs[2][0]
        interpolated_v = (
            weight0 * uvs[0][1] + weight1 * uvs[1][1]
        ) + weight2 * uvs[2][1]
        interpolated_shade = (
            weight0 * shades[0] + weight1 * shades[1]
        ) + weight2 * shades[2]

        scaled_u = np.clip(
            interpolated_u, 0.0, native_material.width - 1) + 0.5
        scaled_v = np.clip(
            interpolated_v, 0.0, native_material.height - 1) + 0.5
        scaled_shade = interpolated_shade + 0.5
        source_x = np.clip(
            np.floor(scaled_u),
            0, native_material.width - 1).astype(np.intp)
        source_y = np.clip(
            np.floor(scaled_v),
            0, native_material.height - 1).astype(np.intp)
        shade = np.clip(
            np.floor(scaled_shade),
            0, 0xFF).astype(np.intp)

        # Python's compensated three-term float sum and NumPy's vector
        # additions are normally identical after quantization.  At an exact
        # half-up boundary they can differ by one ULP, which is enough to pick
        # a neighboring texel or shade.  Re-evaluate only those rare samples
        # with the scalar expressions so the accelerated backend remains byte
        # identical without sacrificing the vectorized interior.
        boundary = (
            (np.abs(scaled_u - np.rint(scaled_u))
             <= SCALAR_QUANTIZATION_RECHECK_EPSILON)
            | (np.abs(scaled_v - np.rint(scaled_v))
               <= SCALAR_QUANTIZATION_RECHECK_EPSILON)
            | (np.abs(scaled_shade - np.rint(scaled_shade))
               <= SCALAR_QUANTIZATION_RECHECK_EPSILON)
        )
        for sample in np.flatnonzero(boundary):
            position = int(sample)
            target_x = left + int(local_x[position])
            target_y = top + strip_start + int(local_y[position])
            edge_scalar = tuple(
                _edge(start, end, target_x + 0.5, target_y + 0.5)
                for start, end in edge_pairs)
            weights_scalar = tuple(
                value / double_area for value in edge_scalar)
            source_x[position] = _nearest_material_local_texel(
                sum(weight * uv[0]
                    for weight, uv in zip(weights_scalar, uvs)),
                native_material.width)
            source_y[position] = _nearest_material_local_texel(
                sum(weight * uv[1]
                    for weight, uv in zip(weights_scalar, uvs)),
                native_material.height)
            shade[position] = _nearest_u8(sum(
                weight * corner_shade
                for weight, corner_shade in zip(weights_scalar, shades)))
        palette_index = material_indices[source_y, source_x]
        invalid = palette_index >= len(native_material.palette_words)
        if np.any(invalid):
            first = int(np.flatnonzero(invalid)[0])
            raise PsxNativeRasterError(
                f"material texel ({int(source_x[first])}, "
                f"{int(source_y[first])}) selects out-of-range CLUT index "
                f"{int(palette_index[first])}")

        output[strip_start + local_y, local_x] = modulation_lut[
            palette_index, shade]

    if coverage_count == 0:
        return _empty_patch()

    patch_width = covered_right - covered_left + 1
    patch_height = covered_bottom - covered_top + 1
    x_start = covered_left - left
    x_stop = x_start + patch_width
    y_start = covered_top - top
    y_stop = y_start + patch_height
    patch = output[y_start:y_stop, x_start:x_stop]
    rgba = np.ascontiguousarray(patch).tobytes(order="C")
    return PsxNativeRasterPatch(
        origin_x=covered_left,
        origin_y=covered_top,
        width=patch_width,
        height=patch_height,
        rgba=rgba,
        coverage_count=coverage_count,
    )


def rasterize_legacy_psw_triangle(
        target_width: object,
        target_height: object,
        screen_triangle: object,
        authored_uvs: object,
        corner_shades: object,
        material: object) -> PsxNativeRasterPatch:
    """Rasterize one PSW/PSV preview triangle into a clipped RGBA patch.

    Samples are evaluated at target pixel centers.  Vertex order is normalized
    only for edge ownership; UVs and shades follow the same permutation, so
    clockwise and counter-clockwise calls produce identical results.  UV and
    grayscale shade are interpolated affinely.  UVs are direct 0..127
    descriptor-relative material-local coordinates, not 0..255 texture-space
    bytes.  Finite fractional UVs and shades are accepted so a caller can
    preserve attributes created by bounded near-plane or BSP clipping.  UV
    uses nearest native-texel sampling; shade uses deterministic half-up
    conversion to an 8-bit vertex colour before
    :func:`psx_texture_modulate_bgr555` is applied.

    This function composites nothing and deliberately accepts no background,
    descriptor origin, TPage, CLUT-offset, runtime material binding, ABR, GTE,
    ordering, or dither state.
    """

    width = _plain_bounded_int(
        target_width, field="target width", minimum=1,
        maximum=MAX_TARGET_DIMENSION)
    height = _plain_bounded_int(
        target_height, field="target height", minimum=1,
        maximum=MAX_TARGET_DIMENSION)
    points = list(_screen_triangle(screen_triangle))
    uvs = list(_three_uvs(authored_uvs))
    shades = list(_three_shades(corner_shades))
    native_material = _validate_material(material)

    double_area = _edge(points[0], points[1], *points[2])
    if not math.isfinite(double_area) or abs(double_area) <= MIN_DOUBLE_AREA:
        raise PsxNativeRasterError(
            "screen triangle is degenerate or numerically unstable")
    if double_area < 0.0:
        points[1], points[2] = points[2], points[1]
        uvs[1], uvs[2] = uvs[2], uvs[1]
        shades[1], shades[2] = shades[2], shades[1]
        double_area = -double_area

    # Convert the vertex bounds to the only integer samples that can possibly
    # contribute, then clip before measuring or allocating the candidate
    # buffer.  Pixel (x, y) is sampled at (x + 0.5, y + 0.5).
    left = max(0, math.ceil(min(point[0] for point in points) - 0.5))
    right = min(
        width - 1, math.floor(max(point[0] for point in points) - 0.5))
    top = max(0, math.ceil(min(point[1] for point in points) - 0.5))
    bottom = min(
        height - 1, math.floor(max(point[1] for point in points) - 0.5))
    if left > right or top > bottom:
        return _empty_patch()

    candidate_width = right - left + 1
    candidate_height = bottom - top + 1
    candidate_pixels = candidate_width * candidate_height
    if candidate_pixels > MAX_PATCH_PIXELS:
        raise PsxNativeRasterError(
            f"clipped raster candidate contains {candidate_pixels} pixels; "
            f"safe limit is {MAX_PATCH_PIXELS}")

    backend = _rasterize_numpy if _np is not None else _rasterize_python
    return backend(
        points=points,
        uvs=uvs,
        shades=shades,
        native_material=native_material,
        double_area=double_area,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
    )
