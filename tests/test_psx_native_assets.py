from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import psx_native_assets

from psx_native_assets import (
    PSX_NATIVE_PARSER_VERSION,
    PsxNativeAssetError,
    PW3_NCLIP_RAW_CORNER_ORDER,
    PW3_RAW_REVERSE_FAN_TRIANGLES,
    PW3_TWO_SIDED_FLAG,
    UNIT_ARCHIVE_MAGIC,
    discover_extracted_psx_builds,
    is_extracted_psx_build_root,
    load_extracted_psx_build,
    mesh_primitive_cull_census,
    mesh_raw_corner_shade_census,
    parse_psx_mesh_bytes,
    parse_psx_mesh_file,
    scan_unit_archive_bytes,
    scan_unit_archive_file,
)
from psx_native_effects import (
    PV2_BINDING_STATUS,
    PV2_BODY_SIZE,
    PV2_FACE_POINTER_RESIDUE,
    PV2_PARSER_ID,
    PV2_PARSER_VERSION,
    PV2_VERTEX_POINTER_RESIDUE,
)
from psx_native_textures import (
    LATE_SELECTOR_TO_PIXEL_BANK_MAPPING,
    LATE_SETGFX_LAYOUT_ID,
    LATE_SETGFX_SIZE,
    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
    SECTOR_PADDED_SETGFX_LAYOUT_ID,
)
from psx_psw_pipeline import psw_material_local_uv_quotient


def _vertices() -> list[tuple[int, int, int]]:
    return [
        (-65536, 0, 0),
        (0, -65536, 0),
        (65536, 0, 0),
        (0, 65536, 32768),
    ]


def _pw3_face(
        indices, selector=7, shades=(10, 20, 30, 40),
        flags=0x2211) -> bytes:
    record = bytearray(26)
    struct.pack_into("<H", record, 0, flags)
    record[2:4] = b"\x33\x44"
    struct.pack_into("<4H", record, 4, *indices)
    record[12:20] = bytes((0, 0, 255, 0, 255, 255, 0, 255))
    struct.pack_into("<H", record, 20, selector)
    record[22:26] = bytes(shades)
    return bytes(record)


def _psw_face(indices, selector=7, shades=(10, 20, 30, 40)) -> bytes:
    record = bytearray(76)
    record[:8] = b"\x10\x20\x30\x40\x50\x60\x70\x80"
    struct.pack_into("<4I", record, 8, *indices)
    uv = (0, 0, 255, 0, 255, 255, 0, 255)
    struct.pack_into("<8I", record, 24, *(value << 16 for value in uv))
    struct.pack_into("<I", record, 56, selector)
    struct.pack_into(
        "<4I", record, 60, *(value << 16 for value in shades))
    return bytes(record)


def _mesh(version=3, *, bad_index=False) -> bytes:
    vertices = _vertices()
    records = (
        [
            _pw3_face((0, 1, 2, 2)),
            _pw3_face(
                (0, 1, 2, 3), selector=19,
                flags=0x2211 | PW3_TWO_SIDED_FLAG),
        ]
        if version == 3 else
        [
            _psw_face((0, 1, 2, 2)),
            _psw_face((0, 1, 2, 3), selector=19),
        ]
    )
    if bad_index:
        records[0] = (
            _pw3_face((0, 1, 99, 99))
            if version == 3 else _psw_face((0, 1, 99, 99)))
    header = bytearray(80)
    struct.pack_into("<I", header, 0, version)
    struct.pack_into("<II", header, 0x38, len(vertices), len(records))
    struct.pack_into("<II", header, 0x40, 80, 80 + 12 * len(vertices))
    payload = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + b"".join(records)
    return payload + b"\x00" * ((-len(payload)) % 4)


def _archive(*, corrupt_padding=False) -> bytes:
    body = _mesh(3)
    first = UNIT_ARCHIVE_MAGIC + b"\xBA" * (0x800 - 4)
    allocation = body + b"\xBA" * (0x800 - len(body))
    if corrupt_padding:
        allocation = allocation[:-1] + b"\x00"
    empty = UNIT_ARCHIVE_MAGIC + b"\xBA" * (0x800 - 4)
    return first + allocation + empty


def _boot_executable_with_allocation_table() -> bytes:
    """Embed the exact synthetic archive table, omitting its final marker."""

    executable = bytearray(b"PS-X EXE" + b"\0" * (0x200 - 8))
    table = struct.pack(
        "<4I",
        0, len(UNIT_ARCHIVE_MAGIC),
        1, len(_mesh(3)),
    )
    executable[0x80:0x80 + len(table)] = table
    return bytes(executable)


