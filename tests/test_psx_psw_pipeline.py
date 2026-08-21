from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import struct
import unittest

from psx_native_assets import parse_psx_mesh_file, scan_unit_archive_file
from psx_native_identity import mesh_semantic_sha256
from psx_psw_pipeline import (
    PSX_POLY_GT4_OPAQUE_COMMAND,
    PSX_POLY_GT4_PACKET_SIZE,
    PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
    PSX_PSW_PIPELINE_EVIDENCE,
    PSX_PSW_PIPELINE_PROFILE_ID,
    PSX_PSW_VIEWER_INTEGRATION_STATE,
    PSW_AUTHORED_TO_GT4_ORDER,
    PSW_FACE_SIZE,
    PSW_NCLIP_AUTHORED_ORDER,
    PSW_NCLIP_POLICY,
    PSW_PREFIX_WORD4_STATE,
    PSW_PRIMITIVE_FLAGS_BIT13,
    PSW_PRIMITIVE_FLAGS_STATE,
    PSW_TEXTURE_DESCRIPTOR_SIZE,
    UA_DECEMBER_MAIN_EXE_SHA256,
    UA_DECEMBER_OVER1_LOAD_ADDRESS,
    UA_DECEMBER_OVER1_SHA256,
    UA_DECEMBER_PSW_FACE_LOOP_ADDRESS_RANGE,
    UA_DECEMBER_PSW_FACE_LOOP_FILE_RANGE,
    UA_DECEMBER_PSW_FACE_LOOP_RANGE_SHA256,
    UA_DECEMBER_PSW_FOURTH_RTPS_ADDRESS,
    UA_DECEMBER_PSW_FOURTH_RTPS_FILE_OFFSET,
    UA_DECEMBER_PSW_LOADER_ADDRESS_RANGE,
    UA_DECEMBER_PSW_LOADER_FILE_RANGE,
    UA_DECEMBER_PSW_LOADER_RANGE_SHA256,
    UA_DECEMBER_PSW_NCLIP_ADDRESS,
    UA_DECEMBER_PSW_NCLIP_FILE_OFFSET,
    UA_DECEMBER_PSW_NCLIP_REJECT_ADDRESS,
    UA_DECEMBER_PSW_NCLIP_REJECT_FILE_OFFSET,
    UA_DECEMBER_PSW_RENDER_FUNCTION_ADDRESS_RANGE,
    UA_DECEMBER_PSW_RENDER_FUNCTION_FILE_RANGE,
    UA_DECEMBER_PSW_RENDER_FUNCTION_RANGE_SHA256,
    UA_DECEMBER_PSW_RTPT_ADDRESS,
    UA_DECEMBER_PSW_RTPT_FILE_OFFSET,
    UA_DECEMBER_UNIT_RENDER_CALL_ADDRESS,
    UA_DECEMBER_UNIT_RENDER_CALL_FILE_OFFSET,
    UA_DECEMBER_V1_PSW_SHA256,
    UA_MARCH_MAIN_EXE_SHA256,
    UA_MARCH_OVER1_LOAD_ADDRESS,
    UA_MARCH_OVER1_SHA256,
    UA_MARCH_PSW_FACE_LOOP_ADDRESS_RANGE,
    UA_MARCH_PSW_FACE_LOOP_FILE_RANGE,
    UA_MARCH_PSW_FACE_LOOP_RANGE_SHA256,
    UA_MARCH_PSW_NCLIP_ADDRESS,
    UA_MARCH_PSW_NCLIP_FILE_OFFSET,
    UA_MARCH_PSW_NCLIP_REJECT_ADDRESS,
    UA_MARCH_PSW_NCLIP_REJECT_FILE_OFFSET,
    UA_MARCH_PSW_RENDER_FUNCTION_ADDRESS_RANGE,
    UA_MARCH_PSW_RENDER_FUNCTION_FILE_RANGE,
    UA_MARCH_PSW_RENDER_FUNCTION_RANGE_SHA256,
    UA_MARCH_PSW_RTPT_ADDRESS,
    UA_MARCH_PSW_RTPT_FILE_OFFSET,
    UA_MARCH_UNIT_BIN_SHA256,
    UA_PSW_PW3_PREFIX_MATCH_FACE_COUNT,
    UA_PSW_PW3_PREFIX_MATCH_MESH_COUNT,
    PsxPswPipelineError,
    authored_to_gt4,
    bind_psw_unit_face,
    decode_psw_face_record,
    decode_psw_texture_descriptor,
    psw_unit_descriptor_index,
    psw_unit_gt4_command,
    psw_unit_nclip_submits,
    psw_material_local_uv_quotient,
)


