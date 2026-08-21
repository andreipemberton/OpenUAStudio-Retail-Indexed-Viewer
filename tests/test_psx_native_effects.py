from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import struct
import unittest

from psx_native_assets import scan_unit_archive_file
from psx_native_effects import (
    PV2_BINDING_STATUS,
    PV2_BODY_SIZE,
    PV2_FACE_POINTER_RESIDUE,
    PV2_PARSER_ID,
    PV2_VERTEX_POINTER_RESIDUE,
    PsxNativeEffectError,
    parse_pv2_bytes,
    parse_pv2_file,
    parse_recovered_pv2_candidate_bytes,
)


def _pv2() -> bytes:
    header = bytearray(32)
    struct.pack_into("<I", header, 0, 1)
    struct.pack_into("<iii", header, 4, 0, 0x25800, 0)
    struct.pack_into(
        "<4I", header, 0x10,
        10, 5, PV2_VERTEX_POINTER_RESIDUE, PV2_FACE_POINTER_RESIDUE)
    vertices = tuple(
        ((index - 5) << 16, ((index % 3) - 1) << 16, index & 1)
        for index in range(10)
    )
    faces = []
    indices = (
        (0, 1, 2, 2),
        (2, 3, 4, 5),
        (4, 5, 6, 7),
        (6, 7, 8, 8),
        (8, 9, 0, 1),
    )
    for face_index, corners in enumerate(indices):
        record = bytearray(40)
        if face_index in (0, 3):
            record[:4] = b"\x00\x20\x00\x00"
        struct.pack_into("<4H", record, 4, *corners)
        struct.pack_into(
            "<8H", record, 12,
            1, 2, 3, 4, 5, 6, 7, 8)
        struct.pack_into("<I", record, 28, 3)
        struct.pack_into("<4H", record, 32, 78, 78, 78, 78)
        faces.append(bytes(record))
    body = (
        bytes(header)
        + b"".join(struct.pack("<iii", *vertex) for vertex in vertices)
        + b"".join(faces)
    )
    assert len(body) == PV2_BODY_SIZE
    return body


