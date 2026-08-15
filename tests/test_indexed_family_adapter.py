from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_parser import AmeshBlock, AttsEntry, BaseObject, TextureRef
from ilbm_parser import IlbmImage
from indexed_family_adapter import (
    IndexedFamilyAdapter,
    UnsupportedIndexedMaterialError,
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    padding = b"\0" if len(payload) & 1 else b""
    return tag + struct.pack(">I", len(payload)) + payload + padding


def _form(kind: bytes, chunks: bytes) -> bytes:
    payload = kind + chunks
    return b"FORM" + struct.pack(">I", len(payload)) + payload


def _write_palette(path: Path, palette: list[tuple[int, int, int]]) -> None:
    payload = bytes(channel for color in palette for channel in color)
    path.write_bytes(_form(b"ILBM", _chunk(b"CMAP", payload)))


def _write_vbmp(path: Path, pixels: bytes, width: int = 256,
                height: int = 256) -> None:
    chunks = _chunk(b"HEAD", struct.pack(">HHH", width, height, 0))
    chunks += _chunk(b"BODY", pixels)
    path.write_bytes(_form(b"VBMP", chunks))


def _identity_table() -> bytes:
    return bytes(range(256)) * 256


def _ua_profile_shader_table() -> bytes:
    table = bytearray(_identity_table())
    for shade in range(256):
        table[shade * 256] = 0
    table[6] = 0
    table[255 * 256:] = bytes(256)
    return bytes(table)


def _ua_profile_tracy_table() -> bytes:
    """Synthetic table satisfying the backend's fixed UA orientation gates."""

    table = bytearray(_identity_table())
    for source in range(256):
        table[source * 256] = 0 if source == 13 else source
    for (source, destination), output in {
        (31, 220): 212,
        (220, 31): 200,
        (13, 255): 255,
        (255, 13): 177,
        (156, 185): 54,
        (159, 185): 106,
        (159, 156): 63,
        (156, 159): 140,
    }.items():
        table[source * 256 + destination] = output
    return bytes(table)


def _make_set_sources(
        root: Path,
        palette: list[tuple[int, int, int]] | None = None,
        shader: bytes | None = None,
        tracy: bytes | None = None) -> tuple[Path, Path, Path, list[tuple[int, int, int]]]:
    palette = list(palette or [(index, index, index) for index in range(256)])
    palette_dir = root / "PALETTE"
    remap_dir = root / "REMAP"
    palette_dir.mkdir(parents=True)
    remap_dir.mkdir(parents=True)
    palette_path = palette_dir / "STANDARD.PAL"
    shader_path = remap_dir / "SHADERMP.ILB"
    tracy_path = remap_dir / "TRACYRMP.ILB"
    _write_palette(palette_path, palette)
    _write_vbmp(shader_path, shader or _ua_profile_shader_table())
    _write_vbmp(tracy_path, tracy or _ua_profile_tracy_table())
    return palette_path, shader_path, tracy_path, palette


def _group_for(block: AmeshBlock, index: int) -> MaterialGroup:
    texture = block.texture
    name = texture.name if texture is not None else ""
    kind = texture.kind if texture is not None else ""
    return MaterialGroup(
        label=name or f"solid #{index}",
        texture_name=name,
        kind=kind,
        block=block,
    )


def _make_family(
        palette_path: Path,
        palette: list[tuple[int, int, int]],
        blocks: list[AmeshBlock],
        textures: dict[str, IlbmImage] | None = None,
        search_roots: list[Path] | None = None,
        effect_overrides: dict[str, Path] | None = None) -> AssetFamily:
    base_object = BaseObject(name="synthetic", ades=list(blocks))
    family_object = FamilyObject(
        base_object=base_object,
        materials=[_group_for(block, index) for index, block in enumerate(blocks)],
        owner_path="root",
    )
    return AssetFamily(
        root_object=family_object,
        textures=dict(textures or {}),
        external_palette=list(palette),
        external_palette_path=palette_path,
        search_roots=[str(path) for path in (search_roots or [palette_path.parent.parent])],
        effect_override_paths=dict(effect_overrides or {}),
    )


def _face(block_index: int, poly_id: int, shade: int | None = 0):
    return SimpleNamespace(
        owner="root", block_index=block_index, poly_id=poly_id, shade=shade
    )


def _material(
        label: str,
        names=(),
        frames=(),
        uv_groups=()):
    return SimpleNamespace(
        label=label,
        anim_bitmap_names=list(names),
        anim_frames=list(frames),
        anim_uv_groups=[list(group) for group in uv_groups],
    )


def _surface_bytes(surface) -> bytes:
    values = surface.indices
    if isinstance(values, bytes):
        return values
    if hasattr(values, "tobytes"):
        return values.tobytes()
    return bytes(values)


class IndexedFamilySourceTests(unittest.TestCase):
    def test_equivalent_duplicate_set_profiles_are_audited_and_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first" / "Set1"
            second = root / "second" / "Set1"
            palette_path, _shader, _tracy, palette = _make_set_sources(first)
            second_palette, _shader2, _tracy2, _ = _make_set_sources(second)
            family = _make_family(
                palette_path, palette, [], search_roots=[first, second])
            family.external_palette_status = "ambiguous"
            family.external_palette_candidates = [
                str(palette_path), str(second_palette), str(second_palette)]

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            assert adapter is not None
            resolution = adapter.source_info["set"][
                "palette_ambiguity_resolution"]
            self.assertIn("one identical complete SET profile", resolution)
            self.assertIn("2 unique candidate root(s)", resolution)

    def test_ambiguous_profile_refuses_a_missing_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "Set1"
            palette_path, _shader, _tracy, palette = _make_set_sources(first)
            missing_palette = (
                root / "disconnected" / "Set1" / "PALETTE" / "STANDARD.PAL"
            )
            family = _make_family(
                palette_path, palette, [], search_roots=[first])
            family.external_palette_status = "ambiguous"
            family.external_palette_candidates = [
                str(palette_path), str(missing_palette),
            ]

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertIsNone(adapter)
            self.assertIn("1 of 2 unique ambiguous", reason)
            self.assertIn("palette is missing", reason)

    def test_ambiguous_profile_refuses_an_incomplete_remap_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "Set1"
            second = root / "Set2"
            palette_path, _shader, _tracy, palette = _make_set_sources(first)
            second_palette = second / "PALETTE" / "STANDARD.PAL"
            second_palette.parent.mkdir(parents=True)
            _write_palette(second_palette, palette)
            second_remap = second / "REMAP"
            second_remap.mkdir()
            _write_vbmp(
                second_remap / "SHADERMP.ILB", _ua_profile_shader_table())
            family = _make_family(
                palette_path, palette, [], search_roots=[first, second])
            family.external_palette_status = "ambiguous"
            family.external_palette_candidates = [
                str(palette_path), str(second_palette),
            ]

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertIsNone(adapter)
            self.assertIn("1 of 2 unique ambiguous", reason)
            self.assertIn("TRACYRMP is missing", reason)

    def test_ambiguous_preview_palette_cannot_select_an_exact_set_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "Set1"
            second = root / "Set2"
            palette_path, _shader, _tracy, palette = _make_set_sources(first)
            different_shader = bytearray(_ua_profile_shader_table())
            different_shader[1 * 256 + 2] = 99
            second_palette, _shader2, _tracy2, _ = _make_set_sources(
                second, shader=bytes(different_shader))
            family = _make_family(
                palette_path, palette, [], search_roots=[first, second])
            family.external_palette_status = "ambiguous"
            family.external_palette_candidates = [
                str(palette_path), str(second_palette),
            ]

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertIsNone(adapter)
            self.assertIn("palette/set origin is ambiguous", reason)
            self.assertIn("2 distinct complete", reason)

    def test_palette_set_remaps_win_and_sources_are_manifest_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected" / "Set1"
            palette_path, shader_path, tracy_path, palette = _make_set_sources(
                selected
            )
            decoy = root / "earlier-search-root"
            _make_set_sources(
                decoy,
                shader=bytes([91]) * 65536,
                tracy=bytes([92]) * 65536,
            )
            family = _make_family(
                palette_path, palette, [], search_roots=[decoy, selected]
            )

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            assert adapter is not None
            self.assertTrue(adapter.available)
            self.assertEqual(adapter.availability_reason, "")
            self.assertIsNotNone(adapter.tables)
            self.assertEqual(adapter.shader_path, shader_path.resolve())
            self.assertEqual(adapter.tracy_path, tracy_path.resolve())
            self.assertEqual(
                adapter.source_hashes["shader"],
                hashlib.sha256(shader_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                adapter.source_hashes["tracy"],
                hashlib.sha256(tracy_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                adapter.source_info["palette"]["path"],
                str(palette_path.resolve()),
            )
            json.dumps(adapter.source_info)

    def test_source_discovery_never_recurses_below_bounded_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Set1"
            palette_dir = root / "PALETTE"
            palette_dir.mkdir(parents=True)
            palette = [(index, index, index) for index in range(256)]
            palette_path = palette_dir / "STANDARD.PAL"
            _write_palette(palette_path, palette)
            hidden = root / "unrelated" / "nested" / "REMAP"
            hidden.mkdir(parents=True)
            _write_vbmp(hidden / "SHADERMP.ILB", _identity_table())
            _write_vbmp(hidden / "TRACYRMP.ILB", _identity_table())
            family = _make_family(palette_path, palette, [], search_roots=[root])

            adapter, reason = IndexedFamilyAdapter.try_create(family)

            self.assertIsNone(adapter)
            self.assertIn("bounded search roots", reason)
            self.assertNotIn(str(hidden), reason)


class IndexedFamilyMaterialTests(unittest.TestCase):
    def test_block_lookup_uses_viewer_material_group_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1"
            )
            first = AmeshBlock(
                color_val=10, atts=[AttsEntry(3, 11, 0, 0, 0)]
            )
            second = AmeshBlock(
                color_val=20, atts=[AttsEntry(4, 22, 0, 0, 0)]
            )
            family = _make_family(palette_path, palette, [first, second])
            # Deliberately differ from base_object.ades.  ViewFace.block_index
            # comes from this material sequence, so the adapter must too.
            family.root_object.materials.reverse()
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None

            surface = adapter.resolve_surface(
                _face(0, 4, shade=None), _material("second"), 0
            )

            self.assertIs(adapter.block_for("root", 0), second)
            self.assertIs(adapter.block_for("root", 1), first)
            self.assertEqual(surface.solid_index, 22)

    def test_animation_selects_raw_bitmap_by_supplied_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1"
            )
            block = AmeshBlock(
                polflags=0x0A,
                texture=TextureRef(kind="bmpanim", name="SPIN.ANM"),
                atts=[AttsEntry(0, 0, 33, 0, 0)],
            )
            textures = {
                "FRAME0.ILBM": IlbmImage(
                    source_name="FRAME0.ILBM", width=1, height=1,
                    pixels=b"\x05",
                ),
                "FRAME1.ILBM": IlbmImage(
                    source_name="FRAME1.ILBM", width=1, height=1,
                    pixels=b"\xc8",
                ),
            }
            family = _make_family(palette_path, palette, [block], textures)
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None
            material = _material(
                "SPIN.ANM",
                names=("FRAME0.ILBM", "FRAME1.ILBM"),
                frames=((10, 0, 0), (10, 1, 1)),
                uv_groups=(((0, 0),), ((255, 255),)),
            )

            first = adapter.resolve_surface(_face(0, 0, 33), material, 0)
            second = adapter.resolve_surface(_face(0, 0, 33), material, 1)

            self.assertEqual(adapter.active_texture_name(material, 0), "FRAME0.ILBM")
            self.assertEqual(adapter.active_texture_name(material, 1), "FRAME1.ILBM")
            self.assertEqual(first.name, "FRAME0.ILBM")
            self.assertEqual(second.name, "FRAME1.ILBM")
            self.assertEqual(_surface_bytes(first), b"\x05")
            self.assertEqual(_surface_bytes(second), b"\xc8")
            self.assertEqual(adapter.active_uvs(material, 1), ((255, 255),))

    def test_same_anm_blocks_keep_independent_frame_and_uv_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1"
            )
            blocks = [
                AmeshBlock(
                    polflags=0x0A,
                    texture=TextureRef(kind="bmpanim", name="SHARED.ANM"),
                    atts=[AttsEntry(poly_id, 0, 0, 0, 0)],
                )
                for poly_id in (3, 4)
            ]
            textures = {
                "A.ILBM": IlbmImage(width=1, height=1, pixels=b"\x0a"),
                "B.ILBM": IlbmImage(width=1, height=1, pixels=b"\x14"),
            }
            family = _make_family(palette_path, palette, blocks, textures)
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None
            material_a = _material(
                "SHARED.ANM",
                names=("A.ILBM", "B.ILBM"),
                frames=((10, 0, 0),),
                uv_groups=(((1, 2),),),
            )
            material_b = _material(
                "SHARED.ANM",
                names=("A.ILBM", "B.ILBM"),
                frames=((10, 1, 0),),
                uv_groups=(((201, 202),),),
            )

            surface_a = adapter.resolve_surface(_face(0, 3), material_a, 0)
            surface_b = adapter.resolve_surface(_face(1, 4), material_b, 0)

            self.assertIs(adapter.block_for("root", 0), blocks[0])
            self.assertIs(adapter.block_for("root", 1), blocks[1])
            self.assertEqual(_surface_bytes(surface_a), b"\x0a")
            self.assertEqual(_surface_bytes(surface_b), b"\x14")
            self.assertEqual(adapter.active_uvs(material_a, 0), ((1, 2),))
            self.assertEqual(adapter.active_uvs(material_b, 0), ((201, 202),))

    def test_solid_uses_per_atts_color_and_structural_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1"
            )
            block = AmeshBlock(
                polflags=0x50,
                color_val=6,
                shade_val=7,
                atts=[AttsEntry(9, 77, 88, 99, 0)],
            )
            family = _make_family(palette_path, palette, [block])
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None

            surface = adapter.resolve_surface(
                _face(0, 9, shade=None), _material("solid"), 0
            )

            self.assertEqual(surface.kind, "solid")
            self.assertEqual(surface.solid_index, 77)
            self.assertIsNone(surface.indices)
            self.assertEqual(surface.shade_mode, "flat")
            self.assertEqual(surface.shade_value, 88)
            self.assertEqual(surface.tracy_mode, "clear")
            self.assertEqual(surface.map_mode, "linear")

    def test_embedded_palette_wins_for_chroma_and_raw_bytes_are_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            external = [(index, index, index) for index in range(256)]
            external[7] = (255, 255, 0)
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1", palette=external
            )
            embedded = [(index, index, index) for index in range(256)]
            embedded[3] = (255, 255, 0)
            embedded[7] = (0, 0, 0)
            embedded_image = IlbmImage(
                source_name="EMBEDDED.ILBM", width=2, height=1,
                palette=embedded, pixels=b"\x03\x07",
            )
            external_image = IlbmImage(
                source_name="EXTERNAL.ILBM", width=1, height=1,
                pixels=b"\x07",
            )
            blocks = [
                AmeshBlock(
                    polflags=0x0A,
                    texture=TextureRef(kind="ilbm", name=name),
                    atts=[AttsEntry(index, 0, 0, 0, 0)],
                )
                for index, name in enumerate(("EMBEDDED.ILBM", "EXTERNAL.ILBM"))
            ]
            family = _make_family(
                palette_path,
                palette,
                blocks,
                {"EMBEDDED.ILBM": embedded_image,
                 "EXTERNAL.ILBM": external_image},
            )
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None

            embedded_surface = adapter.resolve_surface(
                _face(0, 0), _material("EMBEDDED.ILBM"), 0
            )
            external_surface = adapter.resolve_surface(
                _face(1, 1), _material("EXTERNAL.ILBM"), 0
            )
            adapter.resolve_surface(_face(0, 0), _material("EMBEDDED.ILBM"), 0)

            self.assertTrue(embedded_surface.chroma_by_index[3])
            self.assertFalse(embedded_surface.chroma_by_index[7])
            self.assertTrue(external_surface.chroma_by_index[7])
            self.assertEqual(_surface_bytes(embedded_surface), b"\x03\x07")
            self.assertIn(id(embedded_image), adapter.raw_texture_cache)
            self.assertEqual(len(adapter.raw_texture_cache), 2)

    def test_effect_override_is_reported_but_never_consumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            palette_path, _shader, _tracy, palette = _make_set_sources(
                root / "Set1"
            )
            override = root / "magenta-effect.png"
            override.write_bytes(b"deliberately not an indexed texture")
            raw = IlbmImage(
                source_name="FX1.ILBM", width=2, height=1,
                pixels=b"\x0d\x9f",
            )
            block = AmeshBlock(
                polflags=0x8A,
                texture=TextureRef(kind="ilbm", name="FX1.ILBM"),
                atts=[AttsEntry(0, 0, 0, 0, 0)],
            )
            family = _make_family(
                palette_path,
                palette,
                [block],
                {"FX1.ILBM": raw},
                effect_overrides={"fx1.ilbm": override},
            )
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None

            surface = adapter.resolve_surface(
                _face(0, 0), _material("FX1.ILBM"), 0
            )

            self.assertEqual(_surface_bytes(surface), b"\x0d\x9f")
            self.assertEqual(surface.tracy_mode, "flat")
            self.assertEqual(
                adapter.source_info["effect_override_paths_ignored"],
                [str(override.resolve())],
            )

    def test_mapped_tracy_raises_controlled_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            palette_path, _shader, _tracy, palette = _make_set_sources(
                Path(temporary) / "Set1"
            )
            block = AmeshBlock(
                polflags=0xCA,
                texture=TextureRef(kind="ilbm", name="MASK.ILBM"),
                atts=[AttsEntry(0, 0, 0, 0, 0)],
            )
            family = _make_family(
                palette_path,
                palette,
                [block],
                {"MASK.ILBM": IlbmImage(width=1, height=1, pixels=b"\x01")},
            )
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            assert adapter is not None

            with self.assertRaisesRegex(
                    UnsupportedIndexedMaterialError, "mapped TRACY"):
                adapter.resolve_surface(
                    _face(0, 0), _material("MASK.ILBM"), 0
                )

            self.assertEqual(adapter.unsupported_mapped_tracy, ("root block 0",))
            self.assertEqual(len(adapter.diagnostics), 1)
            self.assertIn("mapped TRACY", adapter.diagnostics[0])


if __name__ == "__main__":
    unittest.main()
