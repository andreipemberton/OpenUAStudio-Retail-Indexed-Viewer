import unittest
from types import SimpleNamespace
from unittest.mock import patch

import indexed_renderer
from indexed_renderer import (
    IndexedDistanceFadeProfile,
    IndexedPiece,
    IndexedRasterizer,
    IndexedSurface,
    IndexedTables,
)


def _palette():
    palette = [
        (index, (index * 3) % 256, (index * 7) % 256)
        for index in range(256)
    ]
    palette[0] = (255, 255, 0)  # Authoring chroma; display index zero is black.
    palette[10] = (4, 5, 6)
    palette[11] = (4, 5, 6)  # Distinct indices may intentionally share RGB.
    return tuple(palette)


def _tables(*, shader_changes=None, tracy_changes=None):
    # Default SHADERMP rows preserve the source index.  Default TRACYRMP rows
    # preserve the background index for every raw source texel.
    shader = bytearray(bytes(range(256)) * 256)
    tracy = bytearray(b"".join(
        bytes((background,)) * 256 for background in range(256)))
    for (shade, source), output in (shader_changes or {}).items():
        shader[shade * 256 + source] = output
    for (background, source), output in (tracy_changes or {}).items():
        tracy[background * 256 + source] = output
    return IndexedTables(_palette(), bytes(shader), bytes(tracy))


def _ua_profile_tracy():
    tracy = bytearray(bytes(range(256)) * 256)
    for source in range(256):
        tracy[source * 256] = 0 if source == 13 else source
    changes = {
        (31, 220): 212,
        (220, 31): 200,
        (13, 255): 255,
        (255, 13): 177,
        (156, 185): 54,
        (159, 185): 106,
        (159, 156): 63,
        (156, 159): 140,
        (0, 156): 223,
        (223, 159): 207,
        (0, 159): 255,
        (255, 156): 191,
    }
    for (source, destination), output in changes.items():
        tracy[source * 256 + destination] = output
    return bytes(tracy)


def _ua_profile_shader():
    shader = bytearray(bytes(range(256)) * 256)
    for shade in range(256):
        shader[shade * 256] = 0
    # Retail row 0 is mostly source-preserving but contains authored aliases;
    # source 6 mapping to black is one of the fixed profile tripwires.
    shader[6] = 0
    shader[255 * 256:] = bytes(256)
    return bytes(shader)


def _solid(
        index, *, name=None, shade_mode="none", shade_value=0,
        tracy_mode="none", map_mode="linear"):
    return IndexedSurface(
        name or f"solid-{index}", "solid", None, 0, 0, None, index,
        shade_mode, shade_value, tracy_mode, map_mode,
    )


def _texture(
        pixels, width, height, *, name="texture", chroma=(),
        shade_mode="none", shade_value=0, tracy_mode="none",
        map_mode="linear", authored_shade_value=None,
        authored_depth_fade_flag=False, distance_fade_eligible=False):
    chroma_by_index = [False] * 256
    for index in chroma:
        chroma_by_index[index] = True
    return IndexedSurface(
        name, "texture", bytes(pixels), width, height,
        tuple(chroma_by_index), None, shade_mode, shade_value,
        tracy_mode, map_mode, authored_shade_value,
        authored_depth_fade_flag, distance_fade_eligible,
    )


def _triangle_piece(
        surface, *, face_id="face", polygon_id=1,
        screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        uvs=None, camera=None, source_order=0, sort_depth=0.0):
    if uvs is None:
        uvs = (
            ((0.0, 0.0),) * len(screen)
            if surface.kind == "texture" else ())
    if camera is None:
        camera = ((0.0, 0.0, 0.0),) * len(screen)
    return IndexedPiece(
        face_id, polygon_id, screen, uvs, camera, surface,
        source_order, sort_depth)


def _quad_piece(surface, *, face_id="face", polygon_id=1, size=2):
    screen = (
        (0.0, 0.0), (float(size), 0.0),
        (float(size), float(size)), (0.0, float(size)),
    )
    uvs = (
        ((0.0, 0.0), (256.0, 0.0),
         (256.0, 256.0), (0.0, 256.0))
        if surface.kind == "texture" else ())
    return _triangle_piece(
        surface, face_id=face_id, polygon_id=polygon_id,
        screen=screen, uvs=uvs,
        camera=((0.0, 0.0, 0.0),) * 4,
    )


def _at(grid, y=0, x=0):
    return int(grid[y][x])