def _synthetic_face_record() -> bytes:
    record = bytearray(PSW_FACE_SIZE)
    struct.pack_into("<II", record, 0, PSW_PRIMITIVE_FLAGS_BIT13, 0xDEADBEEF)
    struct.pack_into("<4I", record, 8, 10, 11, 12, 13)
    struct.pack_into(
        "<8i", record, 24,
        4 << 16, 6 << 16,
        8 << 16, 10 << 16,
        -1 << 16, -1 << 16,
        254 << 16, 255 << 16,
    )
    struct.pack_into("<I", record, 56, 7)
    struct.pack_into("<4i", record, 60, 10 << 16, 20 << 16,
                     30 << 16, 40 << 16)
    return bytes(record)


def _synthetic_descriptor(*, tpage: int = 0x0060) -> bytes:
    descriptor = bytearray(PSW_TEXTURE_DESCRIPTOR_SIZE)
    struct.pack_into("<H", descriptor, 4, 32)
    struct.pack_into("<H", descriptor, 6, 64)
    struct.pack_into("<H", descriptor, 12, tpage)
    struct.pack_into("<H", descriptor, 14, 0x0123)
    return bytes(descriptor)


def _corpus_file(
        corpus: Path, build: str, *relative: str) -> Path | None:
    suffix = Path(build, "work", "disc_files", *relative)
    candidates = (
        corpus / "technical" / "analysis" / suffix,
        corpus / "analysis" / suffix,
        corpus / suffix,
    )
    return next((path for path in candidates if path.is_file()), None)


