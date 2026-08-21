from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from psx_native_assets import (
    PsxNativeBuild,
    PsxNativeFace,
    PsxNativeMesh,
    PsxNativeModelSlotEvidence,
)
from psx_native_contract import (
    PsxNativeContractError,
    validate_psx_native_build,
    validate_psx_native_effect_inventory,
    validate_psx_native_mesh,
    validate_psx_native_texture_inventory,
    validate_psx_native_texture_pack,
)
from psx_native_effects import (
    PV2_BINDING_STATUS,
    PV2_FORMAT_VERSION,
    PV2_PARSER_ID,
    PV2_PARSER_VERSION,
    PsxPv2Mesh,
)
from psx_native_identity import PSX_MODEL_SLOT_EVIDENCE_ID
from psx_native_textures import (
    FULL_RECORD_SIZE,
    LATE_SELECTOR_TO_PIXEL_BANK_MAPPING,
    LATE_SETGFX_LAYOUT_ID,
    MATERIAL_SLOT_COUNT,
    PACKED_PIXEL_BYTES,
    SECTOR_PAD_BYTE,
    SECTOR_PADDED_EMPTY_ALLOCATION,
    SECTOR_PADDED_EMPTY_MARKER,
    SECTOR_PADDED_FULL_ALLOCATION,
    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
    SECTOR_PADDED_SETGFX_LAYOUT_ID,
    ZERO_RECORD_HEADER,
    PsxNativeTexturePack,
    parse_late_setgfx_bytes,
    parse_sector_padded_setgfx_bytes,
)


_UNIT_HASH = "33" * 32
_EXECUTABLE_HASH = "22" * 32
_TABLE_HASH = "55" * 32


def _native_face(version: int) -> PsxNativeFace:
    raw_uvs = ((0, 0), (64, 0), (64, 64), (0, 64))
    signed_uvs = tuple(
        (u << 16, v << 16) for u, v in raw_uvs
    ) if version == 1 else ()
    record_size = 76 if version == 1 else 26
    prefix_size = 8 if version == 1 else 4
    raw_record = bytearray(record_size)
    if version == 3:
        struct.pack_into("<H", raw_record, 0, 0x4010)
        struct.pack_into("<4H", raw_record, 4, 0, 1, 2, 2)
        raw_record[12:20] = bytes(
            component for uv in raw_uvs for component in uv)
        struct.pack_into("<H", raw_record, 20, 7)
        raw_record[22:26] = bytes((128, 128, 128, 128))
    else:
        struct.pack_into("<4I", raw_record, 8, 0, 1, 2, 2)
        struct.pack_into(
            "<8i",
            raw_record,
            24,
            *(component << 16 for uv in raw_uvs for component in uv),
        )
        struct.pack_into("<I", raw_record, 56, 7)
        struct.pack_into(
            "<4I", raw_record, 60,
            *(shade << 16 for shade in (128, 128, 128, 128)))
    return PsxNativeFace(
        source_offset=116,
        raw_record=bytes(raw_record),
        opaque_prefix=bytes(raw_record[:prefix_size]),
        vertex_indices=(0, 1, 2),
        uv_bytes=raw_uvs[:3],
        texture_selector=7,
        corner_shades=(128, 128, 128),
        raw_vertex_indices=(0, 1, 2, 2),
        raw_uv_bytes=raw_uvs,
        raw_corner_shades=(128, 128, 128, 128),
        psw_uv_fixed_16_16=signed_uvs,
        pw3_primitive_flags=None if version == 1 else 0x4010,
    )


def _native_mesh(
        version: int = 3, *, logical_path: str | None = None,
        archive_ordinal: int | None = None,
        model_slot: int | None = None,
        model_slot_evidence_id: str | None = None) -> PsxNativeMesh:
    return PsxNativeMesh(
        logical_path=(
            logical_path
            if logical_path is not None else
            (
                "UNITMODL/UNIT.BIN"
                if archive_ordinal is not None else
                ("UNITMODL/V1.PSW" if version == 1 else "UNITMODL/V1.PW3")
            )
        ),
        format_id="PSW/PSV" if version == 1 else "PW3",
        format_version=version,
        archive_ordinal=archive_ordinal,
        archive_offset=(
            None if archive_ordinal is None else archive_ordinal * 0x800),
        archive_sector=archive_ordinal,
        body_size=192 if version == 1 else 144,
        body_sha256="66" * 32,
        vertex_stream_sha256="77" * 32,
        face_stream_sha256="88" * 32,
        raw_vertices=((0, 0, 0), (65536, 0, 0), (0, 65536, 0)),
        vertices=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        faces=(_native_face(version),),
        model_slot=model_slot,
        model_slot_evidence_id=model_slot_evidence_id,
    )


