from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from psx_native_assets import parse_psx_mesh_bytes, parse_psx_mesh_file
import psx_native_identity
from psx_native_identity import (
    JUNE_V56B_MAIN_LOAD_FILE_OFFSET,
    JUNE_V56B_NEAR_VIEW_METRIC_MAX,
    JUNE_V56B_OVERLAY_SELECT_FILE_OFFSET,
    PSX_MODEL_SLOT_EVIDENCE_ID,
    PsxNativeIdentityError,
    identify_june_v56b_override,
    identify_unit_archive_slots_bytes,
    identify_unit_archive_slots_file,
    loose_model_identity,
    match_mesh_lineage,
    mesh_geometry_sha256,
    mesh_semantic_sha256,
    require_loose_model_identity,
)


SECTOR_SIZE = 0x800
MAGIC = bytes.fromhex("4e0d0a1a")


def _pw3_mesh(
        *, vertex_count: int = 4, face_count: int = 1,
        uv_bias: int = 0) -> bytes:
    vertices = tuple(
        (index << 16, (index % 3) << 16, -(index << 15))
        for index in range(vertex_count)
    )
    records = []
    for face_index in range(face_count):
        record = bytearray(26)
        indices = (
            face_index % vertex_count,
            (face_index + 1) % vertex_count,
            (face_index + 2) % vertex_count,
            (face_index + 3) % vertex_count,
        )
        # Four distinct vertices are needed when vertex_count permits it.  A
        # repeated last corner is the native triangle convention otherwise.
        if len(set(indices)) < 3:
            indices = (0, 1, 2, 2)
        struct.pack_into("<H", record, 0, 0x4000)
        struct.pack_into("<4H", record, 4, *indices)
        record[12:20] = bytes(
            ((value + uv_bias) & 0xFF)
            for value in (1, 2, 3, 4, 5, 6, 7, 8))
        struct.pack_into("<H", record, 20, 3)
        record[22:26] = bytes((20, 30, 40, 50))
        records.append(bytes(record))
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, vertex_count, face_count)
    struct.pack_into(
        "<II", header, 0x40, 80, 80 + vertex_count * 12)
    unaligned = (
        bytes(header)
        + b"".join(struct.pack("<iii", *vertex) for vertex in vertices)
        + b"".join(records)
    )
    return unaligned + b"\0" * ((-len(unaligned)) & 3)


def _empty_sector() -> bytes:
    return MAGIC + b"\xBA" * (SECTOR_SIZE - len(MAGIC))