class PswFaceContractTests(unittest.TestCase):
    def test_exact_wide_face_layout_decodes_without_prefix_invention(self) -> None:
        face = decode_psw_face_record(_synthetic_face_record())

        self.assertEqual(PSW_PRIMITIVE_FLAGS_BIT13, face.prefix.primitive_flags)
        self.assertEqual(0xDEADBEEF, face.prefix.unresolved_word4)
        self.assertTrue(face.prefix.bit13_is_set)
        self.assertEqual((10, 11, 12, 13), face.vertex_indices)
        self.assertEqual(7, face.texture_selector)
        self.assertEqual((10 << 16, 20 << 16, 30 << 16, 40 << 16),
                         face.shade_fixed_16_16)

    def test_authored_corner_order_is_exactly_gt4_1_0_2_3(self) -> None:
        self.assertEqual((1, 0, 2, 3), PSW_AUTHORED_TO_GT4_ORDER)
        self.assertEqual((1, 0, 2), PSW_NCLIP_AUTHORED_ORDER)
        self.assertEqual(("one", "zero", "two", "three"),
                         authored_to_gt4(("zero", "one", "two", "three")))

    def test_unit_loader_uv_shade_and_descriptor_binding(self) -> None:
        face = decode_psw_face_record(_synthetic_face_record())
        descriptor = decode_psw_texture_descriptor(_synthetic_descriptor())

        binding = bind_psw_unit_face(face, descriptor)

        self.assertEqual(8, binding.descriptor_index)
        self.assertEqual(PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
                         binding.command)
        self.assertEqual(0x0060, binding.tpage)
        self.assertEqual(0x0123, binding.clut_offset)
        self.assertEqual((11, 10, 12, 13), binding.vertex_indices)
        self.assertEqual(
            ((36, 69), (34, 67), (32, 64), (159, 191)),
            binding.uv_bytes,
        )
        self.assertEqual((20, 10, 30, 40), binding.shade_bytes)

    def test_gt4_command_uses_only_the_proven_tpage_abr_mask(self) -> None:
        for tpage in (0, 0x001F, 0x0080, 0x7F00):
            with self.subTest(tpage=tpage):
                self.assertEqual(
                    PSX_POLY_GT4_OPAQUE_COMMAND,
                    psw_unit_gt4_command(tpage),
                )
        for tpage in (0x0020, 0x0040, 0x0060, 0xFFFF):
            with self.subTest(tpage=tpage):
                self.assertEqual(
                    PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
                    psw_unit_gt4_command(tpage),
                )

    def test_nclip_is_unconditional_and_strictly_positive(self) -> None:
        self.assertEqual("unconditional_strict_positive_mac0", PSW_NCLIP_POLICY)
        self.assertFalse(psw_unit_nclip_submits(-1))
        self.assertFalse(psw_unit_nclip_submits(0))
        self.assertTrue(psw_unit_nclip_submits(1))

    def test_material_local_uv_quotient_matches_the_exact_signed_formula(
            self) -> None:
        expected = (
            (-1 << 16, 0),
            (0, 0),
            (1 << 16, 0),
            (2 << 16, 1),
            (3 << 16, 1),
            (254 << 16, 127),
            (255 << 16, 127),
        )
        for signed_fixed, quotient in expected:
            with self.subTest(signed_fixed=signed_fixed):
                self.assertEqual(
                    psw_material_local_uv_quotient(signed_fixed), quotient)

    def test_material_local_uv_quotient_fails_closed(self) -> None:
        invalid = (
            True,
            1.0,
            "0",
            1,
            -1,
            -2 << 16,
            256 << 16,
            -0x80000000,
            0x7FFFFFFF,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(PsxPswPipelineError):
                    psw_material_local_uv_quotient(value)  # type: ignore[arg-type]

    def test_runtime_binding_retains_out_of_preview_quotient_and_wrap(self) \
            -> None:
        record = bytearray(_synthetic_face_record())
        # Preview-local q=128 is outside one decoded 128-wide material, but
        # the executable-backed packet path still adds descriptor U=32 and
        # writes the low byte.  The GT4 authored order puts corner 0 second.
        struct.pack_into("<i", record, 24, 256 << 16)
        face = decode_psw_face_record(bytes(record))
        descriptor = decode_psw_texture_descriptor(_synthetic_descriptor())

        with self.assertRaisesRegex(
                PsxPswPipelineError, "quotient must be in"):
            psw_material_local_uv_quotient(face.uv_fixed_16_16[0][0])
        binding = bind_psw_unit_face(face, descriptor)

        self.assertEqual(binding.uv_bytes[1][0], 160)

    def test_prefix_semantic_boundaries_are_machine_readable(self) -> None:
        self.assertIn("preserved_to_pw3", PSW_PRIMITIVE_FLAGS_STATE)
        self.assertIn("unapplied", PSW_PRIMITIVE_FLAGS_STATE)
        self.assertIn("unresolved", PSW_PREFIX_WORD4_STATE)
        self.assertIn("not_read", PSW_PREFIX_WORD4_STATE)
        self.assertEqual(
            "material_local_uv_quotient_preview_integrated_"
            "descriptor_runtime_binding_unresolved",
            PSX_PSW_VIEWER_INTEGRATION_STATE)

    def test_profile_and_packet_constants_are_explicit(self) -> None:
        self.assertEqual("ua_psw_unit_gt4_static_v1",
                         PSX_PSW_PIPELINE_PROFILE_ID)
        self.assertIn("cross_build_static", PSX_PSW_PIPELINE_EVIDENCE)
        self.assertEqual(0x4C, PSW_FACE_SIZE)
        self.assertEqual(0x34, PSX_POLY_GT4_PACKET_SIZE)
        self.assertEqual(8, psw_unit_descriptor_index(7))

    def test_public_inputs_fail_closed_on_type_and_width(self) -> None:
        for record in (bytearray(PSW_FACE_SIZE), b"", bytes(PSW_FACE_SIZE - 1)):
            with self.subTest(record_type=type(record), length=len(record)):
                with self.assertRaises(PsxPswPipelineError):
                    decode_psw_face_record(record)  # type: ignore[arg-type]
        with self.assertRaises(PsxPswPipelineError):
            decode_psw_texture_descriptor(bytearray(16))  # type: ignore[arg-type]
        with self.assertRaises(PsxPswPipelineError):
            authored_to_gt4((0, 1, 2))
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                with self.assertRaises(PsxPswPipelineError):
                    psw_unit_nclip_submits(value)  # type: ignore[arg-type]
        with self.assertRaises(PsxPswPipelineError):
            psw_unit_descriptor_index(0x10000)
        with self.assertRaises(PsxPswPipelineError):
            psw_unit_gt4_command(-1)

    def test_decoded_contract_is_immutable(self) -> None:
        face = decode_psw_face_record(_synthetic_face_record())
        with self.assertRaises(FrozenInstanceError):
            face.texture_selector = 2  # type: ignore[misc]


@unittest.skipUnless(
    os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
    "set OPENUA_PSX_CORPUS_ROOT for recovered PSW/PSV checks",
)
class RecoveredPswCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])

    def _required(self, build: str, *relative: str) -> Path:
        path = _corpus_file(self.corpus, build, *relative)
        self.assertIsNotNone(
            path,
            f"OPENUA_PSX_CORPUS_ROOT has no {build}/{'/'.join(relative)}",
        )
        assert path is not None
        return path

    def test_full_file_and_half_open_code_range_anchors(self) -> None:
        december_exe = self._required("1998-12-18", "PSX.EXE")
        december_over = self._required("1998-12-18", "OVER1.TXT")
        march_exe = self._required("1999-03-12", "PSX.EXE")
        march_over = self._required("1999-03-12", "OVER1.TXT")

        files = (
            (december_exe, UA_DECEMBER_MAIN_EXE_SHA256),
            (december_over, UA_DECEMBER_OVER1_SHA256),
            (march_exe, UA_MARCH_MAIN_EXE_SHA256),
            (march_over, UA_MARCH_OVER1_SHA256),
        )
        for path, expected_hash in files:
            with self.subTest(path=path):
                self.assertEqual(
                    expected_hash, hashlib.sha256(path.read_bytes()).hexdigest())

        ranges = (
            (
                december_over,
                UA_DECEMBER_PSW_RENDER_FUNCTION_FILE_RANGE,
                UA_DECEMBER_PSW_RENDER_FUNCTION_RANGE_SHA256,
            ),
            (
                december_over,
                UA_DECEMBER_PSW_FACE_LOOP_FILE_RANGE,
                UA_DECEMBER_PSW_FACE_LOOP_RANGE_SHA256,
            ),
            (
                december_over,
                UA_DECEMBER_PSW_LOADER_FILE_RANGE,
                UA_DECEMBER_PSW_LOADER_RANGE_SHA256,
            ),
            (
                march_over,
                UA_MARCH_PSW_RENDER_FUNCTION_FILE_RANGE,
                UA_MARCH_PSW_RENDER_FUNCTION_RANGE_SHA256,
            ),
            (
                march_over,
                UA_MARCH_PSW_FACE_LOOP_FILE_RANGE,
                UA_MARCH_PSW_FACE_LOOP_RANGE_SHA256,
            ),
        )
        for path, offsets, expected_hash in ranges:
            with self.subTest(path=path, offsets=offsets):
                start, end = offsets
                self.assertEqual(
                    expected_hash,
                    hashlib.sha256(path.read_bytes()[start:end]).hexdigest(),
                )

    def test_runtime_addresses_are_load_base_plus_file_offset(self) -> None:
        december_pairs = (
            (UA_DECEMBER_UNIT_RENDER_CALL_FILE_OFFSET,
             UA_DECEMBER_UNIT_RENDER_CALL_ADDRESS),
            (UA_DECEMBER_PSW_RTPT_FILE_OFFSET, UA_DECEMBER_PSW_RTPT_ADDRESS),
            (UA_DECEMBER_PSW_NCLIP_FILE_OFFSET,
             UA_DECEMBER_PSW_NCLIP_ADDRESS),
            (UA_DECEMBER_PSW_NCLIP_REJECT_FILE_OFFSET,
             UA_DECEMBER_PSW_NCLIP_REJECT_ADDRESS),
            (UA_DECEMBER_PSW_FOURTH_RTPS_FILE_OFFSET,
             UA_DECEMBER_PSW_FOURTH_RTPS_ADDRESS),
        )
        for offset, address in december_pairs:
            self.assertEqual(UA_DECEMBER_OVER1_LOAD_ADDRESS + offset, address)
        for offsets, addresses in (
            (UA_DECEMBER_PSW_RENDER_FUNCTION_FILE_RANGE,
             UA_DECEMBER_PSW_RENDER_FUNCTION_ADDRESS_RANGE),
            (UA_DECEMBER_PSW_FACE_LOOP_FILE_RANGE,
             UA_DECEMBER_PSW_FACE_LOOP_ADDRESS_RANGE),
            (UA_DECEMBER_PSW_LOADER_FILE_RANGE,
             UA_DECEMBER_PSW_LOADER_ADDRESS_RANGE),
        ):
            self.assertEqual(
                tuple(UA_DECEMBER_OVER1_LOAD_ADDRESS + value
                      for value in offsets),
                addresses,
            )

        march_pairs = (
            (UA_MARCH_PSW_RTPT_FILE_OFFSET, UA_MARCH_PSW_RTPT_ADDRESS),
            (UA_MARCH_PSW_NCLIP_FILE_OFFSET, UA_MARCH_PSW_NCLIP_ADDRESS),
            (UA_MARCH_PSW_NCLIP_REJECT_FILE_OFFSET,
             UA_MARCH_PSW_NCLIP_REJECT_ADDRESS),
        )
        for offset, address in march_pairs:
            self.assertEqual(UA_MARCH_OVER1_LOAD_ADDRESS + offset, address)
        for offsets, addresses in (
            (UA_MARCH_PSW_RENDER_FUNCTION_FILE_RANGE,
             UA_MARCH_PSW_RENDER_FUNCTION_ADDRESS_RANGE),
            (UA_MARCH_PSW_FACE_LOOP_FILE_RANGE,
             UA_MARCH_PSW_FACE_LOOP_ADDRESS_RANGE),
        ):
            self.assertEqual(
                tuple(UA_MARCH_OVER1_LOAD_ADDRESS + value
                      for value in offsets),
                addresses,
            )

    def test_gte_opcode_anchors_and_face_stride_are_exact(self) -> None:
        december = self._required("1998-12-18", "OVER1.TXT").read_bytes()
        march = self._required("1999-03-12", "OVER1.TXT").read_bytes()

        self.assertEqual(0x4A280030, struct.unpack_from(
            "<I", december, UA_DECEMBER_PSW_RTPT_FILE_OFFSET)[0])
        self.assertEqual(0x4B400006, struct.unpack_from(
            "<I", december, UA_DECEMBER_PSW_NCLIP_FILE_OFFSET)[0])
        self.assertEqual(0x4A180001, struct.unpack_from(
            "<I", december, UA_DECEMBER_PSW_FOURTH_RTPS_FILE_OFFSET)[0])
        self.assertEqual(0x2529004C, struct.unpack_from(
            "<I", december, 0x9FF4)[0])
        self.assertEqual(0x2718004C, struct.unpack_from(
            "<I", december, 0x9FFC)[0])

        self.assertEqual(0x4A280030, struct.unpack_from(
            "<I", march, UA_MARCH_PSW_RTPT_FILE_OFFSET)[0])
        self.assertEqual(0x4B400006, struct.unpack_from(
            "<I", march, UA_MARCH_PSW_NCLIP_FILE_OFFSET)[0])
        self.assertEqual(0x2529004C, struct.unpack_from(
            "<I", march, 0xED28)[0])
        self.assertEqual(0x2718004C, struct.unpack_from(
            "<I", march, 0xED30)[0])

    def test_prefix_bit13_is_preserved_across_18_exact_mesh_lineages(self) -> None:
        december_unit = self._required(
            "1998-12-18", "UNITMODL", "V1.PSW").parent
        march_archive = self._required(
            "1999-03-12", "UNITMODL", "UNIT.BIN")
        self.assertEqual(
            UA_DECEMBER_V1_PSW_SHA256,
            hashlib.sha256((december_unit / "V1.PSW").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            UA_MARCH_UNIT_BIN_SHA256,
            hashlib.sha256(march_archive.read_bytes()).hexdigest(),
        )

        later_meshes = scan_unit_archive_file(march_archive)
        by_semantic_hash = {
            mesh_semantic_sha256(mesh): mesh for mesh in later_meshes
        }
        matched_meshes = 0
        matched_faces = 0
        observed_flags: set[int] = set()
        for path in sorted(december_unit.glob("*.PSW")):
            older = parse_psx_mesh_file(path)
            later = by_semantic_hash.get(mesh_semantic_sha256(older))
            if later is None:
                continue
            matched_meshes += 1
            self.assertEqual(len(older.faces), len(later.faces))
            for old_face, new_face in zip(older.faces, later.faces):
                old_flags = struct.unpack_from("<I", old_face.opaque_prefix)[0]
                new_flags = struct.unpack_from("<H", new_face.opaque_prefix)[0]
                observed_flags.add(old_flags)
                self.assertEqual(old_flags, new_flags)
                matched_faces += 1

        self.assertEqual({0, PSW_PRIMITIVE_FLAGS_BIT13}, observed_flags)
        self.assertEqual(UA_PSW_PW3_PREFIX_MATCH_MESH_COUNT, matched_meshes)
        self.assertEqual(UA_PSW_PW3_PREFIX_MATCH_FACE_COUNT, matched_faces)


if __name__ == "__main__":
    unittest.main()