def _pv2(*, translation=(0, 0x25800, 0)) -> bytes:
    header = bytearray(32)
    struct.pack_into("<I", header, 0, 1)
    struct.pack_into("<iii", header, 4, *translation)
    struct.pack_into(
        "<4I", header, 0x10,
        10, 5, PV2_VERTEX_POINTER_RESIDUE, PV2_FACE_POINTER_RESIDUE)
    vertices = b"\0" * (10 * 12)
    face = bytearray(40)
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    body = bytes(header) + vertices + bytes(face) * 5
    assert len(body) == PV2_BODY_SIZE
    return body


class PsxNativeAssetTests(unittest.TestCase):
    def test_pw3_preserves_native_vertices_uv_selector_shades_and_opaque(self):
        mesh = parse_psx_mesh_bytes(
            _mesh(3), logical_path="UNITMODL/UNIT.BIN",
            archive_ordinal=4, archive_offset=0x5000)

        self.assertEqual(mesh.format_id, "PW3")
        self.assertEqual(mesh.format_version, 3)
        self.assertEqual(mesh.archive_ordinal, 4)
        self.assertIsNone(mesh.model_slot)
        self.assertIsNone(mesh.model_slot_evidence_id)
        self.assertTrue(mesh.label.startswith(
            "dense ordinal 004 | PW3 | "))
        self.assertEqual(mesh.archive_sector, 10)
        self.assertEqual(mesh.raw_vertices[0], (-65536, 0, 0))
        self.assertEqual(mesh.vertices[0], (-1.0, 0.0, 0.0))
        self.assertEqual(mesh.faces[0].vertex_indices, (0, 1, 2))
        self.assertEqual(
            mesh.faces[0].uv_bytes, ((0, 0), (255, 0), (255, 255)))
        self.assertEqual(mesh.faces[0].texture_selector, 7)
        self.assertEqual(mesh.faces[0].corner_shades, (10, 20, 30))
        self.assertEqual(
            mesh.faces[0].raw_vertex_indices, (0, 1, 2, 2))
        self.assertEqual(
            mesh.faces[0].raw_uv_bytes,
            ((0, 0), (255, 0), (255, 255), (0, 255)))
        self.assertEqual(mesh.faces[0].psw_uv_fixed_16_16, ())
        self.assertEqual(
            mesh.faces[0].raw_corner_shades, (10, 20, 30, 40))
        self.assertEqual(mesh.faces[0].opaque_prefix, b"\x11\x22\x33\x44")
        self.assertEqual(mesh.faces[0].pw3_primitive_flags, 0x2211)
        self.assertFalse(mesh.faces[0].pw3_two_sided)
        self.assertEqual(mesh.faces[1].vertex_indices, (0, 1, 2, 3))
        self.assertTrue(mesh.faces[1].pw3_two_sided)
        self.assertEqual(PW3_NCLIP_RAW_CORNER_ORDER, (1, 0, 2))
        self.assertEqual(
            PW3_RAW_REVERSE_FAN_TRIANGLES,
            ((0, 2, 1), (0, 3, 2)))
        self.assertEqual(
            mesh_primitive_cull_census(mesh),
            (("pw3_bit14_clear_nclip_strict_positive", 1),
             ("pw3_bit14_set_two_sided", 1)))
        self.assertEqual(
            mesh_raw_corner_shade_census(mesh),
            ((10, 2), (20, 2), (30, 2), (40, 2)))

    def test_psw_v1_narrows_the_cross_validated_fields_only(self):
        mesh = parse_psx_mesh_bytes(
            _mesh(1), logical_path="UNITMODL/V1.PSW")

        self.assertEqual(mesh.format_id, "PSW/PSV")
        self.assertEqual(mesh.format_version, 1)
        self.assertEqual(mesh.faces[0].vertex_indices, (0, 1, 2))
        self.assertEqual(mesh.faces[0].texture_selector, 7)
        self.assertEqual(mesh.faces[0].corner_shades, (10, 20, 30))
        self.assertEqual(
            mesh.faces[0].psw_uv_fixed_16_16,
            ((0, 0), (255 << 16, 0),
             (255 << 16, 255 << 16), (0, 255 << 16)))
        self.assertIsNone(mesh.faces[0].pw3_primitive_flags)
        # None denotes that the PW3-only bit-14 bypass is inapplicable.  The
        # recovered PSW/PSV path still performs unconditional NCLIP.
        self.assertIsNone(mesh.faces[0].pw3_two_sided)
        self.assertEqual(
            mesh_primitive_cull_census(mesh),
            (("psw_psv_unconditional_nclip_strict_positive", 2),))
        self.assertEqual(
            mesh.faces[0].opaque_prefix,
            b"\x10\x20\x30\x40\x50\x60\x70\x80")

    def test_psw_v1_accepts_the_observed_wrapped_minus_one_uv(self):
        source = bytearray(_mesh(1))
        face_offset = 80 + len(_vertices()) * 12
        struct.pack_into("<I", source, face_offset + 24, 0xFFFF0000)

        mesh = parse_psx_mesh_bytes(
            bytes(source), logical_path="UNITMODL/V1.PSW")

        self.assertEqual(mesh.faces[0].uv_bytes[0][0], 255)
        self.assertEqual(
            mesh.faces[0].psw_uv_fixed_16_16[0][0], -1 << 16)

    def test_psw_v1_rejects_fractional_or_out_of_range_narrowing(self):
        face_offset = 80 + len(_vertices()) * 12
        cases = (
            (face_offset + 24, 0x00010001, "non-integral PSW UV"),
            (face_offset + 24, 0x01000000, "non-narrowable PSW UV"),
            (face_offset + 60, 0x000A0001,
             "non-integral PSW corner shade"),
            (face_offset + 60, 0x01000000,
             "non-narrowable PSW corner shade"),
            (face_offset + 24, 0xFFFE0000,
             "non-narrowable PSW UV"),
        )
        for offset, value, message in cases:
            with self.subTest(offset=offset, value=value):
                source = bytearray(_mesh(1))
                struct.pack_into("<I", source, offset, value)
                with self.assertRaisesRegex(PsxNativeAssetError, message):
                    parse_psx_mesh_bytes(
                        bytes(source), logical_path="bad.PSW")

    def test_loose_mesh_requires_exact_full_source_identity(self):
        source = _mesh(3)
        mesh = parse_psx_mesh_bytes(source, logical_path="V1.PW3")

        self.assertEqual(mesh.body_size, len(source))
        self.assertEqual(
            mesh.body_sha256, hashlib.sha256(source).hexdigest())
        with self.assertRaisesRegex(
                PsxNativeAssetError, "expected exact body size"):
            parse_psx_mesh_bytes(
                source + b"unvalidated trailer", logical_path="V1.PW3")

    def test_archive_provenance_offset_must_be_sector_aligned(self):
        with self.assertRaisesRegex(
                PsxNativeAssetError, "archive offset.*sector aligned"):
            parse_psx_mesh_bytes(
                _mesh(3), logical_path="UNITMODL/UNIT.BIN",
                archive_ordinal=0, archive_offset=0x801)

    def test_invalid_vertex_index_fails_closed(self):
        with self.assertRaisesRegex(
                PsxNativeAssetError, "outside 0..3"):
            parse_psx_mesh_bytes(
                _mesh(3, bad_index=True), logical_path="bad.PW3")

    def test_unit_archive_accepts_marker_gap_and_preserves_ordinal(self):
        meshes = scan_unit_archive_bytes(_archive())

        self.assertEqual(len(meshes), 1)
        self.assertEqual(meshes[0].archive_ordinal, 0)
        self.assertEqual(meshes[0].archive_offset, 0x800)
        self.assertEqual(meshes[0].archive_sector, 1)

    def test_unit_archive_rejects_non_ba_allocation_padding(self):
        with self.assertRaisesRegex(
                PsxNativeAssetError, "padding.*not uniformly 0xBA"):
            scan_unit_archive_bytes(_archive(corrupt_padding=True))

    def test_unit_archive_requires_a_whole_number_of_sectors(self):
        with self.assertRaisesRegex(
                PsxNativeAssetError, "not a whole number.*sectors"):
            scan_unit_archive_bytes(_archive() + b"\xBA")

    def test_extracted_build_inventory_is_native_and_portable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "LISTS").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            (root / "LISTS" / "VEHICLE.TXT").write_bytes(
                b"Weasel\0Zeppelin\0")

            build = load_extracted_psx_build(root)
            identity = build.portable_identity

            self.assertEqual(len(build.meshes), 1)
            self.assertEqual(build.vehicle_roster, ("Weasel", "Zeppelin"))
            self.assertEqual(
                identity["source_container_kind"],
                "extracted_psx_disc_tree")
            self.assertEqual(identity["unit_archive_path"],
                             "UNITMODL/UNIT.BIN")
            self.assertNotIn(str(root), repr(identity))
            self.assertEqual(
                identity["name_binding_status"],
                "friendly_name_unmapped_roster")
            self.assertEqual(
                identity["model_slot_binding_status"],
                "unavailable_unproven")
            self.assertIsNone(identity["model_slot_evidence"])
            self.assertIsNone(build.model_slot_evidence)
            self.assertIsNone(build.meshes[0].model_slot)
            self.assertIsNone(build.meshes[0].model_slot_evidence_id)
            self.assertEqual(build.effects, ())
            self.assertEqual(identity["native_effect_count"], 0)
            self.assertEqual(identity["native_effects"], [])

    def test_extracted_build_binds_slots_only_from_exact_boot_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            executable = _boot_executable_with_allocation_table()
            archive = _archive()
            (root / "PSX.EXE").write_bytes(executable)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(archive)

            build = load_extracted_psx_build(root)
            mesh = build.meshes[0]
            evidence = build.model_slot_evidence
            portable = build.portable_identity

            # Dense mesh ordinal zero occupies executable allocation slot one;
            # slot zero is the archive's empty marker allocation.  This is the
            # compatibility distinction the runtime contract must preserve.
            self.assertEqual(PSX_NATIVE_PARSER_VERSION, 3)
            self.assertEqual(mesh.archive_ordinal, 0)
            self.assertEqual(mesh.model_slot, 1)
            self.assertEqual(
                mesh.model_slot_evidence_id,
                "psx_unit_bin_executable_allocation_table_v1")
            self.assertTrue(mesh.label.startswith(
                "model slot 001 | dense ordinal 000 | PW3 | "))
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence.empty_model_slots, (0,))
            self.assertEqual(evidence.trailing_sentinel_archive_sector, 2)
            self.assertEqual(evidence.executable_table_offset, 0x80)
            self.assertEqual(evidence.executable_table_entry_count, 2)
            self.assertEqual(
                evidence.unit_archive_sha256,
                hashlib.sha256(archive).hexdigest())
            self.assertEqual(
                evidence.boot_executable_sha256,
                hashlib.sha256(executable).hexdigest())
            self.assertEqual(
                evidence.executable_table_sha256,
                hashlib.sha256(executable[0x80:0x90]).hexdigest())
            self.assertEqual(
                portable["model_slot_binding_status"],
                "executable_allocation_table_proven")
            self.assertEqual(
                portable["model_slot_evidence"]["empty_model_slots"], [0])
            self.assertEqual(
                portable["model_slot_evidence"]
                ["trailing_sentinel_archive_sector"], 2)

    def test_extracted_build_inventories_only_immediate_recovered_pv2s(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "TEST_ART" / "nested").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            unit_source = _pv2(translation=(0, 0x25800, 0))
            test_source = _pv2(translation=(-0x25800, 0, 0))
            (root / "UNITMODL" / "Zeta.PV2").write_bytes(unit_source)
            (root / "TEST_ART" / "alpha.pv2").write_bytes(test_source)
            # Unknown sizes remain unavailable, and nested PV2s are outside
            # the explicitly bounded immediate-directory inventory.
            (root / "UNITMODL" / "UNKNOWN.PV2").write_bytes(b"unknown")
            (root / "TEST_ART" / "nested" / "IGNORED.PV2").write_bytes(
                _pv2())

            build = load_extracted_psx_build(root)
            portable = build.portable_identity

            self.assertEqual(
                tuple(effect.logical_path for effect in build.effects),
                ("TEST_ART/alpha.pv2", "UNITMODL/Zeta.PV2"))
            self.assertEqual(portable["native_effect_count"], 2)
            self.assertEqual(
                tuple(item["logical_path"]
                      for item in portable["native_effects"]),
                ("TEST_ART/alpha.pv2", "UNITMODL/Zeta.PV2"))
            for effect, item in zip(
                    build.effects, portable["native_effects"]):
                self.assertEqual(item["source_sha256"], effect.source_sha256)
                self.assertEqual(
                    item["vertex_stream_sha256"],
                    effect.vertex_stream_sha256)
                self.assertEqual(
                    item["face_stream_sha256"], effect.face_stream_sha256)
                self.assertEqual(item["parser_id"], PV2_PARSER_ID)
                self.assertEqual(item["parser_version"], PV2_PARSER_VERSION)
                self.assertEqual(item["binding_status"], PV2_BINDING_STATUS)
                self.assertNotIn("translation", item)
                self.assertNotIn(str(root), repr(item))

    def test_extracted_build_rejects_malformed_recovered_size_pv2(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "TEST_ART").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            (root / "TEST_ART" / "BAD.PV2").write_bytes(
                bytes(PV2_BODY_SIZE))

            with self.assertRaisesRegex(
                    PsxNativeAssetError,
                    "native PV2 validation failed.*unsupported PV2 version"):
                load_extracted_psx_build(root)

    def test_extracted_build_rejects_casefold_duplicate_pv2_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "TEST_ART").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            for name in ("STRASSE.PV2", "STRAßE.PV2"):
                (root / "TEST_ART" / name).write_bytes(_pv2())

            with self.assertRaisesRegex(
                    PsxNativeAssetError,
                    "ambiguous case-insensitive PV2 names"):
                load_extracted_psx_build(root)

    def test_extracted_build_rejects_reparse_pv2_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            effect_path = root / "UNITMODL" / "EFFECT.PV2"
            effect_path.write_bytes(_pv2())
            real_detector = psx_native_assets._is_reparse_point

            def simulated_reparse(candidate):
                candidate = Path(candidate)
                return candidate == effect_path or real_detector(candidate)

            with mock.patch(
                    "psx_native_assets._is_reparse_point",
                    side_effect=simulated_reparse):
                with self.assertRaisesRegex(
                        PsxNativeAssetError, "symlinks or junctions"):
                    load_extracted_psx_build(root)

    def test_discovery_is_bounded_to_the_approved_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            approved = Path(temporary) / "approved"
            inside = approved / "library" / "disc"
            outside = Path(temporary) / "outside"
            for root in (inside, outside):
                (root / "UNITMODL").mkdir(parents=True)
                (root / "SYSTEM.CNF").write_bytes(b"test")

            found = discover_extracted_psx_builds(approved, max_depth=4)

            self.assertEqual(found, (inside.resolve(),))
            self.assertNotIn(outside.resolve(), found)

    def test_build_root_requires_a_real_unitmodl_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            root.mkdir()
            (root / "SYSTEM.CNF").write_bytes(b"test")

            self.assertFalse(is_extracted_psx_build_root(root))

            (root / "UNITMODL").mkdir()
            self.assertTrue(is_extracted_psx_build_root(root))

    def test_boot_path_rejects_parent_traversal_out_of_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\..\\outside\\PSX.EXE;1\n",
                encoding="ascii")
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            (parent / "outside").mkdir()
            (parent / "outside" / "PSX.EXE").write_bytes(
                b"PS-X EXE" + b"\0" * 128)

            with self.assertRaisesRegex(
                    PsxNativeAssetError, "unsafe executable path"):
                load_extracted_psx_build(root)

    def test_casefold_ambiguous_boot_component_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\STRASSE\\PSX.EXE;1\n",
                encoding="ascii")
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            # These names are distinct on NTFS but both casefold to
            # ``strasse``, exercising ambiguity independently of the host's
            # ordinary case sensitivity.
            for name in ("STRASSE", "STRAßE"):
                (root / name).mkdir()
                (root / name / "PSX.EXE").write_bytes(
                    b"PS-X EXE" + b"\0" * 128)

            with self.assertRaisesRegex(
                    PsxNativeAssetError,
                    "ambiguous case-insensitive source entry"):
                load_extracted_psx_build(root)

    def test_reparse_source_component_fails_before_mesh_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            loose = root / "UNITMODL" / "V1.PW3"
            loose.write_bytes(_mesh(3))

            real_detector = psx_native_assets._is_reparse_point

            def simulated_reparse(candidate):
                candidate = Path(candidate)
                return (candidate == loose
                        or real_detector(candidate))

            # Creating Windows symlinks requires a host privilege that the
            # test runner intentionally does not assume.  Simulating the
            # detector's positive result still verifies the fail-closed load
            # boundary used for both symlinks and directory junctions.
            with mock.patch(
                    "psx_native_assets._is_reparse_point",
                    side_effect=simulated_reparse):
                with self.assertRaisesRegex(
                        PsxNativeAssetError, "symlinks or junctions"):
                    load_extracted_psx_build(root)

    def test_direct_native_files_reject_reparse_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mesh_source = root / "V1.PW3"
            archive_source = root / "UNIT.BIN"
            mesh_source.write_bytes(_mesh(3))
            archive_source.write_bytes(_archive())
            real_detector = psx_native_assets._is_reparse_point

            for source, loader in (
                    (mesh_source, parse_psx_mesh_file),
                    (archive_source, scan_unit_archive_file)):
                with self.subTest(source=source.name):
                    def simulated_reparse(candidate, source=source):
                        candidate = Path(candidate)
                        return (candidate == source
                                or real_detector(candidate))

                    with mock.patch(
                            "psx_native_assets._is_reparse_point",
                            side_effect=simulated_reparse):
                        with self.assertRaisesRegex(
                                PsxNativeAssetError,
                                "source path contains a symlink or junction"):
                            loader(source)

    def test_malformed_late_texture_pack_rejects_the_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "GFX").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            malformed = bytearray(LATE_SETGFX_SIZE)
            malformed[0] = 1
            (root / "GFX" / "SET1GFX.BIN").write_bytes(malformed)

            with self.assertRaisesRegex(
                    PsxNativeAssetError,
                    "native texture validation failed.*invalid header"):
                load_extracted_psx_build(root)

    def test_malformed_recognized_sector_pack_rejects_the_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "GFX").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            # 0xDA000 is a recognized December layout size, but zero-filled
            # allocation padding is not the required 0xBA corpus padding.
            (root / "GFX" / "SET1GFX.BIN").write_bytes(
                bytes(0xDA000))

            with self.assertRaisesRegex(
                    PsxNativeAssetError,
                    "native texture validation failed.*non-0xBA"):
                load_extracted_psx_build(root)

    def test_unknown_texture_pack_size_remains_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "disc"
            (root / "UNITMODL").mkdir(parents=True)
            (root / "GFX").mkdir()
            (root / "SYSTEM.CNF").write_text(
                "BOOT = cdrom:\\PSX.EXE;1\n", encoding="ascii")
            (root / "PSX.EXE").write_bytes(b"PS-X EXE" + b"\0" * 128)
            (root / "UNITMODL" / "UNIT.BIN").write_bytes(_archive())
            (root / "GFX" / "SET1GFX.BIN").write_bytes(
                b"unsupported layout" * 128)

            build = load_extracted_psx_build(root)

            self.assertEqual(build.texture_packs, ())
            self.assertEqual(
                build.portable_identity["native_texture_packs"], [])

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_complete_december_psw_psv_corpus_uv_domain(self):
        """Cover loose TEST/TEST_ART meshes outside the viewer inventory."""

        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        disc_root = (
            corpus / "technical" / "analysis" / "1998-12-18"
            / "work" / "disc_files")
        paths = tuple(sorted(
            (
                path for path in disc_root.rglob("*")
                if path.is_file()
                and path.suffix.casefold() in {".psw", ".psv"}
            ),
            key=lambda path: (
                path.relative_to(disc_root).as_posix().casefold(),
                path.relative_to(disc_root).as_posix(),
            ),
        ))
        meshes = tuple(
            parse_psx_mesh_file(
                path,
                logical_path=path.relative_to(disc_root).as_posix(),
            )
            for path in paths
        )
        components = tuple(
            component
            for mesh in meshes
            for face in mesh.faces
            for uv in face.psw_uv_fixed_16_16
            for component in uv
        )
        quotients = tuple(
            psw_material_local_uv_quotient(component)
            for component in components
        )
        histogram = json.dumps(
            sorted(Counter(quotients).items()),
            separators=(",", ":"),
        ).encode("ascii")

        self.assertEqual(len(paths), 33)
        self.assertEqual(
            tuple(
                path.relative_to(disc_root).as_posix()
                for path in paths
                if not path.relative_to(disc_root).as_posix().startswith(
                    "UNITMODL/")
            ),
            ("TEST/TANK.PSV", "TEST_ART/DEFAULT.PSV"),
        )
        self.assertEqual(sum(len(mesh.faces) for mesh in meshes), 1346)
        self.assertEqual(len(components), 10768)
        self.assertEqual(
            (min(components), max(components), sum(value < 0
                                                    for value in components)),
            (-1 << 16, 255 << 16, 2),
        )
        self.assertEqual(
            (min(quotients), max(quotients), len(set(quotients))),
            (0, 127, 128),
        )
        self.assertEqual(
            hashlib.sha256(histogram).hexdigest(),
            "16075b7b4bee941d42a09c15d471197f65a3f3f564264c85f79d5f49295bb162",
        )

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_local_representative_build_counts(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        # Exact hashes and offsets are the recovered executable allocation
        # tables; archive-only similarities are intentionally insufficient.
        slot_evidence = {
            "1999-03-12": (
                "53c40bb8ad160df689d89df69af102e986719e7138df90779e9197ae14856e70",
                "d5df80950cc0e639ef94eaed0ba85cbfc663fc9c5973147c1e67b826192b74db",
                0x5C818,
                300,
                "85708eb53a7bf2108b680b23553042a67a400a8c4cd47ce30e180ad357867376",
                False,
            ),
            "1999-05-14": (
                "28d4be27af2037d8ba9e72fda4577b56e18435ba86ee3d8e5184b733820b2fa2",
                "a27684b3a8c700776c86cbfdeb50166542d1088b752c04c10c9d4ffb268f4693",
                0x6D170,
                350,
                "170be4010dcb49778afb980336db5e0dd0c6bd34e9c1cafbb508a46b4443d8c6",
                True,
            ),
            "1999-06-15": (
                "ea9def3942ba20077d4c06591dc3acdb85d7641e47d728eeb653267947bae767",
                "5a70e6c970c08cc9825f7d11251ac5eb4633cc624ee6002bf549e15f6ef83a71",
                0x6E650,
                380,
                "89e93e088e9514c537969a8dd1ffc080d2435471efecedee2b629017a5d106e4",
                True,
            ),
        }
        effect_inventory = {
            "1998-12-18": (
                0, 30, 20,
                "4f34a3d101e328c7b701772c4cbb6a1bdfd290344df031bca557d2601bbdb7cd"),
            "1999-03-12": (
                2, 28, 20,
                "7c00b405d3b55181d069e959633ba758d21d14c6ba8bb94e35140e432ea9e80f"),
            "1999-05-14": (
                6, 28, 20,
                "60f284d0b0d6893357736ff60d23599d0a8db1de459cc1d38280063b541295dc"),
            "1999-06-15": (
                6, 28, 20,
                "60f284d0b0d6893357736ff60d23599d0a8db1de459cc1d38280063b541295dc"),
        }
        expected = {
            "1998-12-18": (
                31, 1, SECTOR_PADDED_SETGFX_LAYOUT_ID,
                SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING, 77, 30),
            "1999-03-12": (
                63, 6, SECTOR_PADDED_SETGFX_LAYOUT_ID,
                SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING, 78, 30),
            "1999-05-14": (
                91, 6, SECTOR_PADDED_SETGFX_LAYOUT_ID,
                SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING, 81, 34),
            # 138 packed models plus the shipped loose V56B.PW3.
            "1999-06-15": (
                139, 6, LATE_SETGFX_LAYOUT_ID,
                LATE_SELECTOR_TO_PIXEL_BANK_MAPPING, 128, 34),
        }
        for label, profile in expected.items():
            with self.subTest(label=label):
                (mesh_count, texture_pack_count, layout_id,
                 selector_mapping, populated_count, effect_count) = profile
                root = corpus / "technical" / "analysis" / label \
                    / "work" / "disc_files"
                build = load_extracted_psx_build(root)
                self.assertEqual(len(build.meshes), mesh_count)
                if label == "1998-12-18":
                    legacy_meshes = tuple(
                        mesh for mesh in build.meshes
                        if mesh.format_version == 1)
                    components = tuple(
                        component
                        for mesh in legacy_meshes
                        for face in mesh.faces
                        for uv in face.psw_uv_fixed_16_16
                        for component in uv
                    )
                    quotients = tuple(
                        psw_material_local_uv_quotient(component)
                        for component in components)
                    histogram = json.dumps(
                        sorted(Counter(quotients).items()),
                        separators=(",", ":"),
                    ).encode("ascii")

                    self.assertEqual(len(legacy_meshes), 31)
                    self.assertEqual(
                        sum(len(mesh.faces) for mesh in legacy_meshes),
                        1296)
                    self.assertEqual(len(components), 10368)
                    self.assertEqual(
                        (min(components), max(components)),
                        (-1 << 16, 255 << 16))
                    self.assertEqual(sum(value < 0 for value in components), 2)
                    self.assertEqual(
                        (min(quotients), max(quotients),
                         len(set(quotients))),
                        (0, 127, 128))
                    self.assertEqual(
                        hashlib.sha256(histogram).hexdigest(),
                        "86f0db6072fd8b57c5b2571058a8da99c398379326642f89f"
                        "8f2c1beb4409309")
                self.assertNotIn(
                    str(root), repr(build.portable_identity))
                evidence_profile = slot_evidence.get(label)
                if evidence_profile is None:
                    self.assertIsNone(build.model_slot_evidence)
                    self.assertEqual(
                        build.portable_identity["model_slot_binding_status"],
                        "unavailable_unproven")
                    self.assertTrue(all(
                        mesh.model_slot is None
                        and mesh.model_slot_evidence_id is None
                        for mesh in build.meshes))
                else:
                    (executable_sha, archive_sha, table_offset, entry_count,
                     table_sha, has_sentinel) = evidence_profile
                    evidence = build.model_slot_evidence
                    self.assertIsNotNone(evidence)
                    assert evidence is not None
                    self.assertEqual(
                        build.boot_executable_sha256, executable_sha)
                    self.assertEqual(build.unit_archive_sha256, archive_sha)
                    self.assertEqual(
                        evidence.boot_executable_sha256, executable_sha)
                    self.assertEqual(
                        evidence.unit_archive_sha256, archive_sha)
                    self.assertEqual(
                        evidence.executable_table_offset, table_offset)
                    self.assertEqual(
                        evidence.executable_table_entry_count, entry_count)
                    self.assertEqual(
                        evidence.executable_table_sha256, table_sha)
                    self.assertEqual(
                        evidence.trailing_sentinel_archive_sector is not None,
                        has_sentinel)
                    packed = tuple(
                        mesh for mesh in build.meshes
                        if mesh.archive_ordinal is not None)
                    self.assertEqual(
                        tuple(mesh.archive_ordinal for mesh in packed),
                        tuple(range(len(packed))))
                    self.assertEqual(
                        len({mesh.model_slot for mesh in packed}), len(packed))
                    self.assertTrue(all(
                        mesh.model_slot is not None
                        and mesh.model_slot_evidence_id == evidence.evidence_id
                        for mesh in packed))
                    self.assertTrue(all(
                        mesh.model_slot is None
                        and mesh.model_slot_evidence_id is None
                        for mesh in build.meshes
                        if mesh.archive_ordinal is None))
                self.assertEqual(len(build.effects), effect_count)
                self.assertEqual(
                    build.portable_identity["native_effect_count"],
                    effect_count)
                self.assertEqual(
                    len(build.portable_identity["native_effects"]),
                    effect_count)
                self.assertEqual(
                    tuple(effect.logical_path for effect in build.effects),
                    tuple(sorted(
                        (effect.logical_path for effect in build.effects),
                        key=lambda logical_path: (
                            logical_path.casefold(), logical_path))))
                (unit_effect_count, test_art_effect_count,
                 unique_effect_hashes, inventory_sha256) = (
                    effect_inventory[label])
                self.assertEqual(sum(
                    effect.logical_path.startswith("UNITMODL/")
                    for effect in build.effects), unit_effect_count)
                self.assertEqual(sum(
                    effect.logical_path.startswith("TEST_ART/")
                    for effect in build.effects), test_art_effect_count)
                self.assertEqual(
                    len({effect.source_sha256 for effect in build.effects}),
                    unique_effect_hashes)
                manifest = b"".join(
                    effect.logical_path.encode("utf-8")
                    + b"\0"
                    + effect.source_sha256.encode("ascii")
                    + b"\n"
                    for effect in build.effects)
                self.assertEqual(
                    hashlib.sha256(manifest).hexdigest(), inventory_sha256)
                for effect, portable_effect in zip(
                        build.effects,
                        build.portable_identity["native_effects"]):
                    self.assertEqual(
                        portable_effect["logical_path"], effect.logical_path)
                    self.assertEqual(
                        portable_effect["source_sha256"],
                        effect.source_sha256)
                    self.assertEqual(
                        portable_effect["parser_id"], PV2_PARSER_ID)
                    self.assertEqual(
                        portable_effect["parser_version"],
                        PV2_PARSER_VERSION)
                    self.assertEqual(
                        portable_effect["binding_status"],
                        "static_effect_mesh_unbound")
                    self.assertNotIn(str(root), repr(portable_effect))
                self.assertEqual(
                    len(build.texture_packs), texture_pack_count)
                self.assertEqual(
                    len(build.portable_identity["native_texture_packs"]),
                    texture_pack_count,
                )
                for pack, portable in zip(
                        build.texture_packs,
                        build.portable_identity["native_texture_packs"]):
                    self.assertEqual(pack.layout_id, layout_id)
                    self.assertEqual(
                        pack.selector_to_pixel_bank_mapping,
                        selector_mapping)
                    self.assertEqual(
                        pack.material_slot_count, populated_count)
                    self.assertEqual(portable["profile"], layout_id)
                    self.assertEqual(portable["layout_id"], layout_id)
                    self.assertEqual(
                        portable["selector_to_pixel_bank_mapping"],
                        selector_mapping)
                    self.assertEqual(
                        portable["populated_selector_count"],
                        populated_count)
                    self.assertEqual(
                        portable["populated_selectors"],
                        list(pack.populated_selectors))
                    self.assertTrue(
                        portable["logical_path"].startswith("GFX/SET"))
                    self.assertTrue(all(
                        type(selector) is int
                        and 0 <= selector < 128
                        for selector in portable["populated_selectors"]))


if __name__ == "__main__":
    unittest.main()