def _archive_and_table(
        allocations: tuple[bytes | None, ...], *,
        include_final_sentinel: bool = False) -> tuple[bytes, bytes]:
    archive = bytearray()
    entries: list[tuple[int, int]] = []
    for body in allocations:
        start_sector = len(archive) // SECTOR_SIZE
        if body is None:
            archive.extend(_empty_sector())
            entries.append((start_sector, 4))
            continue
        allocation_size = (
            (len(body) + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE)
        archive.extend(body)
        archive.extend(b"\xBA" * (allocation_size - len(body)))
        entries.append((start_sector, len(body)))
    if include_final_sentinel:
        archive.extend(_empty_sector())
    table = b"".join(struct.pack("<II", *entry) for entry in entries)
    return bytes(archive), table


def _fake_executable(table: bytes, *, copies: int = 1) -> bytes:
    return (
        b"PS-X EXE"
        + b"\xCC" * 120
        + (table + b"\xCD" * 32) * copies
    )


def _analysis_root(corpus: Path) -> Path | None:
    candidates = (
        corpus / "technical" / "analysis",
        corpus / "analysis",
        corpus,
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


class PsxNativeIdentityTests(unittest.TestCase):
    def test_loose_filename_identity_is_numeric_asset_evidence_only(self):
        identity = require_loose_model_identity("UNITMODL\\v56b.pw3")
        self.assertEqual(identity.logical_path, "UNITMODL/v56b.pw3")
        self.assertEqual(identity.asset_number, 56)
        self.assertEqual(identity.variant, "B")
        self.assertEqual(identity.asset_key, "V56B")
        self.assertEqual(identity.extension, ".PW3")
        self.assertEqual(identity.evidence_basis, "loose_filename_v_numeric")

        self.assertEqual(
            require_loose_model_identity("UNITMODL/V272.PSW").asset_number,
            272)
        for path in (
                "UNITMODL/VP_HUBI1.PSW",
                "TEST_ART/DEFAULT.PW3",
                "UNITMODL/V056.PSW",
                "UNITMODL/V0.PSW",
                "UNITMODL/V56B.PV2"):
            with self.subTest(path=path):
                self.assertIsNone(loose_model_identity(path))
        with self.assertRaisesRegex(PsxNativeIdentityError, "normalized"):
            loose_model_identity("UNITMODL/../V1.PSW")
        with self.assertRaisesRegex(PsxNativeIdentityError, "not a strict"):
            require_loose_model_identity("UNITMODL/DEFAULT.PW3")

    def test_executable_table_preserves_dense_ordinal_and_exposes_slots(self):
        first = _pw3_mesh(uv_bias=0)
        second = _pw3_mesh(uv_bias=1)
        archive, table = _archive_and_table(
            (None, first, None, second), include_final_sentinel=True)
        identity = identify_unit_archive_slots_bytes(
            archive,
            executable_bytes=_fake_executable(table),
        )

        self.assertEqual(identity.evidence_id, PSX_MODEL_SLOT_EVIDENCE_ID)
        self.assertEqual(identity.executable_table_offset, 128)
        self.assertEqual(identity.executable_table_entry_count, 4)
        self.assertEqual(len(identity.allocations), 5)
        self.assertEqual(identity.model_slots, (1, 3))
        self.assertEqual(
            tuple(mesh.archive_ordinal for mesh in identity.meshes), (0, 1))
        self.assertEqual(identity.mesh_for_model_slot(1).archive_ordinal, 0)
        self.assertEqual(identity.mesh_for_model_slot(3).archive_ordinal, 1)
        self.assertEqual(
            identity.allocation_for_model_slot(2).kind, "empty_model_slot")
        with self.assertRaisesRegex(PsxNativeIdentityError, "empty placeholder"):
            identity.mesh_for_model_slot(2)
        self.assertIsNotNone(identity.trailing_sentinel)
        assert identity.trailing_sentinel is not None
        self.assertEqual(identity.trailing_sentinel.allocation_index, 4)
        self.assertIsNone(identity.trailing_sentinel.model_slot)
        with self.assertRaises(FrozenInstanceError):
            identity.executable_table_offset = 0  # type: ignore[misc]

    def test_final_marker_is_a_slot_when_the_executable_table_contains_it(self):
        archive, table = _archive_and_table((None, _pw3_mesh(), None))
        identity = identify_unit_archive_slots_bytes(
            archive, executable_bytes=_fake_executable(table))
        self.assertIsNone(identity.trailing_sentinel)
        self.assertEqual(
            identity.allocation_for_model_slot(2).kind, "empty_model_slot")

    def test_sentinel_classification_fails_closed_without_exact_evidence(self):
        archive, table = _archive_and_table(
            (None, _pw3_mesh()), include_final_sentinel=True)
        with self.assertRaisesRegex(
                PsxNativeIdentityError, "remain unproven"):
            identify_unit_archive_slots_bytes(
                archive,
                executable_bytes=_fake_executable(table[:-8]),
            )
        with self.assertRaisesRegex(PsxNativeIdentityError, "ambiguous"):
            identify_unit_archive_slots_bytes(
                archive,
                executable_bytes=_fake_executable(table, copies=2),
            )
        with self.assertRaisesRegex(PsxNativeIdentityError, "PS-X EXE"):
            identify_unit_archive_slots_bytes(
                archive, executable_bytes=b"not an executable" + table)

    def test_exact_sector_aligned_body_needs_no_padding_bytes(self):
        # 80 + 34*12 + 60*26 = exactly 0x800 bytes.
        body = _pw3_mesh(vertex_count=34, face_count=60)
        self.assertEqual(len(body), SECTOR_SIZE)
        archive, table = _archive_and_table((None, body))
        identity = identify_unit_archive_slots_bytes(
            archive, executable_bytes=_fake_executable(table))
        self.assertEqual(identity.mesh_for_model_slot(1).body_size, SECTOR_SIZE)

    def test_direct_evidence_files_reject_reparse_ancestors_before_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            redirected_parent = root / "redirected-source"
            redirected_parent.mkdir()
            archive, table = _archive_and_table((None, _pw3_mesh()))
            archive_path = redirected_parent / "UNIT.BIN"
            executable_path = redirected_parent / "PSX.EXE"
            archive_path.write_bytes(archive)
            executable_path.write_bytes(_fake_executable(table))
            real_detector = psx_native_identity._is_reparse_point

            def simulated_reparse(candidate: Path) -> bool:
                return (
                    Path(candidate) == redirected_parent
                    or real_detector(Path(candidate))
                )

            with patch.object(
                    psx_native_identity, "_is_reparse_point",
                    side_effect=simulated_reparse):
                with self.assertRaisesRegex(
                        PsxNativeIdentityError,
                        "symlink, junction, or reparse point"):
                    identify_unit_archive_slots_file(
                        archive_path, executable_path)

    def test_normalized_lineage_collision_never_becomes_a_binding(self):
        anchor = parse_psx_mesh_bytes(
            _pw3_mesh(), logical_path="UNITMODL/V90.PW3")
        same_a = parse_psx_mesh_bytes(
            _pw3_mesh(), logical_path="UNITMODL/UNIT.BIN")
        same_b = parse_psx_mesh_bytes(
            _pw3_mesh(), logical_path="UNITMODL/UNIT.BIN")
        changed = parse_psx_mesh_bytes(
            _pw3_mesh(uv_bias=1), logical_path="UNITMODL/UNIT.BIN")
        match = match_mesh_lineage(
            anchor, {90: same_a, 91: same_b, 92: changed})
        self.assertEqual(match.status, "exact_semantic_ambiguous")
        self.assertEqual(match.candidate_model_slots, (90, 91))
        self.assertIsNone(match.proven_model_slot)
        self.assertEqual(
            mesh_geometry_sha256(anchor), mesh_geometry_sha256(changed))
        self.assertNotEqual(
            mesh_semantic_sha256(anchor), mesh_semantic_sha256(changed))

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_canonical_executable_tables_and_cross_build_anchors(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        analysis = _analysis_root(corpus)
        self.assertIsNotNone(analysis)
        assert analysis is not None

        # Exact source and table hashes are evidence sentinels.  Parsing is
        # read-only and the before/after checks protect recovered source bytes.
        expected = {
            "1999-03-12": {
                "boot": "PSX.EXE",
                "exe_sha": "53c40bb8ad160df689d89df69af102e986719e7138df90779e9197ae14856e70",
                "unit_sha": "d5df80950cc0e639ef94eaed0ba85cbfc663fc9c5973147c1e67b826192b74db",
                "table_offset": 0x5C818,
                "table_count": 300,
                "table_sha": "85708eb53a7bf2108b680b23553042a67a400a8c4cd47ce30e180ad357867376",
                "mesh_count": 63,
                "sentinel": None,
            },
            "1999-05-14": {
                "boot": "PSX.EXE",
                "exe_sha": "28d4be27af2037d8ba9e72fda4577b56e18435ba86ee3d8e5184b733820b2fa2",
                "unit_sha": "a27684b3a8c700776c86cbfdeb50166542d1088b752c04c10c9d4ffb268f4693",
                "table_offset": 0x6D170,
                "table_count": 350,
                "table_sha": "170be4010dcb49778afb980336db5e0dd0c6bd34e9c1cafbb508a46b4443d8c6",
                "mesh_count": 91,
                "sentinel": 350,
            },
            "1999-06-16": {
                "boot": "SCES_019.63",
                "exe_sha": "ea9def3942ba20077d4c06591dc3acdb85d7641e47d728eeb653267947bae767",
                "unit_sha": "5a70e6c970c08cc9825f7d11251ac5eb4633cc624ee6002bf549e15f6ef83a71",
                "table_offset": 0x6E650,
                "table_count": 380,
                "table_sha": "89e93e088e9514c537969a8dd1ffc080d2435471efecedee2b629017a5d106e4",
                "mesh_count": 138,
                "sentinel": 380,
            },
        }
        identified = {}
        source_hashes = {}
        for build, profile in expected.items():
            with self.subTest(build=build):
                root = analysis / build / "work" / "disc_files"
                archive = root / "UNITMODL" / "UNIT.BIN"
                executable = root / profile["boot"]
                source_hashes[archive] = hashlib.sha256(
                    archive.read_bytes()).hexdigest()
                source_hashes[executable] = hashlib.sha256(
                    executable.read_bytes()).hexdigest()
                self.assertEqual(source_hashes[archive], profile["unit_sha"])
                self.assertEqual(source_hashes[executable], profile["exe_sha"])

                result = identify_unit_archive_slots_file(
                    archive, executable,
                    logical_path="UNITMODL/UNIT.BIN")
                identified[build] = result
                self.assertEqual(
                    result.executable_table_offset, profile["table_offset"])
                self.assertEqual(
                    result.executable_table_entry_count,
                    profile["table_count"])
                self.assertEqual(
                    result.executable_table_sha256, profile["table_sha"])
                self.assertEqual(len(result.meshes), profile["mesh_count"])
                sentinel = result.trailing_sentinel
                self.assertEqual(
                    None if sentinel is None else sentinel.allocation_index,
                    profile["sentinel"])

        march = identified["1999-03-12"]
        december = analysis / "1998-12-18" / "work" / "disc_files" \
            / "UNITMODL"
        candidates = {
            allocation.model_slot: allocation.mesh
            for allocation in march.allocations
            if allocation.model_slot is not None and allocation.mesh is not None
        }
        v22_path = december / "V22.PSW"
        v90_path = december / "V90.PSW"
        v91_path = december / "V91.PSW"
        v11_path = december / "V11.PSW"
        for path in (v22_path, v90_path, v91_path, v11_path):
            source_hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()

        # Exact normalized semantics corroborate the runtime slot table.
        v22 = match_mesh_lineage(parse_psx_mesh_file(v22_path), candidates)
        self.assertEqual(v22.status, "exact_semantic_unique")
        self.assertEqual(v22.proven_model_slot, 22)

        # Negative controls: filename V90 collides across four runtime slots;
        # V91 is carried at slot 94, so filename number != universal slot id.
        v90 = match_mesh_lineage(parse_psx_mesh_file(v90_path), candidates)
        self.assertEqual(v90.status, "exact_semantic_ambiguous")
        self.assertEqual(v90.candidate_model_slots, (90, 91, 92, 93))
        self.assertIsNone(v90.proven_model_slot)
        v91 = match_mesh_lineage(parse_psx_mesh_file(v91_path), candidates)
        self.assertEqual(v91.status, "exact_semantic_unique")
        self.assertEqual(v91.proven_model_slot, 94)

        # V11 retained geometry but changed semantic face fields.  Geometry
        # similarity is reported and intentionally never promoted to binding.
        v11 = match_mesh_lineage(parse_psx_mesh_file(v11_path), candidates)
        self.assertEqual(v11.status, "geometry_only_unique")
        self.assertEqual(v11.candidate_model_slots, (11,))
        self.assertIsNone(v11.proven_model_slot)

        for path, before in source_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_canonical_v56b_is_a_near_view_override_not_animation(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        analysis = _analysis_root(corpus)
        self.assertIsNotNone(analysis)
        assert analysis is not None
        root = analysis / "1999-06-16" / "work" / "disc_files"
        executable = root / "SCES_019.63"
        overlay = root / "OVER1.TXT"
        asset = root / "UNITMODL" / "V56B.PW3"
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (executable, overlay, asset)
        }
        self.assertEqual(
            before[asset],
            "3bd8e691042ce9670e8104fc4f397c260d1a89ffd96dc9e15697fa2879b0734f")
        evidence = identify_june_v56b_override(
            executable_bytes=executable.read_bytes(),
            overlay_bytes=overlay.read_bytes(),
            asset_bytes=asset.read_bytes(),
            logical_path="UNITMODL/V56B.PW3",
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.model_slot, 56)
        self.assertEqual(
            evidence.selection_kind,
            "near_view_metric_conditional_model_override")
        self.assertEqual(
            evidence.near_view_metric,
            "shifted_x_delta_squared_plus_twice_shifted_z_delta_squared")
        self.assertEqual(
            evidence.near_view_metric_max_inclusive,
            JUNE_V56B_NEAR_VIEW_METRIC_MAX)
        self.assertEqual(JUNE_V56B_NEAR_VIEW_METRIC_MAX, 99_999)
        self.assertEqual(
            evidence.main_load_file_offset,
            JUNE_V56B_MAIN_LOAD_FILE_OFFSET)
        self.assertEqual(JUNE_V56B_MAIN_LOAD_FILE_OFFSET, 0x54614)
        self.assertEqual(
            evidence.overlay_select_file_offset,
            JUNE_V56B_OVERLAY_SELECT_FILE_OFFSET)
        self.assertEqual(JUNE_V56B_OVERLAY_SELECT_FILE_OFFSET, 0xF738)

        # Exact opcode-range hashes pin the load and conditional substitution
        # evidence without requiring a disassembler in the public test suite.
        main_chunk = executable.read_bytes()[0x54614:0x54614 + 96]
        overlay_chunk = overlay.read_bytes()[0xF738:0xF738 + 56]
        self.assertEqual(
            hashlib.sha256(main_chunk).hexdigest(),
            "093400237b26afddd45b3e9a1f35c5c979c8e66dd0b5b772cc35c1328b1c7ded")
        self.assertEqual(
            hashlib.sha256(overlay_chunk).hexdigest(),
            "592faf8e73f44ab54014b84da2ff86b0bb77c8b5ba305bb73b3474e9a1e1a044")

        mutated = bytearray(asset.read_bytes())
        mutated[-1] ^= 1
        self.assertIsNone(identify_june_v56b_override(
            executable_bytes=executable.read_bytes(),
            overlay_bytes=overlay.read_bytes(),
            asset_bytes=bytes(mutated),
            logical_path="UNITMODL/V56B.PW3",
        ))
        self.assertIsNone(identify_june_v56b_override(
            executable_bytes=executable.read_bytes(),
            overlay_bytes=overlay.read_bytes(),
            asset_bytes=asset.read_bytes(),
            logical_path="UNITMODL/V56.PW3",
        ))
        for path, source_hash in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source_hash)


if __name__ == "__main__":
    unittest.main()