def _pv2(logical_path: str) -> PsxPv2Mesh:
    return PsxPv2Mesh(
        logical_path=logical_path,
        format_id="PV2",
        format_version=PV2_FORMAT_VERSION,
        parser_id=PV2_PARSER_ID,
        parser_version=PV2_PARSER_VERSION,
        binding_status=PV2_BINDING_STATUS,
        body_size=352,
        source_sha256="99" * 32,
        vertex_stream_sha256="aa" * 32,
        face_stream_sha256="bb" * 32,
        raw_header=b"\0" * 32,
        raw_translation=(0, 0, 0),
        translation=(0.0, 0.0, 0.0),
        vertex_pointer_residue=0x00427C20,
        face_pointer_residue=0x00427C98,
        raw_vertices=(),
        vertices=(),
        faces=(),
    )


def _evidence() -> PsxNativeModelSlotEvidence:
    return PsxNativeModelSlotEvidence(
        evidence_id=PSX_MODEL_SLOT_EVIDENCE_ID,
        unit_archive_sha256=_UNIT_HASH,
        boot_executable_sha256=_EXECUTABLE_HASH,
        executable_table_offset=0x6E650,
        executable_table_entry_count=4,
        executable_table_sha256=_TABLE_HASH,
        empty_model_slots=(1,),
        trailing_sentinel_archive_sector=99,
    )


def _build(
        meshes: tuple[PsxNativeMesh, ...], *,
        evidence: PsxNativeModelSlotEvidence | None = None,
        effects: tuple[PsxPv2Mesh, ...] = (),
        texture_packs: tuple[PsxNativeTexturePack, ...] = (),
        ) -> PsxNativeBuild:
    has_packed_meshes = any(
        mesh.archive_ordinal is not None for mesh in meshes)
    return PsxNativeBuild(
        root=Path("synthetic-psx-disc"),
        system_cnf_logical_path="SYSTEM.CNF",
        system_cnf_sha256="11" * 32,
        boot_executable_logical_path="SCES_019.63",
        boot_executable_sha256=_EXECUTABLE_HASH,
        unit_archive_logical_path=(
            "UNITMODL/UNIT.BIN" if has_packed_meshes else None),
        unit_archive_sha256=_UNIT_HASH if has_packed_meshes else None,
        vehicle_roster_logical_path=None,
        vehicle_roster_sha256=None,
        vehicle_roster=(),
        meshes=meshes,
        texture_packs=texture_packs,
        model_slot_evidence=evidence,
        effects=effects,
    )


@lru_cache(maxsize=None)
def _compact_pack(
        logical_path: str = "GFX/SET1GFX.BIN") -> PsxNativeTexturePack:
    records: list[bytes] = []
    palette = struct.pack("<16H", *range(16))
    packed = bytes(PACKED_PIXEL_BYTES)
    for selector in range(MATERIAL_SLOT_COUNT):
        record = ZERO_RECORD_HEADER + palette
        if selector < 32:
            record += packed
            assert len(record) == FULL_RECORD_SIZE
        records.append(record)
    return parse_late_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


@lru_cache(maxsize=None)
def _sector_pack(
        logical_path: str = "GFX/SET2GFX.BIN") -> PsxNativeTexturePack:
    records: list[bytes] = []
    palette = struct.pack("<16H", *range(16))
    packed = bytes(PACKED_PIXEL_BYTES)
    for selector in range(MATERIAL_SLOT_COUNT):
        if selector < 77:
            payload = ZERO_RECORD_HEADER + palette + packed
            records.append(
                payload
                + bytes((SECTOR_PAD_BYTE,))
                * (SECTOR_PADDED_FULL_ALLOCATION - len(payload)))
        else:
            records.append(
                SECTOR_PADDED_EMPTY_MARKER
                + bytes((SECTOR_PAD_BYTE,))
                * (SECTOR_PADDED_EMPTY_ALLOCATION
                   - len(SECTOR_PADDED_EMPTY_MARKER)))
    return parse_sector_padded_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