def _analysis_root(corpus: Path) -> Path | None:
    candidates = (
        corpus / "technical" / "analysis",
        corpus / "analysis",
        corpus,
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


class PsxNativeEffectsTests(unittest.TestCase):
    def test_strict_static_pv2_decode_preserves_opaque_fields(self):
        source = _pv2()
        mesh = parse_pv2_bytes(
            source, logical_path="TEST_ART\\SLRPH00.PV2")
        self.assertEqual(mesh.logical_path, "TEST_ART/SLRPH00.PV2")
        self.assertEqual(mesh.format_id, "PV2")
        self.assertEqual(mesh.format_version, 1)
        self.assertEqual(mesh.parser_id, PV2_PARSER_ID)
        self.assertEqual(mesh.binding_status, PV2_BINDING_STATUS)
        self.assertEqual(mesh.body_size, 352)
        self.assertEqual(mesh.source_sha256, hashlib.sha256(source).hexdigest())
        self.assertEqual(mesh.raw_translation, (0, 0x25800, 0))
        self.assertEqual(mesh.translation, (0.0, 2.34375, 0.0))
        self.assertEqual(mesh.vertex_pointer_residue, 0x00427C20)
        self.assertEqual(mesh.face_pointer_residue, 0x00427C98)
        self.assertEqual(len(mesh.raw_vertices), 10)
        self.assertEqual(len(mesh.faces), 5)
        self.assertEqual(mesh.faces[0].opaque_prefix, b"\x00\x20\x00\x00")
        self.assertEqual(mesh.faces[0].raw_vertex_indices, (0, 1, 2, 2))
        self.assertEqual(mesh.faces[0].vertex_indices, (0, 1, 2))
        self.assertEqual(mesh.faces[0].raw_uv_values[0], (1, 2))
        self.assertEqual(mesh.faces[0].texture_selector, 3)
        self.assertEqual(mesh.faces[0].raw_corner_shades, (78, 78, 78, 78))
        self.assertEqual(
            mesh.portable_identity["binding_status"],
            "static_effect_mesh_unbound")
        self.assertIn("PV2 static effect", mesh.label)
        with self.assertRaises(FrozenInstanceError):
            mesh.binding_status = "animated"  # type: ignore[misc]

    def test_pv2_rejects_every_unproven_layout_extension(self):
        mutations = {}

        truncated = bytearray(_pv2())
        mutations["exactly 352"] = bytes(truncated[:-1])

        wrong_version = bytearray(_pv2())
        struct.pack_into("<I", wrong_version, 0, 2)
        mutations["unsupported PV2 version"] = bytes(wrong_version)

        wrong_count = bytearray(_pv2())
        struct.pack_into("<I", wrong_count, 0x10, 9)
        mutations["PV2 counts"] = bytes(wrong_count)

        wrong_vertex_pointer = bytearray(_pv2())
        struct.pack_into("<I", wrong_vertex_pointer, 0x18, 0x00427C24)
        mutations["vertex-pointer residue"] = bytes(wrong_vertex_pointer)

        wrong_face_pointer = bytearray(_pv2())
        struct.pack_into("<I", wrong_face_pointer, 0x1C, 0x00427C9C)
        mutations["face-pointer residue"] = bytes(wrong_face_pointer)

        face_offset = 32 + 10 * 12
        wrong_prefix = bytearray(_pv2())
        wrong_prefix[face_offset:face_offset + 4] = b"\x01\x00\x00\x00"
        mutations["opaque prefix"] = bytes(wrong_prefix)

        wrong_index = bytearray(_pv2())
        struct.pack_into("<H", wrong_index, face_offset + 4, 10)
        mutations["vertex index 10"] = bytes(wrong_index)

        wrong_uv = bytearray(_pv2())
        struct.pack_into("<H", wrong_uv, face_offset + 12, 256)
        mutations["non-narrowable PV2 UV"] = bytes(wrong_uv)

        wrong_selector = bytearray(_pv2())
        struct.pack_into("<I", wrong_selector, face_offset + 28, 128)
        mutations["selector 128"] = bytes(wrong_selector)

        wrong_shade = bytearray(_pv2())
        struct.pack_into("<H", wrong_shade, face_offset + 32, 256)
        mutations["non-narrowable PV2 corner shade"] = bytes(wrong_shade)

        collapsed = bytearray(_pv2())
        struct.pack_into("<4H", collapsed, face_offset + 4, 0, 0, 0, 0)
        mutations["unique corners"] = bytes(collapsed)

        for message, candidate in mutations.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(PsxNativeEffectError, message):
                    parse_pv2_bytes(candidate, logical_path="BAD.PV2")

        with self.assertRaisesRegex(PsxNativeEffectError, "nonempty"):
            parse_pv2_bytes(_pv2(), logical_path="bad\0path.PV2")

    def test_recovered_size_candidate_policy_and_logical_path_sanitizing(self):
        self.assertIsNone(parse_recovered_pv2_candidate_bytes(
            b"unknown PV2 layout", logical_path="TEST_ART/UNKNOWN.PV2"))

        malformed = bytearray(_pv2())
        struct.pack_into("<I", malformed, 0, 2)
        with self.assertRaisesRegex(
                PsxNativeEffectError, "unsupported PV2 version"):
            parse_recovered_pv2_candidate_bytes(
                bytes(malformed), logical_path="TEST_ART/BAD.PV2")

        for logical_path in (
                "../BAD.PV2", "/BAD.PV2", "C:/BAD.PV2",
                "TEST_ART//BAD.PV2"):
            with self.subTest(logical_path=logical_path):
                with self.assertRaisesRegex(
                        PsxNativeEffectError,
                        "normalized and relative"):
                    parse_pv2_bytes(
                        _pv2(), logical_path=logical_path)

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_canonical_pv2_census_hashes_and_no_packed_ordinal_binding(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        analysis = _analysis_root(corpus)
        self.assertIsNotNone(analysis)
        assert analysis is not None

        pv2_paths = tuple(sorted(
            analysis.rglob("*.PV2"), key=lambda path: str(path).casefold()))
        self.assertEqual(len(pv2_paths), 264)
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in pv2_paths
        }
        self.assertEqual(len(set(before.values())), 20)
        parsed = tuple(parse_pv2_file(path) for path in pv2_paths)
        self.assertTrue(all(mesh.body_size == 352 for mesh in parsed))
        self.assertTrue(all(len(mesh.vertices) == 10 for mesh in parsed))
        self.assertTrue(all(len(mesh.faces) == 5 for mesh in parsed))
        self.assertTrue(all(
            mesh.binding_status == "static_effect_mesh_unbound"
            for mesh in parsed))

        june = analysis / "1999-06-16" / "work" / "disc_files"
        horizontal_path = june / "TEST_ART" / "SLRPH01.PV2"
        vertical_path = june / "TEST_ART" / "SLRPV22.PV2"
        horizontal_alias = june / "UNITMODL" / "SLRPH04.PV2"
        vertical_alias = june / "UNITMODL" / "SLURPV.PV2"
        expected_hashes = {
            horizontal_path: "5bbf8a5e8375758a7f9136532928b3d43a378073b7ad492936ac82e04ceed41c",
            vertical_path: "4b89e8c3e7ebdcf47087d73ad329f1850659858d34bc0f9feb66960e16033fcf",
            horizontal_alias: "5bbf8a5e8375758a7f9136532928b3d43a378073b7ad492936ac82e04ceed41c",
            vertical_alias: "4b89e8c3e7ebdcf47087d73ad329f1850659858d34bc0f9feb66960e16033fcf",
        }
        for path, expected_hash in expected_hashes.items():
            self.assertEqual(before[path], expected_hash)

        horizontal = parse_pv2_file(horizontal_path)
        vertical = parse_pv2_file(vertical_path)
        self.assertEqual(horizontal.raw_translation, (0, 0x25800, 0))
        self.assertEqual(vertical.raw_translation, (-0x25800, 0, 0))
        self.assertEqual(
            (min(v[0] for v in horizontal.raw_vertices),
             max(v[0] for v in horizontal.raw_vertices)),
            (-39321600, 39321600))
        self.assertEqual(
            (min(v[1] for v in vertical.raw_vertices),
             max(v[1] for v in vertical.raw_vertices)),
            (-39321600, 39321600))

        unit_path = june / "UNITMODL" / "UNIT.BIN"
        unit_before = hashlib.sha256(unit_path.read_bytes()).hexdigest()
        self.assertEqual(
            unit_before,
            "5a70e6c970c08cc9825f7d11251ac5eb4633cc624ee6002bf549e15f6ef83a71")
        packed = scan_unit_archive_file(unit_path)
        packed_body_hashes = {mesh.body_sha256 for mesh in packed}
        packed_vertex_hashes = {mesh.vertex_stream_sha256 for mesh in packed}
        packed_geometry = {
            (
                mesh.raw_vertices,
                tuple(face.raw_vertex_indices for face in mesh.faces),
            )
            for mesh in packed
        }
        unit_bytes = unit_path.read_bytes()
        for path in (horizontal_path, vertical_path):
            mesh = parse_pv2_file(path)
            self.assertNotIn(mesh.source_sha256, packed_body_hashes)
            self.assertNotIn(mesh.vertex_stream_sha256, packed_vertex_hashes)
            self.assertNotIn(
                (
                    mesh.raw_vertices,
                    tuple(face.raw_vertex_indices for face in mesh.faces),
                ),
                packed_geometry,
            )
            # No PV2 body or path record is embedded in UNIT.BIN.  The June
            # executable carries PV2 paths separately, outside ordinal data.
            self.assertEqual(unit_bytes.find(path.read_bytes()), -1)
            self.assertEqual(
                unit_bytes.lower().find(path.name.encode("ascii").lower()), -1)

        self.assertEqual(hashlib.sha256(unit_path.read_bytes()).hexdigest(), unit_before)
        for path, source_hash in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source_hash)


if __name__ == "__main__":
    unittest.main()