class IndexedRendererTests(unittest.TestCase):
    def test_from_decoded_fails_closed_on_wrong_or_transposed_ua_tables(self):
        shader = _ua_profile_shader()
        tracy = _ua_profile_tracy()
        decoded_shader = SimpleNamespace(
            width=256, height=256, pixels=shader)
        decoded_tracy = SimpleNamespace(
            width=256, height=256, pixels=tracy)

        tables = IndexedTables.from_decoded(
            _palette(), decoded_shader, decoded_tracy)
        self.assertEqual(tables.tracy_index(31, 220), 212)
        self.assertIs(tables.validate_ua_profile(), tables)

        wrong = bytearray(tracy)
        wrong[31 * 256 + 220] = 0
        with self.assertRaisesRegex(ValueError, r"\[31\]\[220\]"):
            IndexedTables.from_decoded(_palette(), shader, wrong)

        transposed = bytes(
            tracy[column * 256 + row]
            for row in range(256)
            for column in range(256)
        )
        with self.assertRaisesRegex(ValueError, "UA profile"):
            IndexedTables.from_decoded(_palette(), shader, transposed)

        transposed_shader = bytes(
            shader[column * 256 + row]
            for row in range(256)
            for column in range(256)
        )
        with self.assertRaisesRegex(ValueError, "SHADERMP UA profile"):
            IndexedTables.from_decoded(
                _palette(), transposed_shader, tracy)

        with self.assertRaisesRegex(ValueError, "SHADERMP UA profile"):
            IndexedTables.from_decoded(
                _palette(), bytes(256 * 256), tracy)

        bad_dimensions = SimpleNamespace(
            width=255, height=256, pixels=shader[:-256])
        with self.assertRaisesRegex(ValueError, "256x256"):
            IndexedTables.from_decoded(
                _palette(), bad_dimensions, decoded_tracy)

    def test_linear_sampling_preserves_texture_y_x_axis_and_nearest_indices(self):
        tables = _tables()
        surface = _texture((1, 2, 3, 4), 2, 2)
        result = IndexedRasterizer.render(
            2, 2, [_quad_piece(surface)], tables)

        self.assertEqual(
            [[_at(result.indices, y, x) for x in range(2)]
             for y in range(2)],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(result.stats["linear_mapped_triangles"], 2)
        self.assertEqual(result.stats["covered_pixels"], 4)

    def test_depth_mapping_is_reciprocal_depth_not_affine(self):
        tables = _tables()
        screen = ((0.0, 0.0), (4.0, 0.0), (0.0, 4.0))
        uvs = ((0.0, 0.0), (256.0, 0.0), (0.0, 0.0))
        camera = ((0.0, 0.0, 3.0),
                  (0.0, 0.0, 0.0),
                  (0.0, 0.0, 0.0))
        linear = _texture((1, 2, 3, 4), 4, 1, map_mode="linear")
        depth = _texture(
            (1, 2, 3, 4), 4, 1, name="depth", map_mode="depth")

        linear_result = IndexedRasterizer.render(
            4, 4, [_triangle_piece(
                linear, screen=screen, uvs=uvs, camera=camera)], tables)
        depth_result = IndexedRasterizer.render(
            4, 4, [_triangle_piece(
                depth, screen=screen, uvs=uvs, camera=camera)], tables)

        # At centre (1.5, .5), affine u=96 samples texel 1; reciprocal-depth
        # u=38.4 samples texel 0.
        self.assertEqual(_at(linear_result.indices, 0, 1), 2)
        self.assertEqual(_at(depth_result.indices, 0, 1), 1)
        self.assertGreater(depth_result.stats["depth_mapped_samples"], 0)

    def test_lookup_axes_and_input_piece_order_are_stable(self):
        tables = _tables(
            shader_changes={(3, 7): 88, (7, 3): 99},
            tracy_changes={
                (20, 31): 44,
                (31, 20): 55,
                (0, 31): 33,
            },
        )
        shaded = _texture(
            (7,), 1, 1, shade_mode="gradient", shade_value=3,
            tracy_mode="clear")
        shade_result = IndexedRasterizer.render(
            1, 1, [_triangle_piece(shaded)], tables)
        self.assertEqual(_at(shade_result.indices), 88)

        base = _triangle_piece(_solid(20), face_id="base", polygon_id=20)
        effect = _triangle_piece(
            _solid(31, tracy_mode="flat"),
            face_id="effect", polygon_id=31)
        forward = IndexedRasterizer.render(1, 1, [base, effect], tables)
        reverse = IndexedRasterizer.render(1, 1, [effect, base], tables)
        self.assertEqual(_at(forward.indices), 44)
        self.assertEqual(_at(reverse.indices), 20)
        self.assertEqual(
            forward.stats["tracy_lookup_axis_order"],
            "TRACYRMP[background][raw_source]",
        )

    def test_flat_tracy_is_sensitive_to_destination_index_not_display_rgb(self):
        tables = _tables(tracy_changes={(10, 5): 70, (11, 5): 71})
        self.assertEqual(tables.palette[10], tables.palette[11])
        effect = _triangle_piece(
            _solid(5, tracy_mode="flat"),
            face_id="effect", polygon_id=5)

        over_ten = IndexedRasterizer.render(
            1, 1,
            [_triangle_piece(_solid(10), face_id="ten"), effect], tables)
        over_eleven = IndexedRasterizer.render(
            1, 1,
            [_triangle_piece(_solid(11), face_id="eleven"), effect], tables)
        self.assertEqual(_at(over_ten.indices), 70)
        self.assertEqual(_at(over_eleven.indices), 71)

    def test_forced_flat_destination_uses_one_diagnostic_row_per_layer(self):
        tables = _tables(tracy_changes={
            (10, 5): 20,
            (20, 6): 30,
            (13, 5): 70,
            (13, 6): 80,
        })
        pieces = [
            _triangle_piece(_solid(10), face_id="opaque"),
            _triangle_piece(
                _solid(5, tracy_mode="flat"), face_id="flat-five",
                sort_depth=2.0),
            _triangle_piece(
                _solid(6, tracy_mode="flat"), face_id="flat-six",
                sort_depth=1.0),
        ]

        live = IndexedRasterizer.render(1, 1, pieces, tables)
        forced = IndexedRasterizer.render(
            1, 1, pieces, tables,
            flat_tracy_destination_override=13)

        self.assertEqual(_at(live.indices), 30)
        self.assertEqual(_at(forced.indices), 80)
        self.assertTrue(live.stats["canonical_retail_destination_policy"])
        self.assertFalse(
            forced.stats["canonical_retail_destination_policy"])
        self.assertFalse(
            forced.stats["source_faithful_flat_destination_reads"])
        self.assertEqual(
            forced.stats["flat_tracy_destination_mode"],
            "forced_diagnostic")
        self.assertEqual(
            forced.stats["flat_tracy_forced_destination_index"], 13)
        self.assertEqual(forced.stats["initial_framebuffer_index"], 0)

    def test_forced_flat_coverage_compares_with_actual_framebuffer(self):
        tables = _tables(tracy_changes={(13, 5): 0})
        effect = _triangle_piece(
            _solid(5, tracy_mode="flat"), face_id="effect")

        result = IndexedRasterizer.render(
            1, 1, [effect], tables,
            flat_tracy_destination_override=13)

        self.assertEqual(_at(result.indices), 0)
        self.assertFalse(bool(result.coverage[0][0]))
        self.assertEqual(result.stats["flat_tracy_changed_samples"], 0)

    def test_forced_flat_destination_does_not_change_clear_tracy(self):
        tables = _tables(shader_changes={(2, 7): 99})
        pieces = [
            _triangle_piece(_solid(40), face_id="base"),
            _triangle_piece(
                _texture(
                    (7,), 1, 1, shade_mode="gradient", shade_value=2,
                    tracy_mode="clear"),
                face_id="clear"),
        ]

        live = IndexedRasterizer.render(1, 1, pieces, tables)
        forced = IndexedRasterizer.render(
            1, 1, pieces, tables,
            flat_tracy_destination_override=13)

        self.assertEqual(_at(live.indices), 99)
        self.assertEqual(_at(forced.indices), 99)
        self.assertEqual(forced.stats["flat_tracy_samples"], 0)

    def test_forced_flat_destination_validates_unsigned_palette_index(self):
        tables = _tables()
        for value in (-1, 256):
            with self.subTest(value=value), self.assertRaises(ValueError):
                IndexedRasterizer.render(
                    1, 1, [], tables,
                    flat_tracy_destination_override=value)
        for value in (True, 1.5, "13"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                IndexedRasterizer.render(
                    1, 1, [], tables,
                    flat_tracy_destination_override=value)

    def test_forced_flat_numpy_and_python_backends_match(self):
        tables = _tables(tracy_changes={
            (13, 5): 70,
            (13, 6): 80,
        })
        pieces = [
            _triangle_piece(_solid(10), face_id="opaque"),
            _triangle_piece(
                _solid(5, tracy_mode="flat"), face_id="flat-five",
                sort_depth=2.0),
            _triangle_piece(
                _solid(6, tracy_mode="flat"), face_id="flat-six",
                sort_depth=1.0),
        ]

        accelerated = IndexedRasterizer.render(
            1, 1, pieces, tables,
            flat_tracy_destination_override=13)
        with patch.object(indexed_renderer, "_np", None):
            fallback = IndexedRasterizer.render(
                1, 1, pieces, tables,
                flat_tracy_destination_override=13)

        self.assertEqual(_at(accelerated.indices), _at(fallback.indices))
        self.assertEqual(
            bool(accelerated.coverage[0][0]),
            bool(fallback.coverage[0][0]))
        for key in (
                "flat_tracy_samples", "flat_tracy_changed_samples",
                "flat_tracy_destination_mode",
                "flat_tracy_forced_destination_index",
                "index_buffer_sha256"):
            self.assertEqual(accelerated.stats[key], fallback.stats[key])
    def test_flat_tracy_uses_retail_background_source_axis_and_depth_order(self):
        tables = _tables(tracy_changes={
            (0, 156): 223,
            (223, 159): 207,
            (0, 159): 255,
            (255, 156): 191,
        })
        source_156 = _triangle_piece(
            _solid(156, tracy_mode="flat"), face_id="source-156",
            sort_depth=2.0)
        source_159 = _triangle_piece(
            _solid(159, tracy_mode="flat"), face_id="source-159",
            sort_depth=1.0)

        forward = IndexedRasterizer.render(
            1, 1, [source_156, source_159], tables)
        reverse = IndexedRasterizer.render(
            1, 1, [
                _triangle_piece(
                    _solid(156, tracy_mode="flat"), face_id="source-156",
                    sort_depth=1.0),
                _triangle_piece(
                    _solid(159, tracy_mode="flat"), face_id="source-159",
                    sort_depth=2.0),
            ], tables)

        self.assertEqual(_at(forward.indices), 207)
        self.assertEqual(_at(reverse.indices), 191)

    def test_flat_tracy_uses_raw_source_without_shade_or_chroma_rejection(self):
        tables = _tables(
            shader_changes={(2, 0): 22},
            tracy_changes={(7, 0): 99, (7, 22): 55},
        )
        base = _triangle_piece(_solid(7), face_id="base", polygon_id=7)
        chroma_effect = _texture(
            (0,), 1, 1, chroma=(0,), shade_mode="gradient",
            shade_value=2, tracy_mode="flat")
        result = IndexedRasterizer.render(
            1, 1,
            [base, _triangle_piece(
                chroma_effect, face_id="chroma", polygon_id=8)],
            tables,
        )

        self.assertEqual(_at(result.indices), 99)
        self.assertTrue(bool(result.coverage[0][0]))
        self.assertEqual(_at(result.polygon_owner), 7)
        self.assertEqual(result.stats["source_chroma_skipped"], 0)
        self.assertEqual(result.stats["flat_tracy_samples"], 1)
        self.assertEqual(result.stats["flat_tracy_source_zero_samples"], 1)

    def test_flat_tracy_table_noop_does_not_create_background_coverage(self):
        tables = _tables()
        noop = _triangle_piece(
            _solid(0, tracy_mode="flat"), face_id="source-zero")
        result = IndexedRasterizer.render(1, 1, [noop], tables)

        self.assertEqual(_at(result.indices), 0)
        self.assertFalse(bool(result.coverage[0][0]))
        self.assertEqual(result.stats["flat_tracy_source_zero_samples"], 1)
        self.assertEqual(result.stats["flat_tracy_changed_samples"], 0)

    def test_flat_source_zero_can_change_background_thirteen_without_erasing_coverage(self):
        tables = _tables(tracy_changes={(13, 0): 0})
        base = _triangle_piece(_solid(13), face_id="base", polygon_id=13)
        effect = _triangle_piece(
            _solid(0, tracy_mode="flat"), face_id="effect",
            polygon_id=0, sort_depth=1.0)
        result = IndexedRasterizer.render(1, 1, [base, effect], tables)

        self.assertEqual(_at(result.indices), 0)
        self.assertTrue(bool(result.coverage[0][0]))
        self.assertEqual(_at(result.polygon_owner), 13)
        self.assertEqual(result.stats["flat_tracy_changed_samples"], 1)

    def test_flat_tracy_deduplicates_bsp_fragments_per_source_face(self):
        changes = {(background, 5): (background + 1) % 256
                   for background in range(256)}
        tables = _tables(tracy_changes=changes)
        effect = _solid(5, tracy_mode="flat")
        first = _triangle_piece(effect, face_id="same", polygon_id=5)
        duplicate_fragment = _triangle_piece(
            effect, face_id="same", polygon_id=5)
        other_face = _triangle_piece(
            effect, face_id="other", polygon_id=6)

        deduplicated = IndexedRasterizer.render(
            1, 1, [first, duplicate_fragment], tables)
        layered = IndexedRasterizer.render(
            1, 1, [first, other_face], tables)
        self.assertEqual(_at(deduplicated.indices), 1)
        self.assertEqual(_at(layered.indices), 2)
        self.assertEqual(deduplicated.stats["flat_tracy_samples"], 1)
        self.assertEqual(
            deduplicated.stats["flat_tracy_duplicate_samples_skipped"], 1)

    def test_reversed_winding_distinct_flat_faces_both_layer(self):
        changes = {(background, 5): (background + 1) % 256
                   for background in range(256)}
        tables = _tables(tracy_changes=changes)
        effect = _solid(5, tracy_mode="flat")
        forward = _triangle_piece(
            effect, face_id="front", polygon_id=77, sort_depth=2.0)
        reverse_winding = _triangle_piece(
            effect, face_id="back", polygon_id=77,
            screen=((0.0, 0.0), (0.0, 1.0), (1.0, 0.0)),
            sort_depth=1.0)
        duplicate_fragment = _triangle_piece(
            effect, face_id="front", polygon_id=77,
            screen=((0.0, 0.0), (0.0, 1.0), (1.0, 0.0)),
            sort_depth=1.0)

        distinct = IndexedRasterizer.render(
            1, 1, [forward, reverse_winding], tables)
        duplicate = IndexedRasterizer.render(
            1, 1, [forward, duplicate_fragment], tables)
        self.assertEqual(_at(distinct.indices), 2)
        self.assertEqual(_at(duplicate.indices), 1)

    def test_opaque_writes_zero_but_clear_skips_only_numeric_zero(self):
        tables = _tables(shader_changes={(2, 0): 99, (2, 7): 88})
        opaque_zero = _texture(
            (0,), 1, 1, chroma=(0,), shade_mode="gradient",
            shade_value=2, tracy_mode="none")
        clear_zero = _texture(
            (0,), 1, 1, chroma=(0,), shade_mode="gradient",
            shade_value=2, tracy_mode="clear")
        clear_palette_chroma = _texture(
            (7,), 1, 1, chroma=(7,), shade_mode="gradient",
            shade_value=2, tracy_mode="clear")

        opaque = IndexedRasterizer.render(
            1, 1, [_triangle_piece(opaque_zero)], tables)
        skipped = IndexedRasterizer.render(
            1, 1, [
                _triangle_piece(_solid(40), face_id="base"),
                _triangle_piece(clear_zero, face_id="clear-zero"),
            ], tables)
        nonzero = IndexedRasterizer.render(
            1, 1, [
                _triangle_piece(_solid(40), face_id="base"),
                _triangle_piece(
                    clear_palette_chroma, face_id="clear-seven"),
            ], tables)

        self.assertEqual(_at(opaque.indices), 99)
        self.assertTrue(bool(opaque.coverage[0][0]))
        self.assertEqual(_at(skipped.indices), 40)
        self.assertEqual(skipped.stats["clear_source_zero_skipped"], 1)
        self.assertEqual(_at(nonzero.indices), 88)

    def test_numpy_and_python_match_for_deferred_transparency_and_occlusion(self):
        tables = _tables(tracy_changes={(10, 6): 20})
        pieces = [
            _triangle_piece(
                _solid(5, tracy_mode="flat"), face_id="behind-flat",
                sort_depth=3.0),
            _triangle_piece(_solid(10), face_id="front-opaque"),
            _triangle_piece(
                _solid(6, tracy_mode="flat"), face_id="front-flat",
                sort_depth=2.0),
            _triangle_piece(
                _solid(7, tracy_mode="clear"), face_id="front-clear",
                sort_depth=1.0),
        ]

        accelerated = IndexedRasterizer.render(1, 1, pieces, tables)
        with patch.object(indexed_renderer, "_np", None):
            fallback = IndexedRasterizer.render(1, 1, pieces, tables)

        self.assertEqual(_at(accelerated.indices), _at(fallback.indices))
        self.assertEqual(_at(accelerated.indices), 7)
        self.assertEqual(_at(accelerated.polygon_owner), 1)
        self.assertEqual(accelerated.stats["transparent_samples_occluded"], 1)
        self.assertEqual(accelerated.stats["flat_tracy_samples"], 1)
        self.assertEqual(
            [entry["tracy_mode"] for entry in
             accelerated.stats["transparent_replay_faces"]],
            ["flat", "flat", "clear"],
        )
        self.assertEqual(
            bool(accelerated.coverage[0][0]), bool(fallback.coverage[0][0]))
        for key in (
                "flat_tracy_samples", "flat_tracy_changed_samples",
                "transparent_samples_occluded", "opaque_or_clear_samples"):
            self.assertEqual(accelerated.stats[key], fallback.stats[key])

    def test_clear_tracy_shades_then_overwrites_destination(self):
        tables = _tables(shader_changes={(2, 7): 99})
        base = _triangle_piece(_solid(40), face_id="base", polygon_id=40)
        clear = _texture(
            (7,), 1, 1, shade_mode="gradient", shade_value=2,
            tracy_mode="clear")
        result = IndexedRasterizer.render(
            1, 1,
            [base, _triangle_piece(
                clear, face_id="clear", polygon_id=41)],
            tables,
        )

        self.assertEqual(_at(result.indices), 99)
        self.assertEqual(_at(result.polygon_owner), 41)
        self.assertTrue(bool(result.coverage[0][0]))
        self.assertEqual(result.stats["opaque_or_clear_samples"], 2)

    def test_shade_none_bypasses_shadermp(self):
        tables = _tables(shader_changes={(2, 7): 99})
        unshaded = _texture(
            (7,), 1, 1, shade_mode="none", shade_value=2)
        result = IndexedRasterizer.render(
            1, 1, [_triangle_piece(unshaded)], tables)
        self.assertEqual(_at(result.indices), 7)

    def test_distance_fade_profile_is_validated_and_derives_retail_start(self):
        profile = IndexedDistanceFadeProfile(enabled=True)

        self.assertEqual(profile.profile_name, "retail_gameplay_near_1400_600")
        self.assertEqual(profile.fade_start, 800.0)
        self.assertEqual(profile.to_metadata()["source_distance_scale"], 1.0)

        for field_name, value, exception in (
                ("enabled", 1, TypeError),
                ("visibility_limit", 0, ValueError),
                ("visibility_limit", float("inf"), ValueError),
                ("fade_length", -1, ValueError),
                ("source_distance_scale", 0, ValueError),
                ("source_distance_scale", True, TypeError)):
            with self.subTest(field=field_name, value=value):
                arguments = {field_name: value}
                with self.assertRaises(exception):
                    IndexedDistanceFadeProfile(**arguments)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            IndexedDistanceFadeProfile(
                visibility_limit=500, fade_length=600)
        with self.assertRaisesRegex(TypeError, "IndexedDistanceFadeProfile"):
            IndexedRasterizer.render(
                1, 1, [], _tables(), distance_fade_profile={})

    def test_disabled_distance_fade_preserves_exact_constant_output(self):
        tables = _tables(shader_changes={(85, 7): 99})
        surface = _texture(
            (7,), 1, 1,
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        piece = _triangle_piece(surface)

        original = IndexedRasterizer.render(1, 1, [piece], tables)
        explicit_off = IndexedRasterizer.render(
            1, 1, [piece], tables,
            distance_fade_profile=IndexedDistanceFadeProfile(
                enabled=False, source_distance_scale=250.0))

        self.assertEqual(_at(original.indices), 7)
        self.assertEqual(
            original.stats["index_buffer_sha256"],
            explicit_off.stats["index_buffer_sha256"],
        )
        self.assertFalse(explicit_off.stats["distance_fade_requested"])
        self.assertFalse(explicit_off.stats["distance_fade_effective"])
        self.assertEqual(
            explicit_off.stats["distance_fade_authored_flagged_piece_count"],
            1,
        )

    def test_distance_fade_uses_source_radial_distance_and_fixed_shade_row(self):
        # Normalized radial eye distance is 4.  At source scale 250 this is
        # 1000 units: b=(1000-800)/600=1/3, whose recovered fixed row is 85.
        tables = _tables(shader_changes={(85, 7): 99})
        surface = _texture(
            (7,), 1, 1,
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        piece = _triangle_piece(surface)
        profile = IndexedDistanceFadeProfile(
            enabled=True, source_distance_scale=250.0)

        accelerated = IndexedRasterizer.render(
            1, 1, [piece], tables, distance_fade_profile=profile)
        with patch.object(indexed_renderer, "_np", None):
            fallback = IndexedRasterizer.render(
                1, 1, [piece], tables, distance_fade_profile=profile)

        self.assertEqual(_at(accelerated.indices), 99)
        self.assertEqual(_at(fallback.indices), 99)
        self.assertTrue(accelerated.stats["distance_fade_requested"])
        self.assertTrue(accelerated.stats["distance_fade_effective"])
        self.assertEqual(
            accelerated.stats["distance_fade_profile"]["fade_start"], 800.0)
        self.assertEqual(
            accelerated.stats["distance_fade_dynamic_piece_count"], 1)
        self.assertEqual(accelerated.stats["distance_fade_samples"], 1)
        self.assertEqual(
            accelerated.stats["distance_fade_dynamic_changed_samples"], 1)
        self.assertEqual(accelerated.stats["distance_fade_changed_samples"], 1)
        self.assertEqual(
            accelerated.stats["index_buffer_sha256"],
            fallback.stats["index_buffer_sha256"],
        )

    def test_distance_fade_interpolates_vertex_fixed_brightness_in_screen_space(self):
        tables = _tables(shader_changes={(33, 7): 77, (32, 7): 66})
        surface = _texture(
            (7,), 1, 1,
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        piece = _triangle_piece(
            surface,
            screen=((0.0, 0.0), (2.0, 0.0), (0.0, 2.0)),
            # At scale 200 these radial distances are 800, 1100, 800.
            camera=((0.0, 0.0, 0.0),
                    (0.0, 0.0, -1.5),
                    (0.0, 0.0, 0.0)),
        )
        profile = IndexedDistanceFadeProfile(
            enabled=True, source_distance_scale=200.0)

        accelerated = IndexedRasterizer.render(
            2, 2, [piece], tables, distance_fade_profile=profile)
        with patch.object(indexed_renderer, "_np", None):
            fallback = IndexedRasterizer.render(
                2, 2, [piece], tables, distance_fade_profile=profile)

        # At pixel centre (.5,.5), screen weights are .5,.25,.25.  Interpolated
        # fixed brightness is 8480, selecting high-byte row 33.
        self.assertEqual(_at(accelerated.indices, 0, 0), 77)
        self.assertEqual(_at(fallback.indices, 0, 0), 77)
        self.assertEqual(
            accelerated.stats["index_buffer_sha256"],
            fallback.stats["index_buffer_sha256"],
        )

    def test_distance_fade_full_black_uses_opaque_zerospan_before_clear_skip(self):
        surface = _texture(
            (0,), 1, 1, tracy_mode="clear",
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        piece = _triangle_piece(surface)
        disabled = IndexedRasterizer.render(1, 1, [piece], _tables())
        enabled = IndexedRasterizer.render(
            1, 1, [piece], _tables(),
            distance_fade_profile=IndexedDistanceFadeProfile(
                enabled=True, source_distance_scale=350.0))

        self.assertFalse(bool(disabled.coverage[0][0]))
        self.assertTrue(bool(enabled.coverage[0][0]))
        self.assertEqual(_at(enabled.indices), 0)
        self.assertEqual(enabled.stats["distance_fade_black_piece_count"], 1)
        self.assertEqual(enabled.stats["distance_fade_samples"], 1)
        self.assertEqual(enabled.stats["transparent_replay_faces"], [])

    def test_distance_fade_all_white_shortcut_bypasses_shadermp(self):
        tables = _tables(shader_changes={(1, 7): 99})
        surface = _texture(
            (7,), 1, 1, shade_mode="gradient", shade_value=1,
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        result = IndexedRasterizer.render(
            1, 1, [_triangle_piece(surface)], tables,
            # Radial source distance 800 is exactly fade_start, so b=0.
            distance_fade_profile=IndexedDistanceFadeProfile(
                enabled=True, source_distance_scale=200.0))

        self.assertEqual(_at(result.indices), 7)
        self.assertEqual(result.stats["distance_fade_bypass_piece_count"], 1)
        self.assertEqual(result.stats["distance_fade_samples"], 0)
        self.assertTrue(result.stats["distance_fade_effective"])

    def test_distance_fade_shortcuts_are_classified_per_source_face(self):
        surface = _texture(
            (7,), 1, 1, shade_mode="none", tracy_mode="clear",
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        near = _triangle_piece(
            surface, face_id="split-face", polygon_id=20,
            screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            camera=((0.0, 0.0, 0.0),) * 3,
            sort_depth=1.0,
        )
        far = _triangle_piece(
            surface, face_id="split-face", polygon_id=20,
            screen=((1.0, 0.0), (2.0, 0.0), (1.0, 1.0)),
            camera=((0.0, 0.0, -1396.0),) * 3,
            sort_depth=2.0,
        )
        result = IndexedRasterizer.render(
            2, 1, [near, far], _tables(),
            distance_fade_profile=IndexedDistanceFadeProfile(enabled=True))

        # Retail classifies the complete source polygon as mixed. Neither BSP
        # fragment may independently become an unshaded or opaque-black face.
        self.assertEqual(result.stats["distance_fade_bypass_piece_count"], 0)
        self.assertEqual(result.stats["distance_fade_black_piece_count"], 0)
        self.assertEqual(result.stats["distance_fade_dynamic_piece_count"], 2)
        self.assertEqual(result.stats["distance_fade_effective_piece_count"], 2)
        self.assertEqual(
            result.stats["distance_fade_whole_polygon_scope"].split(";")[0],
            "all BSP fragments sharing one source_face_id are classified "
            "together",
        )

    def test_supplied_clipped_vertex_brightness_is_not_recomputed_at_bsp(self):
        surface = _texture(
            (7,), 1, 1, shade_mode="gradient", shade_value=1,
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        # Camera vertices alone would classify this as all-black. The supplied
        # channel represents brightness evaluated before a viewer-only BSP
        # intersection and must remain the authority.
        piece = IndexedPiece(
            "bsp-face", 30,
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            ((0.0, 0.0),) * 3,
            ((0.0, 0.0, -1396.0),) * 3,
            surface,
            vertex_brightness=(0.0, 0.5, 1.0),
            vertex_shade_fixed=(384.0, 32768.0, 65152.0),
            vertex_distance_fade_signature=(1400.0, 600.0, 1.0),
        )
        profile = IndexedDistanceFadeProfile(enabled=True)
        accelerated = IndexedRasterizer.render(
            1, 1, [piece], _tables(), distance_fade_profile=profile)
        with patch.object(indexed_renderer, "_np", None):
            fallback = IndexedRasterizer.render(
                1, 1, [piece], _tables(), distance_fade_profile=profile)

        self.assertEqual(
            accelerated.stats["distance_fade_dynamic_piece_count"], 1)
        self.assertEqual(accelerated.stats["distance_fade_black_piece_count"], 0)
        self.assertEqual(
            accelerated.stats["distance_fade_vertex_channel_piece_count"], 1)
        self.assertEqual(
            accelerated.stats["index_buffer_sha256"],
            fallback.stats["index_buffer_sha256"])

        with self.assertRaisesRegex(ValueError, "do not match"):
            IndexedRasterizer.render(
                1, 1, [piece], _tables(),
                distance_fade_profile=IndexedDistanceFadeProfile(
                    enabled=True, visibility_limit=2100.0,
                    fade_length=900.0))

        with self.assertRaisesRegex(ValueError, "source range"):
            IndexedPiece(
                "bad-fixed", 31,
                ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
                ((0.0, 0.0),) * 3,
                ((0.0, 0.0, 0.0),) * 3,
                surface,
                vertex_brightness=(0.0, 0.5, 1.0),
                vertex_shade_fixed=(-1.0, 32768.0, 65153.0),
                vertex_distance_fade_signature=(1400.0, 600.0, 1.0),
            )

    def test_distance_fade_never_applies_to_flat_tracy(self):
        surface = _texture(
            (7,), 1, 1, tracy_mode="flat",
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=False,
        )
        result = IndexedRasterizer.render(
            1, 1, [_triangle_piece(surface)], _tables(),
            distance_fade_profile=IndexedDistanceFadeProfile(
                enabled=True, source_distance_scale=350.0))

        self.assertEqual(result.stats["distance_fade_eligible_piece_count"], 0)
        self.assertFalse(result.stats["distance_fade_effective"])
        self.assertEqual(result.stats["flat_tracy_samples"], 1)
        with self.assertRaisesRegex(ValueError, "flat TRACY"):
            _texture(
                (7,), 1, 1, tracy_mode="flat",
                authored_shade_value=0,
                authored_depth_fade_flag=True,
                distance_fade_eligible=True,
            )
        for invalid_shade_mode in ("flat", "line"):
            with self.subTest(shade_mode=invalid_shade_mode):
                with self.assertRaisesRegex(ValueError, "gradient shade"):
                    _texture(
                        (7,), 1, 1, shade_mode=invalid_shade_mode,
                        authored_shade_value=3,
                        authored_depth_fade_flag=True,
                        distance_fade_eligible=True,
                    )

    def test_coverage_keeps_mapped_index_zero_opaque_and_background_clear(self):
        tables = _tables(shader_changes={(2, 7): 0})
        mapped_zero = _solid(
            7, shade_mode="gradient", shade_value=2,
            tracy_mode="clear")
        result = IndexedRasterizer.render(
            2, 1, [_triangle_piece(mapped_zero)], tables)

        self.assertEqual(_at(result.indices, 0, 0), 0)
        self.assertEqual(_at(result.indices, 0, 1), 0)
        self.assertTrue(bool(result.coverage[0][0]))
        self.assertFalse(bool(result.coverage[0][1]))
        self.assertEqual(result.stats["index_zero_pixels"], 2)
        self.assertEqual(result.stats["covered_index_zero_pixels"], 1)

        rgba = result.to_rgba(tables, transparent_background=True)
        self.assertEqual(rgba[:4], bytes((0, 0, 0, 255)))
        self.assertEqual(rgba[4:8], bytes((0, 0, 0, 0)))
        opaque = result.to_rgba(tables, transparent_background=False)
        self.assertEqual(opaque[4:8], bytes((0, 0, 0, 255)))

    def test_pure_python_fallback_import_contract_and_render(self):
        # Construction and rendering cannot rely on ndarray-only behaviour.
        with patch.object(indexed_renderer, "_np", None):
            tables = _tables()
            piece = _triangle_piece(_solid(23), polygon_id=23)
            result = IndexedRasterizer.render(1, 1, [piece], tables)
            self.assertIsInstance(result.indices, list)
            self.assertEqual(result.indices, [[23]])
            self.assertEqual(
                result.to_rgba(tables),
                bytes((*tables.display_palette[23], 255)),
            )


if __name__ == "__main__":
    unittest.main()