def _replace_pack_slot(
        pack: PsxNativeTexturePack, slot_index: int, **changes,
        ) -> PsxNativeTexturePack:
    slots = list(pack.slots)
    slots[slot_index] = replace(slots[slot_index], **changes)
    return replace(pack, slots=tuple(slots))


def _replace_pack_mapping(
        pack: PsxNativeTexturePack, selector: int, value: int | None,
        ) -> PsxNativeTexturePack:
    mapping = list(pack.selector_pixel_banks)
    mapping[selector] = value
    return replace(pack, selector_pixel_banks=tuple(mapping))


def _replace_pixel_bank(
        pack: PsxNativeTexturePack, bank: int, value: bytes,
        ) -> PsxNativeTexturePack:
    banks = list(pack.pixel_banks)
    banks[bank] = value
    return replace(pack, pixel_banks=tuple(banks))


def _proven_build() -> PsxNativeBuild:
    packed = tuple(
        _native_mesh(
            logical_path="UNITMODL/UNIT.BIN",
            archive_ordinal=ordinal,
            model_slot=slot,
            model_slot_evidence_id=PSX_MODEL_SLOT_EVIDENCE_ID,
        )
        for ordinal, slot in enumerate((0, 2, 3))
    )
    loose = _native_mesh(
        1, logical_path="UNITMODL/V90.PSW",
    )
    return _build(
        packed + (loose,),
        evidence=_evidence(),
        effects=(_pv2("EFFECTS/A.PV2"), _pv2("effects/b.pv2")),
    )


class PsxNativeMeshContractTests(unittest.TestCase):
    def test_exact_v1_and_v3_mesh_contracts_are_accepted(self):
        for mesh in (_native_mesh(1), _native_mesh(3)):
            with self.subTest(version=mesh.format_version):
                self.assertIs(validate_psx_native_mesh(mesh), mesh)

    def test_mesh_version_and_format_are_bidirectionally_exact(self):
        psw = _native_mesh(1)
        pw3 = _native_mesh(3)
        candidates = (
            replace(psw, format_id="PW3"),
            replace(pw3, format_id="PSW/PSV"),
            replace(pw3, format_version=1),
            replace(pw3, format_version=2),
            replace(pw3, format_version=True),
        )
        for candidate in candidates:
            with self.subTest(
                    version=candidate.format_version,
                    format_id=candidate.format_id), self.assertRaisesRegex(
                        PsxNativeContractError, "format/version|plain integer"):
                validate_psx_native_mesh(candidate)

    def test_v1_requires_none_pw3_flags_and_present_signed_uv_tuples(self):
        mesh = _native_mesh(1)
        face = mesh.faces[0]
        invalid_faces = (
            replace(face, pw3_primitive_flags=0),
            replace(face, psw_uv_fixed_16_16=()),
            replace(
                face,
                psw_uv_fixed_16_16=(
                    (True, 0), (0, 0), (0, 0), (0, 0)),
            ),
            replace(
                face,
                psw_uv_fixed_16_16=(
                    (1 << 31, 0), (0, 0), (0, 0), (0, 0)),
            ),
            replace(
                face,
                psw_uv_fixed_16_16=(
                    (1, 0), (0, 0), (0, 0), (0, 0)),
            ),
            replace(
                face,
                psw_uv_fixed_16_16=(
                    (256 << 16, 0), (0, 0), (0, 0), (0, 0)),
            ),
        )
        for invalid_face in invalid_faces:
            with self.subTest(face=invalid_face), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_mesh(
                    replace(mesh, faces=(invalid_face,)))

    def test_v3_requires_plain_u16_flags_and_an_empty_psw_uv_tuple(self):
        mesh = _native_mesh(3)
        face = mesh.faces[0]
        invalid_faces = (
            replace(face, pw3_primitive_flags=None),
            replace(face, pw3_primitive_flags=True),
            replace(face, pw3_primitive_flags=0x10000),
            replace(
                face,
                psw_uv_fixed_16_16=(
                    (0, 0), (0, 0), (0, 0), (0, 0)),
            ),
        )
        for invalid_face in invalid_faces:
            with self.subTest(face=invalid_face), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_mesh(
                    replace(mesh, faces=(invalid_face,)))

    def test_raw_face_cardinality_domains_and_record_sizes_are_exact(self):
        mesh = _native_mesh(3)
        face = mesh.faces[0]
        invalid_faces = (
            replace(face, raw_record=face.raw_record[:-1]),
            replace(face, opaque_prefix=b"\0" * 3),
            replace(face, opaque_prefix=b"\0" * 4),
            replace(face, raw_vertex_indices=(0, 1, 2)),
            replace(face, raw_vertex_indices=(0, 1, 3, 2)),
            replace(face, raw_vertex_indices=(1, 0, 2, 2)),
            replace(
                face,
                raw_uv_bytes=((0, 0), (0, 0), (0, 0), (0, 256)),
            ),
            replace(
                face,
                raw_uv_bytes=((1, 0), (64, 0), (64, 64), (0, 64)),
            ),
            replace(face, raw_corner_shades=(0, 0, 0)),
            replace(face, raw_corner_shades=(0, 0, 0, 256)),
            replace(face, raw_corner_shades=(127, 128, 128, 128)),
            replace(face, texture_selector=True),
            replace(face, texture_selector=8),
            replace(face, pw3_primitive_flags=0),
            replace(face, vertex_indices=(0, 2, 1)),
            replace(face, uv_bytes=((1, 0), (64, 0), (64, 64))),
            replace(face, corner_shades=(127, 128, 128)),
            replace(face, source_offset=117),
        )
        for invalid_face in invalid_faces:
            with self.subTest(face=invalid_face), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_mesh(replace(
                    mesh, faces=(invalid_face,)))

        invalid_meshes = (
            replace(mesh, vertices=()),
            replace(mesh, raw_vertices=mesh.raw_vertices[:-1]),
            replace(mesh, faces=()),
            replace(mesh, body_size=145),
            replace(
                mesh,
                raw_vertices=((True, 0, 0),) + mesh.raw_vertices[1:]),
            replace(
                mesh,
                raw_vertices=(((0, 0),) + mesh.raw_vertices[1:])),
            replace(
                mesh,
                vertices=((float("nan"), 0.0, 0.0),) + mesh.vertices[1:]),
            replace(
                mesh,
                vertices=((0.5, 0.0, 0.0),) + mesh.vertices[1:]),
        )
        for invalid_mesh in invalid_meshes:
            with self.subTest(mesh=invalid_mesh), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_mesh(invalid_mesh)


class PsxNativeEffectContractTests(unittest.TestCase):
    def test_exact_ordered_static_pv2_inventory_is_accepted(self):
        effects = (_pv2("EFFECTS/A.PV2"), _pv2("effects/b.pv2"))
        self.assertIs(
            validate_psx_native_effect_inventory(effects), effects)

    def test_pv2_metadata_must_match_the_canonical_static_contract(self):
        effect = _pv2("EFFECTS/A.PV2")
        candidates = (
            replace(effect, format_id="PW3"),
            replace(effect, format_version=3),
            replace(effect, format_version=True),
            replace(effect, parser_id="another_parser"),
            replace(effect, parser_version=2),
            replace(effect, parser_version=True),
            replace(effect, binding_status="animated_effect"),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_effect_inventory((candidate,))

    def test_pv2_paths_are_casefold_unique_and_canonically_ordered(self):
        duplicate = (_pv2("EFFECTS/A.PV2"), _pv2("effects/a.pv2"))
        unordered = (_pv2("effects/z.pv2"), _pv2("EFFECTS/A.PV2"))
        for effects, message in (
                (duplicate, "case-insensitively unique"),
                (unordered, "case-insensitive order")):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_effect_inventory(effects)


class PsxNativePortableProvenanceTests(unittest.TestCase):
    def test_build_paths_hashes_optional_pairs_and_roster_are_exact(self):
        build = _build((_native_mesh(),))
        candidates = (
            replace(build, system_cnf_logical_path=r"DISC\SYSTEM.CNF"),
            replace(build, system_cnf_logical_path="../SYSTEM.CNF"),
            replace(build, system_cnf_sha256="AB" * 32),
            replace(
                build,
                unit_archive_logical_path="UNITMODL/UNIT.BIN",
            ),
            replace(build, unit_archive_sha256=_UNIT_HASH),
            replace(
                build,
                vehicle_roster_logical_path="LISTS/VEHICLE.TXT",
            ),
            replace(build, vehicle_roster=("TAERKASTEN",)),
            replace(build, vehicle_roster=[]),  # type: ignore[arg-type]
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_build(candidate)

    def test_mesh_and_effect_paths_and_hashes_are_portable(self):
        mesh = _native_mesh()
        effect = _pv2("EFFECTS/A.PV2")
        invalid_meshes = (
            replace(mesh, logical_path="/UNITMODL/V1.PW3"),
            replace(mesh, logical_path="UNITMODL//V1.PW3"),
            replace(mesh, body_sha256="AB" * 32),
            replace(mesh, vertex_stream_sha256="short"),
        )
        for candidate in invalid_meshes:
            with self.subTest(mesh=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_mesh(candidate)

        invalid_effects = (
            replace(effect, logical_path=r"EFFECTS\A.PV2"),
            replace(effect, source_sha256="AB" * 32),
            replace(effect, face_stream_sha256="short"),
        )
        for candidate in invalid_effects:
            with self.subTest(effect=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_effect_inventory((candidate,))


class PsxNativeTextureContractTests(unittest.TestCase):
    def test_parser_packs_are_accepted_without_resolving_materials(self):
        compact = _compact_pack()
        sector = _sector_pack()
        with patch.object(
                PsxNativeTexturePack,
                "material",
                side_effect=AssertionError("material resolution is forbidden")):
            self.assertIs(validate_psx_native_texture_pack(compact), compact)
            self.assertIs(validate_psx_native_texture_pack(sector), sector)

    def test_texture_pack_path_and_hash_are_exact_portable_provenance(self):
        pack = _compact_pack()
        candidates = (
            replace(pack, logical_path="/GFX/SET1GFX.BIN"),
            replace(pack, logical_path=r"GFX\SET1GFX.BIN"),
            replace(pack, source_sha256="AB" * 32),
            replace(pack, source_sha256="short"),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_texture_pack(candidate)

    def test_layout_mapping_and_table_cardinalities_are_exact(self):
        compact = _compact_pack()
        candidates = (
            replace(compact, layout_id="invented_layout"),
            replace(
                compact,
                selector_to_pixel_bank_mapping=(
                    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING),
            ),
            replace(compact, slots=compact.slots[:-1]),
            replace(compact, pixel_banks=compact.pixel_banks[:-1]),
            replace(
                compact,
                selector_pixel_banks=compact.selector_pixel_banks[:-1],
            ),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_texture_pack(candidate)

        sector = _sector_pack()
        self.assertEqual(
            sector.layout_id, SECTOR_PADDED_SETGFX_LAYOUT_ID)
        self.assertEqual(
            sector.selector_to_pixel_bank_mapping,
            SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING)
        with self.assertRaises(PsxNativeContractError):
            validate_psx_native_texture_pack(replace(
                sector,
                layout_id=LATE_SETGFX_LAYOUT_ID,
                selector_to_pixel_bank_mapping=(
                    LATE_SELECTOR_TO_PIXEL_BANK_MAPPING),
            ))

    def test_compact_slots_palette_and_low_five_bit_mapping_are_consistent(self):
        pack = _compact_pack()
        first_slot = pack.slots[0]
        changed_words = (1,) + first_slot.palette_words[1:]
        changed_color = replace(first_slot.palette[0], stp=True)
        changed_palette = (changed_color,) + first_slot.palette[1:]
        candidates = (
            _replace_pack_slot(pack, 0, selector=1),
            _replace_pack_slot(pack, 0, source_offset=1),
            _replace_pack_slot(pack, 0, available=False),
            _replace_pack_slot(
                pack, 0, allocation_size=SECTOR_PADDED_FULL_ALLOCATION),
            _replace_pack_slot(pack, 0, header=SECTOR_PADDED_EMPTY_MARKER),
            _replace_pack_slot(pack, 0, palette_words=changed_words),
            _replace_pack_slot(pack, 0, palette=changed_palette),
            _replace_pack_mapping(pack, 33, 2),
            _replace_pixel_bank(pack, 0, b""),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_texture_pack(candidate)

    def test_sector_availability_direct_mapping_and_empty_payload_are_consistent(self):
        pack = _sector_pack()
        populated = 0
        empty = 77
        candidates = (
            _replace_pack_slot(pack, populated, allocation_size=0x800),
            _replace_pack_slot(pack, populated, source_offset=1),
            _replace_pack_slot(pack, empty, available=True),
            _replace_pack_slot(pack, empty, header=ZERO_RECORD_HEADER),
            _replace_pack_mapping(pack, empty, empty),
            _replace_pixel_bank(
                pack, empty, bytes(PACKED_PIXEL_BYTES * 2)),
            _replace_pack_slot(
                pack,
                empty,
                palette_words=pack.slots[populated].palette_words,
                palette=pack.slots[populated].palette,
            ),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_texture_pack(candidate)

    def test_pack_inventory_is_casefold_unique_ordered_and_build_validated(self):
        compact = _compact_pack("GFX/SET1GFX.BIN")
        sector = _sector_pack("GFX/SET2GFX.BIN")
        packs = (compact, sector)
        self.assertIs(validate_psx_native_texture_inventory(packs), packs)
        build = _build((_native_mesh(),), texture_packs=packs)
        self.assertIs(validate_psx_native_build(build), build)

        duplicate = replace(
            sector,
            logical_path="gfx/set1gfx.bin",
        )
        for inventory, message in (
                ((sector, compact), "case-insensitive order"),
                ((compact, duplicate), "case-insensitively unique")):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_texture_inventory(inventory)

        with self.assertRaises(PsxNativeContractError):
            validate_psx_native_build(replace(
                build,
                texture_packs=list(packs),  # type: ignore[arg-type]
            ))


class PsxNativeBuildContractTests(unittest.TestCase):
    def test_complete_model_slot_proof_and_loose_mesh_are_accepted(self):
        build = _proven_build()
        self.assertIs(validate_psx_native_build(build), build)

    def test_build_requires_a_nonempty_coherent_mesh_inventory(self):
        with self.assertRaisesRegex(PsxNativeContractError, "at least one"):
            validate_psx_native_build(_build(()))

        packed = _native_mesh(archive_ordinal=0)
        loose = _native_mesh(logical_path="UNITMODL/V2.PW3")
        packed_after_loose = _build((loose, packed))
        with self.assertRaisesRegex(
                PsxNativeContractError, "packed native meshes must appear"):
            validate_psx_native_build(packed_after_loose)

    def test_packed_archive_offsets_and_sectors_are_exact(self):
        packed = _native_mesh(archive_ordinal=0)
        candidates = (
            replace(packed, archive_offset=None),
            replace(packed, archive_offset=1),
            replace(packed, archive_sector=1),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                    PsxNativeContractError):
                validate_psx_native_build(_build((candidate,)))

        loose = _native_mesh()
        with self.assertRaisesRegex(PsxNativeContractError, "loose native"):
            validate_psx_native_build(_build((replace(
                loose, archive_offset=0, archive_sector=0),)))

    def test_unit_archive_provenance_and_packed_paths_are_iff_exact(self):
        packed_build = _build((_native_mesh(archive_ordinal=0),))
        without_unit = replace(
            packed_build,
            unit_archive_logical_path=None,
            unit_archive_sha256=None,
        )
        with self.assertRaisesRegex(PsxNativeContractError, "exactly when"):
            validate_psx_native_build(without_unit)

        loose_build = _build((_native_mesh(),))
        with_unit = replace(
            loose_build,
            unit_archive_logical_path="UNITMODL/UNIT.BIN",
            unit_archive_sha256=_UNIT_HASH,
        )
        with self.assertRaisesRegex(PsxNativeContractError, "exactly when"):
            validate_psx_native_build(with_unit)

        relabeled = replace(
            packed_build.meshes[0], logical_path="UNITMODL/V1.PW3")
        with self.assertRaisesRegex(PsxNativeContractError, "exactly match"):
            validate_psx_native_build(replace(
                packed_build, meshes=(relabeled,)))

    def test_loose_mesh_paths_are_casefold_unique_and_ordered(self):
        first = _native_mesh(logical_path="UNITMODL/A.PW3")
        second = _native_mesh(logical_path="unitmodl/b.pw3")
        valid = _build((first, second))
        self.assertIs(validate_psx_native_build(valid), valid)

        duplicate = replace(second, logical_path="unitmodl/a.pw3")
        unordered = _build((second, first))
        for candidate, message in (
                (_build((first, duplicate)), "case-insensitively unique"),
                (unordered, "case-insensitive order")):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_build(candidate)

    def test_dense_ordinals_must_be_unique_and_contiguous_from_zero(self):
        build = _proven_build()
        packed = build.meshes[:3]
        loose = build.meshes[3:]
        candidates = (
            replace(
                build,
                meshes=packed[:2] + (
                    replace(packed[2], archive_ordinal=1),) + loose,
            ),
            replace(
                build,
                meshes=packed[:2] + (
                    replace(packed[2], archive_ordinal=3),) + loose,
            ),
        )
        for candidate, message in zip(
                candidates, ("unique", "contiguous"), strict=True):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_build(candidate)

    def test_evidence_id_and_source_hashes_must_match_exactly(self):
        build = _proven_build()
        evidence = build.model_slot_evidence
        assert evidence is not None
        candidates = (
            replace(
                build,
                model_slot_evidence=replace(evidence, evidence_id="wrong"),
            ),
            replace(
                build,
                model_slot_evidence=replace(
                    evidence, unit_archive_sha256="ff" * 32),
            ),
            replace(
                build,
                model_slot_evidence=replace(
                    evidence, boot_executable_sha256="ee" * 32),
            ),
            replace(
                build,
                model_slot_evidence=replace(
                    evidence,
                    executable_table_sha256="AB" * 32,
                ),
            ),
        )
        messages = ("evidence ID", "UNIT.BIN hash", "executable hash", "SHA-256")
        for candidate, message in zip(candidates, messages, strict=True):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_build(candidate)

    def test_each_packed_mesh_must_carry_the_exact_evidence_id(self):
        build = _proven_build()
        bad_mesh = replace(
            build.meshes[0], model_slot_evidence_id="wrong_evidence")
        candidate = replace(build, meshes=(bad_mesh,) + build.meshes[1:])
        with self.assertRaisesRegex(
                PsxNativeContractError, "exact build model-slot evidence ID"):
            validate_psx_native_build(candidate)

    def test_packed_count_must_equal_table_count_minus_empty_count(self):
        build = _proven_build()
        candidate = replace(
            build,
            meshes=build.meshes[:2] + build.meshes[3:],
        )
        with self.assertRaisesRegex(
                PsxNativeContractError, "table count.*minus empty-slot"):
            validate_psx_native_build(candidate)

    def test_model_slots_must_be_unique_and_exactly_cover_nonempty_entries(self):
        build = _proven_build()
        duplicate = replace(
            build,
            meshes=build.meshes[:2] + (
                replace(build.meshes[2], model_slot=2),) + build.meshes[3:],
        )
        wrong_set = replace(
            build,
            meshes=build.meshes[:2] + (
                replace(build.meshes[2], model_slot=4),) + build.meshes[3:],
        )
        for candidate, message in (
                (duplicate, "model slots must be unique"),
                (wrong_set, "exactly equal the non-empty")):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_build(candidate)

    def test_empty_slots_are_canonical_unique_and_inside_the_table(self):
        build = _proven_build()
        evidence = build.model_slot_evidence
        assert evidence is not None
        candidates = (
            replace(
                build,
                model_slot_evidence=replace(
                    evidence, empty_model_slots=(1, 1)),
            ),
            replace(
                build,
                model_slot_evidence=replace(
                    evidence, empty_model_slots=(4,)),
            ),
        )
        for candidate, message in zip(
                candidates, ("unique and ordered", "inside"), strict=True):
            with self.subTest(message=message), self.assertRaisesRegex(
                    PsxNativeContractError, message):
                validate_psx_native_build(candidate)

    def test_loose_meshes_never_inherit_packed_slots(self):
        build = _proven_build()
        loose = replace(
            build.meshes[-1],
            model_slot=1,
            model_slot_evidence_id=PSX_MODEL_SLOT_EVIDENCE_ID,
        )
        candidate = replace(build, meshes=build.meshes[:-1] + (loose,))
        with self.assertRaisesRegex(
                PsxNativeContractError, "Loose native meshes|loose native"):
            validate_psx_native_build(candidate)

    def test_meshes_cannot_claim_slots_when_build_evidence_is_absent(self):
        packed = _native_mesh(archive_ordinal=0)
        unproven = _build((packed,))
        self.assertIs(validate_psx_native_build(unproven), unproven)
        claimed = replace(
            packed,
            model_slot=0,
            model_slot_evidence_id=PSX_MODEL_SLOT_EVIDENCE_ID,
        )
        with self.assertRaisesRegex(
                PsxNativeContractError, "without frozen executable-table"):
            validate_psx_native_build(_build((claimed,)))

    def test_build_validation_also_enforces_the_effect_inventory(self):
        build = _build(
            (_native_mesh(archive_ordinal=0),),
            effects=(_pv2("effects/z.pv2"), _pv2("effects/a.pv2")),
        )
        with self.assertRaisesRegex(
                PsxNativeContractError, "case-insensitive order"):
            validate_psx_native_build(build)


if __name__ == "__main__":
    unittest.main()
