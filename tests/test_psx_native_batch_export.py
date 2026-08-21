from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport, VIEW_PRESET_ANGLES
from psx_native_assets import (
    PSX_NATIVE_PARSER_VERSION,
    PsxNativeBuild,
    PsxNativeModelSlotEvidence,
    mesh_primitive_cull_census,
    mesh_raw_corner_shade_census,
    parse_psx_mesh_bytes,
)
from psx_native_raster import PSX_LEGACY_PSW_RASTER_PROFILE_ID
from psx_native_effects import (
    PV2_FACE_POINTER_RESIDUE,
    PV2_VERTEX_POINTER_RESIDUE,
    parse_pv2_bytes,
)
from psx_native_textures import (
    MATERIAL_SLOT_COUNT,
    PIXEL_BANK_COUNT,
    SECTOR_PADDED_EMPTY_ALLOCATION,
    SECTOR_PADDED_EMPTY_MARKER,
    SECTOR_PADDED_FULL_ALLOCATION,
    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
    SECTOR_PADDED_SETGFX_LAYOUT_ID,
    ZERO_RECORD_HEADER,
    parse_late_setgfx_bytes,
    parse_sector_padded_setgfx_bytes,
)
import snapshot_studio.psx_batch_export as psx_batch_export
from snapshot_studio.psx_batch_export import (
    DEFAULT_PSX_NATIVE_BATCH_VIEWS,
    PSX_NATIVE_BATCH_MANIFEST_FILENAME,
    PSX_NATIVE_BATCH_MANIFEST_SCHEMA,
    PSX_NATIVE_BATCH_MANIFEST_SCHEMA_VERSION,
    PSX_NATIVE_BATCH_PROFILE_ID,
    PSX_NATIVE_MANUAL_PROFILE_ID,
    PSX_NATIVE_SNAPSHOT_SCHEMA,
    PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION,
    PsxNativeAtomicWriteError,
    PsxNativeBatchCollisionError,
    PsxNativeBatchConfig,
    PsxNativeBatchError,
    PsxNativeBatchExporter,
    PsxNativeBatchProvenanceError,
    build_native_snapshot_identity,
    build_native_snapshot_sidecar,
    prove_existing_native_snapshot_pair,
    validate_native_renderer_info,
    write_atomic_png_json_pair,
)


def _native_mesh(
        ordinal: int = 0, selector: int = 7,
        *, logical_path: str = "UNITMODL/UNIT.BIN"):
    vertices = (
        (-65536, 65536, 0),
        (0, -65536, 32768),
        (65536, 65536, 0),
    )
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, len(vertices), 1)
    struct.pack_into("<II", header, 0x40, 80, 80 + 12 * len(vertices))
    face = bytearray(26)
    face[:4] = b"\x10\x20\x30\x40"
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    face[12:20] = bytes((0, 0, 128, 255, 255, 0, 255, 0))
    struct.pack_into("<H", face, 20, selector)
    face[22:26] = bytes((10, 20, 30, 30))
    data = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + bytes(face)
    data += b"\0" * ((-len(data)) % 4)
    return parse_psx_mesh_bytes(
        data,
        logical_path=logical_path,
        archive_ordinal=ordinal,
        archive_offset=(ordinal + 1) * 0x800,
    )


def _native_texture_pack(logical_path: str = "GFX/SET1GFX.BIN"):
    records = []
    for selector in range(MATERIAL_SLOT_COUNT):
        palette = [0] * 16
        palette[1] = 0x001F
        palette[2] = 0x03E0
        record = bytearray(
            ZERO_RECORD_HEADER + struct.pack("<16H", *palette))
        if selector < PIXEL_BANK_COUNT:
            record.extend(bytes((0x21,)) * 0x2000)
        records.append(bytes(record))
    return parse_late_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


def _native_sector_texture_pack(
        logical_path: str = "GFX/SET2GFX.BIN"):
    records = []
    for selector in range(MATERIAL_SLOT_COUNT):
        if selector < 77:
            palette = [0] * 16
            palette[1] = 0x001F
            palette[2] = 0x03E0
            record = bytearray(
                ZERO_RECORD_HEADER + struct.pack("<16H", *palette))
            record.extend(bytes((0x21,)) * 0x2000)
            record.extend(
                bytes((0xBA,))
                * (SECTOR_PADDED_FULL_ALLOCATION - len(record)))
        else:
            record = bytearray(SECTOR_PADDED_EMPTY_MARKER)
            record.extend(
                bytes((0xBA,))
                * (SECTOR_PADDED_EMPTY_ALLOCATION - len(record)))
        records.append(bytes(record))
    return parse_sector_padded_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


def _native_psw_mesh(
        selector: int = 7,
        *, logical_path: str = "UNITMODL/V1.PSW"):
    vertices = (
        (-65536, 65536, 0),
        (0, -65536, 32768),
        (65536, 65536, 0),
    )
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 1)
    struct.pack_into("<II", header, 0x38, len(vertices), 1)
    struct.pack_into("<II", header, 0x40, 80, 80 + 12 * len(vertices))
    face = bytearray(76)
    face[:8] = b"\x10\x20\x30\x40\x50\x60\x70\x80"
    struct.pack_into("<4I", face, 8, 0, 1, 2, 2)
    struct.pack_into(
        "<8I", face, 24,
        *(value << 16 for value in (0, 0, 128, 255, 255, 0, 255, 0)))
    struct.pack_into("<I", face, 56, selector)
    struct.pack_into(
        "<4I", face, 60,
        *(value << 16 for value in (64, 128, 255, 255)))
    data = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + bytes(face)
    return parse_psx_mesh_bytes(data, logical_path=logical_path)


def _native_build(root: Path, meshes, *texture_packs) -> PsxNativeBuild:
    return PsxNativeBuild(
        root=root,
        system_cnf_logical_path="SYSTEM.CNF",
        system_cnf_sha256="11" * 32,
        boot_executable_logical_path="SCES_019.63",
        boot_executable_sha256="22" * 32,
        unit_archive_logical_path="UNITMODL/UNIT.BIN",
        unit_archive_sha256="33" * 32,
        vehicle_roster_logical_path="LISTS/VEHICLE.TXT",
        vehicle_roster_sha256="44" * 32,
        vehicle_roster=("UNMAPPED_000",),
        meshes=tuple(meshes),
        texture_packs=tuple(texture_packs),
    )


def _native_pv2():
    header = bytearray(32)
    struct.pack_into("<I", header, 0, 1)
    struct.pack_into("<iii", header, 4, 0, 0x25800, 0)
    struct.pack_into(
        "<4I", header, 0x10,
        10, 5, PV2_VERTEX_POINTER_RESIDUE, PV2_FACE_POINTER_RESIDUE)
    vertices = bytes(10 * 12)
    face = bytearray(40)
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    source = bytes(header) + vertices + bytes(face) * 5
    return parse_pv2_bytes(
        source, logical_path="TEST_ART/SYNTHETIC.PV2")


def _solid_image(width: int = 12, height: int = 10, seed: int = 1) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(20 + seed, 40 + seed, 60 + seed, 255))
    return image


def _minimal_sidecar(label: str = "atomic") -> dict[str, object]:
    # The atomic writer is deliberately tested independently of the native
    # renderer-alignment validator used by build_native_snapshot_sidecar.
    return {
        "schema_id": PSX_NATIVE_SNAPSHOT_SCHEMA,
        "schema_version": PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION,
        "identity": {"test_identity": label},
        "renderer": {"test_renderer": label},
    }


class PsxNativeBatchExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_core_exporter_refuses_source_root_and_lexical_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source_root = parent / "disc"
            source_root.mkdir()
            build = _native_build(source_root, (_native_mesh(),))
            before = tuple(source_root.iterdir())

            forbidden = (
                source_root,
                source_root / "generated",
                source_root / ".." / source_root.name / "lexical-child",
            )
            for output_root in forbidden:
                with self.subTest(output_root=output_root):
                    with self.assertRaisesRegex(
                            PsxNativeBatchError,
                            "output must be outside the extracted"):
                        PsxNativeBatchExporter(
                            build,
                            PsxNativeBatchConfig(
                                output_root=output_root,
                                width=24,
                                height=24,
                                views=("Front",),
                            ),
                        )

            self.assertEqual(tuple(source_root.iterdir()), before)
            self.assertFalse((source_root / "generated").exists())
            self.assertFalse((source_root / "lexical-child").exists())

    def test_core_exporter_refuses_resolved_alias_into_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source_root = parent / "disc"
            source_root.mkdir()
            alias_output = parent / "source-alias" / "generated"
            build = _native_build(source_root, (_native_mesh(),))
            path_type = type(source_root)
            real_resolve = path_type.resolve

            def simulated_resolve(candidate, *, strict=False):
                if Path(candidate) == alias_output:
                    return source_root / "resolved-alias-child"
                return real_resolve(candidate, strict=strict)

            with patch.object(
                    path_type, "resolve", autospec=True,
                    side_effect=simulated_resolve):
                with self.assertRaisesRegex(
                        PsxNativeBatchError,
                        "output must be outside the extracted"):
                    PsxNativeBatchExporter(
                        build,
                        PsxNativeBatchConfig(
                            output_root=alias_output,
                            width=24,
                            height=24,
                            views=("Front",),
                        ),
                    )

            self.assertFalse(alias_output.exists())
            self.assertFalse((source_root / "resolved-alias-child").exists())

    def test_core_exporter_fails_closed_when_boundary_resolution_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source_root = parent / "disc"
            source_root.mkdir()
            output_root = parent / "unresolved-output"
            build = _native_build(source_root, (_native_mesh(),))
            path_type = type(source_root)
            real_resolve = path_type.resolve

            def simulated_resolve(candidate, *, strict=False):
                if Path(candidate) == output_root:
                    raise OSError("forced boundary resolution failure")
                return real_resolve(candidate, strict=strict)

            with patch.object(
                    path_type, "resolve", autospec=True,
                    side_effect=simulated_resolve):
                with self.assertRaisesRegex(
                        PsxNativeBatchError,
                        "could not resolve the native batch output/source"):
                    PsxNativeBatchExporter(
                        build,
                        PsxNativeBatchConfig(
                            output_root=output_root,
                            width=24,
                            height=24,
                            views=("Front",),
                        ),
                    )

            self.assertFalse(output_root.exists())
            self.assertEqual(tuple(source_root.iterdir()), ())

    def test_cancelled_manifest_rejects_output_root_swapped_to_reparse(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source_root = parent / "disc"
            source_root.mkdir()
            output_root = parent / "not-created-yet"
            exporter = PsxNativeBatchExporter(
                _native_build(source_root, (_native_mesh(),)),
                PsxNativeBatchConfig(
                    output_root=output_root,
                    width=24,
                    height=24,
                    views=("Front",),
                ),
            )
            exporter.request_cancel()
            real_detector = psx_batch_export._is_reparse_point

            def simulated_reparse(candidate: Path) -> bool:
                return (
                    Path(candidate) == exporter.output_root
                    or real_detector(Path(candidate))
                )

            with patch.object(
                    psx_batch_export, "_is_reparse_point",
                    side_effect=simulated_reparse):
                with self.assertRaisesRegex(
                        PsxNativeBatchError,
                        "symlink, junction, or reparse point"):
                    exporter.start()

            self.assertFalse(output_root.exists())
            self.assertEqual(tuple(source_root.iterdir()), ())

    def test_atomic_pair_reresolves_boundary_after_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "disc"
            source_root.mkdir()
            output_root = root / "out"
            output_root.mkdir()
            png_path = output_root / "native.png"
            path_type = type(png_path)
            real_resolve = path_type.resolve
            output_resolves = 0

            def simulated_resolve(candidate, *, strict=False):
                nonlocal output_resolves
                if Path(candidate) == png_path:
                    output_resolves += 1
                    if output_resolves >= 2:
                        return source_root / "redirected-native.png"
                return real_resolve(candidate, strict=strict)

            with patch.object(
                    path_type, "resolve", autospec=True,
                    side_effect=simulated_resolve):
                with self.assertRaisesRegex(
                        PsxNativeBatchError,
                        "must remain outside the extracted"):
                    write_atomic_png_json_pair(
                        _solid_image(), png_path, _minimal_sidecar(),
                        source_root=source_root)

            self.assertGreaterEqual(output_resolves, 2)
            self.assertFalse(png_path.exists())
            self.assertFalse(png_path.with_suffix(".png.json").exists())
            self.assertEqual(tuple(output_root.iterdir()), ())

    def test_topology_batch_enumerates_every_mesh_and_fixed_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            build = _native_build(
                temporary_path / "private-disc-root",
                (_native_mesh(0, 7), _native_mesh(1, 19)),
            )
            created: list[AssetViewport] = []

            def viewport_factory():
                viewport = AssetViewport()
                created.append(viewport)
                return viewport

            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "out",
                    width=40,
                    height=32,
                    background_rgba=(9, 10, 11, 255),
                ),
                viewport_factory=viewport_factory,
            )
            result = exporter.run()

            self.assertFalse(result.cancelled)
            self.assertEqual(result.total, 2 * len(VIEW_PRESET_ANGLES))
            self.assertEqual(result.written, result.total)
            self.assertEqual(result.skipped_verified, 0)
            self.assertEqual(
                [record.view_name for record in result.records[:10]],
                list(DEFAULT_PSX_NATIVE_BATCH_VIEWS),
            )
            self.assertEqual(
                [record.mesh_index for record in result.records],
                [0] * 10 + [1] * 10,
            )
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0]._snapshot_active)
            self.assertIsNone(created[0]._snapshot_saved_state)
            root_text = str(build.root)
            self.assertEqual(
                result.manifest_path,
                exporter.output_root / PSX_NATIVE_BATCH_MANIFEST_FILENAME)
            self.assertTrue(result.manifest_path.is_file())
            manifest_bytes = result.manifest_path.read_bytes()
            self.assertEqual(
                result.manifest_sha256,
                hashlib.sha256(manifest_bytes).hexdigest())
            manifest = json.loads(manifest_bytes)
            self.assertEqual(
                manifest["schema_id"], PSX_NATIVE_BATCH_MANIFEST_SCHEMA)
            self.assertEqual(
                manifest["schema_version"],
                PSX_NATIVE_BATCH_MANIFEST_SCHEMA_VERSION)
            self.assertEqual(
                manifest["renderer_contract"]["parser_version"],
                PSX_NATIVE_PARSER_VERSION)
            self.assertEqual(
                manifest["renderer_contract"]["profile"]
                ["pw3_nclip_raw_corner_order"], [1, 0, 2])
            self.assertEqual(manifest["terminal_state"], "complete")
            self.assertEqual(manifest["plan"]["total"], result.total)
            self.assertEqual(len(manifest["records"]), result.total)
            self.assertEqual(manifest["execution"]["written"], result.total)
            self.assertNotIn(root_text, manifest_bytes.decode("utf-8"))

            for manifest_record in manifest["records"]:
                expected_mesh = build.meshes[manifest_record["mesh_index"]]
                self.assertEqual(
                    manifest_record["mesh"]
                    ["native_primitive_cull_census"],
                    [list(item) for item in
                     mesh_primitive_cull_census(expected_mesh)])
                self.assertEqual(
                    manifest_record["mesh"]
                    ["native_raw_corner_shade_census"],
                    [list(item) for item in
                     mesh_raw_corner_shade_census(expected_mesh)])

            for record in result.records:
                png_path = exporter.output_root / record.relative_png
                json_path = exporter.output_root / record.relative_json
                self.assertTrue(png_path.is_file())
                self.assertTrue(json_path.is_file())
                self.assertEqual(png_path.with_suffix(".png.json"), json_path)
                sidecar_text = json_path.read_text(encoding="utf-8")
                sidecar = json.loads(sidecar_text)
                self.assertNotIn(root_text, sidecar_text)
                self.assertEqual(
                    sidecar["identity"]["source"]["container_kind"],
                    "extracted_psx_disc_tree",
                )
                self.assertEqual(
                    sidecar["identity"]["capture_profile_id"],
                    PSX_NATIVE_BATCH_PROFILE_ID,
                )
                self.assertIsNotNone(
                    sidecar["identity"]["view"]["camera_state"])
                self.assertEqual(
                    sidecar["identity"]["texture"]["mode"],
                    "topology_only",
                )
                self.assertEqual(
                    sidecar["renderer"]["source_asset_pipeline"],
                    "psx_native_disc_assets",
                )
                self.assertTrue(
                    sidecar["renderer"]["native_psx_asset_decode"])
                self.assertFalse(
                    sidecar["renderer"]["pc_openua_source_used"])
                self.assertFalse(sidecar["renderer"]["fallback_used"])
                expected_mesh = build.meshes[record.mesh_index]
                expected_cull = [
                    list(item)
                    for item in mesh_primitive_cull_census(expected_mesh)]
                expected_raw_shades = [
                    list(item)
                    for item in mesh_raw_corner_shade_census(expected_mesh)]
                self.assertEqual(
                    sidecar["identity"]["mesh"]
                    ["native_primitive_cull_census"], expected_cull)
                self.assertEqual(
                    sidecar["identity"]["mesh"]
                    ["native_raw_corner_shade_census"],
                    expected_raw_shades)
                self.assertEqual(
                    sidecar["renderer"]
                    ["native_primitive_cull_census"], expected_cull)
                self.assertEqual(
                    sidecar["renderer"]
                    ["native_raw_corner_shade_census"],
                    expected_raw_shades)
                self.assertEqual(
                    sidecar["renderer"]["pw3_primitive_cull_policy"],
                    "bit14_clear_nclip_strict_positive; "
                    "bit14_set_two_sided")
                self.assertEqual(
                    sidecar["artifact"]["png_sha256"], record.png_sha256)
                reader = QImage(str(png_path))
                self.assertEqual((reader.width(), reader.height()), (40, 32))

    def test_proven_runtime_slot_coexists_with_dense_ordinal_everywhere(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            evidence = PsxNativeModelSlotEvidence(
                evidence_id="psx_unit_bin_executable_allocation_table_v1",
                unit_archive_sha256="33" * 32,
                boot_executable_sha256="22" * 32,
                executable_table_offset=0x6E650,
                executable_table_entry_count=2,
                executable_table_sha256="55" * 32,
                empty_model_slots=(0,),
                trailing_sentinel_archive_sector=99,
            )
            mesh = replace(
                _native_mesh(ordinal=0),
                model_slot=1,
                model_slot_evidence_id=evidence.evidence_id,
            )
            build = replace(
                _native_build(temporary_path / "disc", (mesh,)),
                model_slot_evidence=evidence,
            )
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "out",
                    width=30,
                    height=28,
                    views=("Front",),
                ),
            )

            result = exporter.run()

            self.assertEqual(result.total, 1)
            record = result.records[0]
            self.assertEqual(record.model_slot, 1)
            self.assertEqual(record.archive_ordinal, 0)
            folder = Path(record.relative_png).parts[0]
            self.assertTrue(folder.startswith(
                "UNIT_SLOT_001_ORD_000_"), folder)

            sidecar = json.loads(
                (exporter.output_root / record.relative_json).read_text(
                    encoding="utf-8"))
            mesh_identity = sidecar["identity"]["mesh"]
            source_identity = sidecar["identity"]["source"]
            renderer = sidecar["renderer"]
            self.assertEqual(mesh_identity["archive_ordinal"], 0)
            self.assertEqual(mesh_identity["model_slot"], 1)
            self.assertEqual(
                mesh_identity["model_slot_evidence_id"],
                evidence.evidence_id)
            self.assertEqual(
                mesh_identity["runtime_identity_status"],
                "executable_allocation_table_proven")
            self.assertEqual(
                mesh_identity["name_binding_status"],
                "model_slot_only_friendly_name_unmapped")
            self.assertEqual(renderer["native_mesh_ordinal"], 0)
            self.assertEqual(renderer["native_model_slot"], 1)
            self.assertEqual(
                renderer["native_model_slot_evidence_id"],
                evidence.evidence_id)
            self.assertEqual(
                source_identity["model_slot_binding_status"],
                "executable_allocation_table_proven")
            self.assertEqual(
                source_identity["model_slot_evidence"], {
                    "evidence_id": evidence.evidence_id,
                    "unit_archive_sha256": "33" * 32,
                    "boot_executable_sha256": "22" * 32,
                    "executable_table_offset": 0x6E650,
                    "executable_table_entry_count": 2,
                    "executable_table_sha256": "55" * 32,
                    "empty_model_slots": [0],
                    "trailing_sentinel_archive_sector": 99,
                })

            manifest = json.loads(result.manifest_path.read_text("utf-8"))
            manifest_mesh = manifest["records"][0]["mesh"]
            self.assertEqual(manifest_mesh["archive_ordinal"], 0)
            self.assertEqual(manifest_mesh["model_slot"], 1)
            self.assertEqual(
                manifest_mesh["model_slot_evidence_id"],
                evidence.evidence_id)
            self.assertEqual(
                manifest["source"]["model_slot_evidence"],
                source_identity["model_slot_evidence"])
            self.assertTrue(
                manifest["records"][0]["relative_png"].startswith(
                    "UNIT_SLOT_001_ORD_000_"))

    def test_proven_runtime_slot_evidence_mismatches_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            evidence = PsxNativeModelSlotEvidence(
                evidence_id="psx_unit_bin_executable_allocation_table_v1",
                unit_archive_sha256="33" * 32,
                boot_executable_sha256="22" * 32,
                executable_table_offset=0x100,
                executable_table_entry_count=2,
                executable_table_sha256="55" * 32,
                empty_model_slots=(0,),
                trailing_sentinel_archive_sector=None,
            )
            mesh = replace(
                _native_mesh(ordinal=0),
                model_slot=1,
                model_slot_evidence_id=evidence.evidence_id,
            )
            valid_build = replace(
                _native_build(temporary_path / "disc", (mesh,)),
                model_slot_evidence=evidence,
            )
            cases = (
                (
                    replace(
                        valid_build,
                        model_slot_evidence=replace(
                            evidence, unit_archive_sha256="ff" * 32)),
                    "UNIT.BIN hash must exactly match the build",
                ),
                (
                    replace(
                        valid_build,
                        meshes=(replace(
                            mesh, model_slot_evidence_id="wrong_evidence"),)),
                    "must carry the exact build model-slot evidence ID",
                ),
            )
            for index, (build, message) in enumerate(cases):
                output = temporary_path / f"rejected-{index}"
                with self.subTest(index=index), self.assertRaisesRegex(
                        PsxNativeBatchProvenanceError, message):
                    PsxNativeBatchExporter(
                        build,
                        PsxNativeBatchConfig(
                            output_root=output,
                            width=24,
                            height=24,
                            views=("Front",),
                        ),
                    ).run()
                self.assertFalse(output.exists())

    def test_frozen_build_rejects_duplicate_runtime_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            evidence = PsxNativeModelSlotEvidence(
                evidence_id="psx_unit_bin_executable_allocation_table_v1",
                unit_archive_sha256="33" * 32,
                boot_executable_sha256="22" * 32,
                executable_table_offset=0x100,
                executable_table_entry_count=2,
                executable_table_sha256="55" * 32,
                empty_model_slots=(),
                trailing_sentinel_archive_sector=None,
            )
            meshes = tuple(
                replace(
                    _native_mesh(ordinal=ordinal, selector=7 + ordinal),
                    model_slot=0,
                    model_slot_evidence_id=evidence.evidence_id,
                )
                for ordinal in range(2)
            )
            build = replace(
                _native_build(temporary_path / "disc", meshes),
                model_slot_evidence=evidence,
            )
            output = temporary_path / "duplicate-slots"

            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError,
                    "model slots must be unique"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=output,
                        width=24,
                        height=24,
                        views=("Front",),
                    ),
                )

            self.assertFalse(output.exists())

    def test_frozen_build_rejects_partial_table_cardinality(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            evidence = PsxNativeModelSlotEvidence(
                evidence_id="psx_unit_bin_executable_allocation_table_v1",
                unit_archive_sha256="33" * 32,
                boot_executable_sha256="22" * 32,
                executable_table_offset=0x100,
                executable_table_entry_count=3,
                executable_table_sha256="55" * 32,
                empty_model_slots=(0,),
                trailing_sentinel_archive_sector=None,
            )
            mesh = replace(
                _native_mesh(ordinal=0),
                model_slot=1,
                model_slot_evidence_id=evidence.evidence_id,
            )
            build = replace(
                _native_build(temporary_path / "disc", (mesh,)),
                model_slot_evidence=evidence,
            )
            output = temporary_path / "partial-table"

            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError,
                    "mesh count must equal executable table count"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=output,
                        width=24,
                        height=24,
                        views=("Front",),
                    ),
                )

            self.assertFalse(output.exists())

    def test_frozen_build_rejects_mutated_pv2_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            effect = _native_pv2()
            mutated = replace(
                effect, parser_version=effect.parser_version + 1)
            build = replace(
                _native_build(
                    temporary_path / "disc", (_native_mesh(),)),
                effects=(mutated,),
            )
            output = temporary_path / "mutated-pv2"

            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError,
                    "canonical PV2 parser identity"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=output,
                        width=24,
                        height=24,
                        views=("Front",),
                    ),
                )

            self.assertFalse(output.exists())

    def test_frozen_topology_and_explicit_texture_pack_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            mesh = _native_mesh()
            pack = _native_texture_pack()
            build = _native_build(
                temporary_path / "disc", (mesh,), pack)

            topology = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "topology",
                    width=36,
                    height=36,
                    views=("Front",),
                ),
            ).run()
            selected = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "selected",
                    width=36,
                    height=36,
                    views=("Front",),
                    texture_pack=pack,
                ),
            ).run()
            alternate_pack = _native_sector_texture_pack()
            alternate_build = _native_build(
                temporary_path / "disc-alternate", (mesh,), alternate_pack)
            alternate = PsxNativeBatchExporter(
                alternate_build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "alternate",
                    width=36,
                    height=36,
                    views=("Front",),
                    texture_pack=alternate_pack,
                ),
            ).run()

            topology_json = json.loads(
                (temporary_path / "topology"
                 / topology.records[0].relative_json).read_text("utf-8"))
            selected_json = json.loads(
                (temporary_path / "selected"
                 / selected.records[0].relative_json).read_text("utf-8"))
            alternate_json = json.loads(
                (temporary_path / "alternate"
                 / alternate.records[0].relative_json).read_text("utf-8"))
            self.assertEqual(
                topology_json["identity"]["texture"]["mode"],
                "topology_only",
            )
            self.assertEqual(
                topology_json["renderer"]["texture_binding_status"],
                "topology_only_operator_default",
            )
            self.assertEqual(
                topology_json["renderer"]["mesh_to_texture_pack_binding"],
                "none_selected",
            )
            self.assertEqual(
                selected_json["identity"]["texture"]["mode"],
                "explicit_native_pack",
            )
            self.assertEqual(
                selected_json["identity"]["texture"]["sha256"],
                pack.source_sha256,
            )
            self.assertEqual(
                selected_json["renderer"]["texture_binding_status"],
                "operator_selected_pack_with_validated_selector_table",
            )
            self.assertEqual(
                selected_json["renderer"]["mesh_to_texture_pack_binding"],
                "operator_selected_environment_variant_no_mesh_inherent_"
                "affinity",
            )
            self.assertEqual(
                alternate_json["identity"]["texture"]["layout_id"],
                SECTOR_PADDED_SETGFX_LAYOUT_ID,
            )
            self.assertEqual(
                alternate_json["renderer"][
                    "native_texture_pack_layout_id"],
                SECTOR_PADDED_SETGFX_LAYOUT_ID,
            )
            self.assertEqual(
                alternate_json["renderer"][
                    "native_texture_selector_to_pixel_bank_mapping"],
                SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
            )
            self.assertEqual(
                alternate_json["renderer"]["native_texture_mapping"],
                SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
            )
            self.assertEqual(
                alternate_json["renderer"]["native_texture_uv_profile"],
                "authored_uv_byte_scaled_256_to_128_nearest_half_up_preview",
            )
            self.assertEqual(
                alternate_json["renderer"]
                ["native_texture_descriptor_origin"],
                "unresolved_not_applied",
            )
            self.assertEqual(
                alternate_json["renderer"]
                ["native_texture_absolute_vram_binding"],
                "unresolved_not_applied",
            )
            self.assertEqual(
                alternate_json["renderer"][
                    "native_texture_material_slot_count"],
                alternate_pack.material_slot_count,
            )
            self.assertEqual(
                alternate_json["renderer"][
                    "native_texture_populated_selectors"],
                list(alternate_pack.populated_selectors),
            )

            foreign_pack = _native_texture_pack("GFX/SET2GFX.BIN")
            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError, "active build"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=temporary_path / "foreign",
                        views=("Front",),
                        texture_pack=foreign_pack,
                    ),
                )

    def test_legacy_psw_texture_semantics_and_pw3_hard_gate_coexist(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            psw_mesh = _native_psw_mesh()
            pw3_mesh = _native_mesh()
            pack = _native_texture_pack()
            build = _native_build(
                temporary_path / "disc", (pw3_mesh, psw_mesh), pack)
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=temporary_path / "out",
                    width=36,
                    height=36,
                    views=("Front",),
                    texture_pack=pack,
                ),
            )

            result = exporter.run()

            self.assertEqual(result.total, 2)
            sidecars = {}
            for record in result.records:
                sidecar = json.loads(
                    (exporter.output_root / record.relative_json).read_text(
                        encoding="utf-8"))
                # Re-run the public sidecar alignment validator over the
                # committed identity and renderer proof.
                build_native_snapshot_sidecar(
                    identity=sidecar["identity"],
                    renderer=sidecar["renderer"],
                )
                sidecars[record.mesh_index] = sidecar

            pw3 = sidecars[0]
            psw = sidecars[1]
            expected_psw_semantics = (
                "bgr555_and_zero_word_transparency_applied; "
                "legacy_psw_material_local_uv_quotient_and_direct_"
                "grayscale_affine_modulation_applied; descriptor_origin_"
                "tpage_clut_offset_stp_abr_runtime_binding_unresolved"
            )
            expected_pw3_semantics = (
                "bgr555_and_zero_word_transparency_applied; "
                "pw3_packet_shade_formulas_recovered_but_effective_"
                "dispatch_unresolved_not_applied; "
                "descriptor_origin_tpage_clut_offset_stp_abr_runtime_"
                "binding_unresolved"
            )

            self.assertEqual(
                psw["identity"]["mesh"]["format"], "PSW/PSV")
            self.assertEqual(
                psw["identity"]["texture"]["mode"],
                "explicit_native_pack")
            self.assertEqual(
                psw["renderer"]["psx_color_semantics"],
                expected_psw_semantics)
            self.assertEqual(
                psw["renderer"]["native_texture_mapping"],
                "selector_S_clut_S_pixel_bank_S_and_31")
            self.assertEqual(
                psw["renderer"]["native_texture_uv_profile"],
                "recovered_signed_16_16_div2_toward_zero_material_local_"
                "pre_origin")
            self.assertEqual(
                psw["renderer"]["native_texture_descriptor_origin"],
                "omitted_material_local_preview_runtime_binding_unresolved")
            self.assertEqual(
                psw["renderer"]["native_texture_absolute_vram_binding"],
                "unresolved_not_applied")
            self.assertEqual(
                psw["renderer"]["psw_psv_raster_profile"],
                PSX_LEGACY_PSW_RASTER_PROFILE_ID)
            self.assertEqual(
                psw["identity"]["renderer_contract"]["profile"]
                ["psw_psv_raster_profile"],
                PSX_LEGACY_PSW_RASTER_PROFILE_ID)
            self.assertNotIn(
                "effective_dispatch_unresolved_not_applied",
                psw["renderer"]["psx_color_semantics"])

            self.assertEqual(pw3["identity"]["mesh"]["format"], "PW3")
            self.assertEqual(
                pw3["renderer"]["psx_color_semantics"],
                expected_pw3_semantics)
            self.assertEqual(
                pw3["renderer"]["native_texture_mapping"],
                "selector_S_clut_S_pixel_bank_S_and_31")
            self.assertEqual(
                pw3["renderer"]["native_texture_uv_profile"],
                "authored_uv_byte_scaled_256_to_128_nearest_half_up_preview")
            self.assertEqual(
                pw3["renderer"]["native_texture_descriptor_origin"],
                "unresolved_not_applied")
            self.assertEqual(
                pw3["renderer"]["native_texture_absolute_vram_binding"],
                "unresolved_not_applied")
            self.assertIn(
                "effective_dispatch_unresolved_not_applied",
                pw3["renderer"]["psx_color_semantics"])
            self.assertNotIn(
                "legacy_psw_direct_grayscale_affine_modulation_applied",
                pw3["renderer"]["psx_color_semantics"])

    def test_skip_existing_requires_complete_byte_exact_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "out"
            mesh = _native_mesh()
            build = _native_build(Path(temporary) / "disc", (mesh,))
            config = PsxNativeBatchConfig(
                output_root=output_root,
                width=32,
                height=32,
                views=("Front", "Back"),
            )
            first = PsxNativeBatchExporter(build, config).run()

            def forbidden_factory():
                raise AssertionError("verified skips must not create a viewport")

            second_exporter = PsxNativeBatchExporter(
                build, config, viewport_factory=forbidden_factory)
            second = second_exporter.run()

            self.assertEqual(first.written, 2)
            self.assertEqual(second.written, 0)
            self.assertEqual(second.skipped_verified, 2)
            self.assertEqual(
                [record.status for record in second.records],
                ["SKIPPED_VERIFIED", "SKIPPED_VERIFIED"],
            )
            self.assertEqual(
                [record.png_sha256 for record in first.records],
                [record.png_sha256 for record in second.records],
            )
            manifest = json.loads(second.manifest_path.read_text("utf-8"))
            self.assertEqual(manifest["execution"]["written"], 0)
            self.assertEqual(manifest["execution"]["skipped_verified"], 2)
            self.assertEqual(
                [record["status"] for record in manifest["records"]],
                ["SKIPPED_VERIFIED", "SKIPPED_VERIFIED"],
            )
            self.assertEqual(
                second.manifest_sha256,
                hashlib.sha256(second.manifest_path.read_bytes()).hexdigest())

    def test_changed_or_partial_existing_pair_fails_before_rendering(self):
        for damage in (
                "png_bytes", "identity", "renderer_extra", "old_schema",
                "missing_json"):
            with self.subTest(damage=damage), \
                    tempfile.TemporaryDirectory() as temporary:
                output_root = Path(temporary) / "out"
                mesh = _native_mesh()
                build = _native_build(Path(temporary) / "disc", (mesh,))
                config = PsxNativeBatchConfig(
                    output_root=output_root,
                    width=30,
                    height=30,
                    views=("Front", "Back"),
                )
                original = PsxNativeBatchExporter(build, config).run()
                png_path = output_root / original.records[0].relative_png
                json_path = output_root / original.records[0].relative_json
                if damage == "png_bytes":
                    with png_path.open("ab") as handle:
                        handle.write(b"changed")
                elif damage == "identity":
                    sidecar = json.loads(json_path.read_text("utf-8"))
                    sidecar["identity"]["view"]["yaw_degrees"] = 123.0
                    json_path.write_text(
                        json.dumps(sidecar, sort_keys=True), encoding="utf-8")
                elif damage == "renderer_extra":
                    sidecar = json.loads(json_path.read_text("utf-8"))
                    sidecar["renderer"]["local_root"] = "C:/private/disc"
                    json_path.write_text(
                        json.dumps(sidecar, sort_keys=True), encoding="utf-8")
                elif damage == "old_schema":
                    sidecar = json.loads(json_path.read_text("utf-8"))
                    sidecar["schema_version"] = 1
                    json_path.write_text(
                        json.dumps(sidecar, sort_keys=True), encoding="utf-8")
                else:
                    json_path.unlink()

                called = False

                def forbidden_factory():
                    nonlocal called
                    called = True
                    raise AssertionError("collision preflight must not render")

                exporter = PsxNativeBatchExporter(
                    build, config, viewport_factory=forbidden_factory)
                with self.assertRaises(PsxNativeBatchCollisionError):
                    exporter.start()
                self.assertFalse(called)
                self.assertEqual(exporter.state, "failed")

    def test_cancellation_stops_between_complete_image_transactions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "out"
            build = _native_build(
                Path(temporary) / "disc", (_native_mesh(),))
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=output_root,
                    width=28,
                    height=28,
                    views=("Front", "Back", "Left"),
                ),
            )

            self.assertEqual(exporter.start().state, "running")
            progress = exporter.step()
            self.assertEqual(progress.completed, 1)
            self.assertEqual(progress.written, 1)
            exporter.request_cancel()
            progress = exporter.step()

            self.assertEqual(progress.state, "cancelled")
            self.assertTrue(progress.cancelled)
            result = exporter.result
            self.assertTrue(result.cancelled)
            self.assertEqual(len(result.records), 1)
            self.assertEqual(len(list(output_root.rglob("*.png"))), 1)
            self.assertEqual(len(list(output_root.rglob("*.png.json"))), 1)
            manifest = json.loads(result.manifest_path.read_text("utf-8"))
            self.assertEqual(manifest["terminal_state"], "cancelled")
            self.assertTrue(manifest["cancelled"])
            self.assertEqual(manifest["execution"]["completed"], 1)
            self.assertEqual(manifest["execution"]["remaining"], 2)
            self.assertEqual(len(manifest["records"]), 1)

    def test_render_failure_exits_snapshot_mode_and_commits_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            created: list[AssetViewport] = []

            def viewport_factory():
                viewport = AssetViewport()
                created.append(viewport)
                return viewport

            build = _native_build(
                Path(temporary) / "disc", (_native_mesh(),))
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=Path(temporary) / "out",
                    width=24,
                    height=24,
                    views=("Front",),
                ),
                viewport_factory=viewport_factory,
            )
            with patch.object(
                    psx_batch_export, "validate_native_renderer_info",
                    side_effect=PsxNativeBatchProvenanceError("forced")):
                with self.assertRaisesRegex(
                        PsxNativeBatchProvenanceError, "forced"):
                    exporter.run()

            self.assertEqual(exporter.state, "failed")
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0]._snapshot_active)
            self.assertIsNone(created[0]._snapshot_saved_state)
            self.assertEqual(
                list((Path(temporary) / "out").rglob("*.png")), [])
            self.assertEqual(
                list((Path(temporary) / "out").rglob("*.json")), [])
            self.assertFalse((
                Path(temporary) / "out"
                / PSX_NATIVE_BATCH_MANIFEST_FILENAME).exists())

    def test_manifest_failure_never_reports_a_false_terminal_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "out"
            build = _native_build(
                Path(temporary) / "disc", (_native_mesh(),))
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=output_root,
                    width=24,
                    height=24,
                    views=("Front",),
                ),
            )
            exporter.start()
            with patch.object(
                    psx_batch_export, "_write_atomic_json_document",
                    side_effect=PsxNativeAtomicWriteError("manifest forced")):
                with self.assertRaisesRegex(
                        PsxNativeAtomicWriteError, "manifest forced"):
                    exporter.step()

            self.assertEqual(exporter.state, "failed")
            self.assertTrue(exporter.done)
            self.assertEqual(len(list(output_root.rglob("*.png"))), 1)
            self.assertEqual(len(list(output_root.rglob("*.png.json"))), 1)
            self.assertFalse((
                output_root / PSX_NATIVE_BATCH_MANIFEST_FILENAME).exists())
            with self.assertRaises(PsxNativeBatchError):
                _ = exporter.result

    def test_atomic_pair_restores_old_pair_if_second_commit_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            png_path = root / "capture.png"
            write_atomic_png_json_pair(
                _solid_image(seed=1), png_path, _minimal_sidecar("old"))
            json_path = png_path.with_suffix(".png.json")
            old_png = png_path.read_bytes()
            old_json = json_path.read_bytes()
            real_replace = os.replace
            replace_calls = 0

            def fail_second_install(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 4:
                    raise OSError("forced second-final failure")
                return real_replace(source, destination)

            with patch.object(
                    psx_batch_export.os, "replace",
                    side_effect=fail_second_install):
                with self.assertRaisesRegex(
                        PsxNativeAtomicWriteError,
                        "forced second-final failure"):
                    write_atomic_png_json_pair(
                        _solid_image(seed=2), png_path,
                        _minimal_sidecar("new"), overwrite=True)

            self.assertEqual(png_path.read_bytes(), old_png)
            self.assertEqual(json_path.read_bytes(), old_json)
            transient = [
                path for path in root.iterdir()
                if path.name.endswith((".stage", ".rollback"))
            ]
            self.assertEqual(transient, [])

    def test_atomic_pair_cleans_reserved_backup_if_pre_move_unlink_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            png_path = root / "capture.png"
            write_atomic_png_json_pair(
                _solid_image(seed=1), png_path, _minimal_sidecar("old"))
            json_path = png_path.with_suffix(".png.json")
            old_png = png_path.read_bytes()
            old_json = json_path.read_bytes()
            path_type = type(root)
            real_unlink = path_type.unlink
            refused_once = False

            def refuse_first_rollback_unlink(candidate, *args, **kwargs):
                nonlocal refused_once
                if candidate.name.endswith(".rollback") and not refused_once:
                    refused_once = True
                    raise PermissionError(
                        "forced rollback reservation unlink failure")
                return real_unlink(candidate, *args, **kwargs)

            with patch.object(
                    path_type, "unlink", autospec=True,
                    side_effect=refuse_first_rollback_unlink):
                with self.assertRaisesRegex(
                        PsxNativeAtomicWriteError,
                        "forced rollback reservation unlink failure"):
                    write_atomic_png_json_pair(
                        _solid_image(seed=2), png_path,
                        _minimal_sidecar("new"), overwrite=True)

            self.assertTrue(refused_once)
            self.assertEqual(png_path.read_bytes(), old_png)
            self.assertEqual(json_path.read_bytes(), old_json)
            transient = [
                path for path in root.iterdir()
                if path.name.endswith((".stage", ".rollback"))
                or path.name.startswith(".psxn-")
            ]
            self.assertEqual(transient, [])

    def test_atomic_pair_rejects_collision_without_touching_old_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            png_path = Path(temporary) / "capture.png"
            write_atomic_png_json_pair(
                _solid_image(seed=1), png_path, _minimal_sidecar("old"))
            json_path = png_path.with_suffix(".png.json")
            old_hashes = (
                hashlib.sha256(png_path.read_bytes()).hexdigest(),
                hashlib.sha256(json_path.read_bytes()).hexdigest(),
            )

            with self.assertRaises(PsxNativeBatchCollisionError):
                write_atomic_png_json_pair(
                    _solid_image(seed=2), png_path,
                    _minimal_sidecar("new"), overwrite=False)

            self.assertEqual(
                old_hashes,
                (
                    hashlib.sha256(png_path.read_bytes()).hexdigest(),
                    hashlib.sha256(json_path.read_bytes()).hexdigest(),
                ),
            )

    def test_atomic_pair_cleans_first_stage_if_second_allocation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            png_path = root / "capture.png"
            real_temporary_path = psx_batch_export._temporary_path
            calls = 0

            def fail_second(parent, name, suffix):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PsxNativeAtomicWriteError(
                        "forced second staging allocation failure")
                return real_temporary_path(parent, name, suffix)

            with patch.object(
                    psx_batch_export, "_temporary_path",
                    side_effect=fail_second):
                with self.assertRaisesRegex(
                        PsxNativeAtomicWriteError,
                        "forced second staging allocation failure"):
                    write_atomic_png_json_pair(
                        _solid_image(), png_path, _minimal_sidecar())

            self.assertFalse(png_path.exists())
            self.assertFalse(png_path.with_suffix(".png.json").exists())
            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows path budget only")
    def test_batch_rejects_overlong_final_paths_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / ("long-native-output-" + "x" * 180)
            build = _native_build(root / "disc", (_native_mesh(),))

            with self.assertRaisesRegex(
                    PsxNativeBatchError, "Choose a shorter output folder"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=output_root,
                        views=("Isometric Front Right",),
                    ),
                )

            self.assertFalse(output_root.exists())

    def test_public_renderer_validator_rejects_pc_or_wrong_source_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            mesh = _native_mesh()
            build = _native_build(Path(temporary) / "disc", (mesh,))
            viewport = AssetViewport()
            try:
                viewport.load_psx_mesh(build, mesh)
                viewport.render_snapshot(QSize(32, 32), QColor("black"))
                raw = viewport.renderer_info
                validated = validate_native_renderer_info(
                    raw, build=build, mesh=mesh, texture_pack=None)
                self.assertEqual(
                    validated["sources"]["psx_build"],
                    build.portable_identity,
                )
                expected_cull = [
                    list(item) for item in
                    mesh_primitive_cull_census(mesh)]
                expected_raw_shades = [
                    list(item) for item in
                    mesh_raw_corner_shade_census(mesh)]
                self.assertEqual(
                    validated["native_primitive_cull_census"],
                    expected_cull)
                self.assertEqual(
                    validated["native_raw_corner_shade_census"],
                    expected_raw_shades)
                for key in (
                        "pw3_corner_storage",
                        "pw3_gpu_packet_corner_order",
                        "pw3_nclip_raw_corner_order",
                        "pw3_raw_reverse_fan_triangles",
                        "pw3_primitive_cull_flag",
                        "pw3_primitive_cull_policy",
                        "pw3_nclip_numeric_domain"):
                    self.assertEqual(validated[key], raw[key])

                for census_key in (
                        "native_primitive_cull_census",
                        "native_raw_corner_shade_census"):
                    with self.subTest(census_key=census_key):
                        missing = json.loads(json.dumps(raw))
                        del missing[census_key]
                        with self.assertRaises(
                                PsxNativeBatchProvenanceError):
                            validate_native_renderer_info(
                                missing, build=build, mesh=mesh,
                                texture_pack=None)
                        mismatched = json.loads(json.dumps(raw))
                        mismatched[census_key][0][1] += 1
                        with self.assertRaises(
                                PsxNativeBatchProvenanceError):
                            validate_native_renderer_info(
                                mismatched, build=build, mesh=mesh,
                                texture_pack=None)

                extra = json.loads(json.dumps(raw))
                extra["caller_extra"] = "not provenance"
                with self.assertRaisesRegex(
                        PsxNativeBatchProvenanceError,
                        "inexact field set"):
                    validate_native_renderer_info(
                        extra, build=build, mesh=mesh, texture_pack=None)

                nested_extra = json.loads(json.dumps(raw))
                nested_extra["sources"]["psx_build"]["root"] = (
                    "C:/private/disc")
                with self.assertRaisesRegex(
                        PsxNativeBatchProvenanceError,
                        "inexact field set"):
                    validate_native_renderer_info(
                        nested_extra, build=build, mesh=mesh,
                        texture_pack=None)

                pc_claim = json.loads(json.dumps(raw))
                pc_claim["pc_openua_source_used"] = True
                with self.assertRaises(PsxNativeBatchProvenanceError):
                    validate_native_renderer_info(
                        pc_claim, build=build, mesh=mesh, texture_pack=None)

                wrong_source = json.loads(json.dumps(raw))
                wrong_source["sources"]["psx_build"][
                    "boot_executable_sha256"] = "ff" * 32
                with self.assertRaises(PsxNativeBatchProvenanceError):
                    validate_native_renderer_info(
                        wrong_source, build=build, mesh=mesh,
                        texture_pack=None)
            finally:
                viewport.close()

    def test_identity_sanitizes_paths_and_never_serializes_build_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            mesh = _native_mesh()
            build = _native_build(
                Path(temporary) / "private" / "disc", (mesh,))
            identity = build_native_snapshot_identity(
                build,
                mesh,
                None,
                view_name="Front",
                view_angles=tuple(VIEW_PRESET_ANGLES["Front"]),
                width=32,
                height=32,
                zoom_percent=100,
            )
            self.assertNotIn(str(build.root), json.dumps(identity))
            self.assertEqual(
                identity["mesh"]["logical_path"], "UNITMODL/UNIT.BIN")
            self.assertEqual(
                identity["mesh"]["native_primitive_cull_census"],
                [list(item) for item in mesh_primitive_cull_census(mesh)])
            self.assertEqual(
                identity["mesh"]["native_raw_corner_shade_census"],
                [list(item) for item in mesh_raw_corner_shade_census(mesh)])

            unsafe_mesh = replace(mesh, logical_path="../../secret.PW3")
            unsafe_build = replace(build, meshes=(unsafe_mesh,))
            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError, "unsafe path"):
                build_native_snapshot_identity(
                    unsafe_build,
                    unsafe_mesh,
                    None,
                    view_name="Front",
                    view_angles=tuple(VIEW_PRESET_ANGLES["Front"]),
                    width=32,
                    height=32,
                    zoom_percent=100,
                )

    def test_manual_identity_captures_full_camera_guides_and_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            mesh = _native_mesh()
            build = _native_build(Path(temporary) / "disc", (mesh,))
            viewport = AssetViewport()
            try:
                viewport.load_psx_mesh(build, mesh)
                viewport.apply_view_preset(
                    "Isometric Front Left", QSize(80, 60), 125)
                camera = viewport.snapshot_camera_info
                identity = build_native_snapshot_identity(
                    build,
                    mesh,
                    None,
                    view_name="Current View",
                    view_angles=viewport.camera_orientation,
                    width=80,
                    height=60,
                    zoom_percent=125,
                    camera_state=camera,
                    guides=True,
                    capture_profile_id=PSX_NATIVE_MANUAL_PROFILE_ID,
                )
                image = viewport.render_snapshot(
                    QSize(80, 60), None, include_guides=True)
                renderer = validate_native_renderer_info(
                    viewport.renderer_info,
                    build=build,
                    mesh=mesh,
                    texture_pack=None,
                )
                sidecar = build_native_snapshot_sidecar(
                    identity=identity, renderer=renderer)
            finally:
                viewport.close()

            self.assertFalse(image.isNull())
            self.assertEqual(sidecar["identity"], identity)
            self.assertEqual(
                identity["capture_profile_id"],
                PSX_NATIVE_MANUAL_PROFILE_ID)
            self.assertTrue(identity["output"]["guides"])
            self.assertEqual(
                identity["view"]["camera_state"], camera)
            self.assertEqual(
                identity["view"]["preset_source"],
                "explicit_full_camera_state")
            self.assertNotIn("batch_profile_id", identity)
            desynchronized = json.loads(json.dumps(identity))
            desynchronized["mesh"]["body_sha256"] = "ff" * 32
            with self.assertRaises(PsxNativeBatchProvenanceError):
                build_native_snapshot_sidecar(
                    identity=desynchronized, renderer=renderer)
            desynchronized_cull = json.loads(json.dumps(identity))
            desynchronized_cull["mesh"][
                "native_primitive_cull_census"][0][1] += 1
            with self.assertRaises(PsxNativeBatchProvenanceError):
                build_native_snapshot_sidecar(
                    identity=desynchronized_cull, renderer=renderer)
            desynchronized_raw_shades = json.loads(json.dumps(renderer))
            desynchronized_raw_shades[
                "native_raw_corner_shade_census"][0][1] += 1
            with self.assertRaises(PsxNativeBatchProvenanceError):
                build_native_snapshot_sidecar(
                    identity=identity,
                    renderer=desynchronized_raw_shades)
            leaked_renderer = json.loads(json.dumps(renderer))
            leaked_renderer["sources"]["psx_build"]["root"] = (
                "C:/private/disc")
            with self.assertRaises(PsxNativeBatchProvenanceError):
                build_native_snapshot_sidecar(
                    identity=identity, renderer=leaked_renderer)

    def test_camera_identity_rejects_malformed_or_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            mesh = _native_mesh()
            build = _native_build(Path(temporary) / "disc", (mesh,))
            valid = {
                "yaw": 0.0,
                "pitch": 0.0,
                "zoom": 1.0,
                "pan": [0.0, 0.0],
                "center": [0.0, 0.0, 0.0],
                "scale": 1.0,
            }
            invalid_states = (
                "not a mapping",
                {key: value for key, value in valid.items()
                 if key != "scale"},
                {**valid, "yaw": float("nan")},
                {**valid, "pan": [0.0, float("inf")]},
                {**valid, "center": [0.0, 0.0]},
                {**valid, "zoom": 0.0},
                {**valid, "scale": -1.0},
            )
            for index, camera in enumerate(invalid_states):
                with self.subTest(index=index), self.assertRaises(
                        PsxNativeBatchProvenanceError):
                    build_native_snapshot_identity(
                        build,
                        mesh,
                        None,
                        view_name="Front",
                        view_angles=(0.0, 0.0),
                        width=32,
                        height=32,
                        zoom_percent=100,
                        camera_state=camera,
                    )
            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError, "do not match"):
                build_native_snapshot_identity(
                    build,
                    mesh,
                    None,
                    view_name="Front",
                    view_angles=(0.0, 0.0),
                    width=32,
                    height=32,
                    zoom_percent=100,
                    camera_state={**valid, "yaw": 1.0},
                )
            with self.assertRaisesRegex(PsxNativeBatchError, "guides"):
                build_native_snapshot_identity(
                    build,
                    mesh,
                    None,
                    view_name="Front",
                    view_angles=(0.0, 0.0),
                    width=32,
                    height=32,
                    zoom_percent=100,
                    guides=1,  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(
                    PsxNativeBatchProvenanceError, "stable ASCII"):
                build_native_snapshot_identity(
                    build,
                    mesh,
                    None,
                    view_name="Front",
                    view_angles=(0.0, 0.0),
                    width=32,
                    height=32,
                    zoom_percent=100,
                    capture_profile_id="manual capture with spaces",
                )

    def test_same_native_identity_is_byte_deterministic_across_output_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            mesh = _native_mesh()
            build = _native_build(temporary_path / "disc", (mesh,))
            results = []
            for name in ("one", "two"):
                results.append(PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=temporary_path / name,
                        width=38,
                        height=34,
                        views=("Isometric Front Right",),
                        background_rgba=(2, 4, 6, 255),
                    ),
                ).run())

            self.assertEqual(
                results[0].records[0].png_sha256,
                results[1].records[0].png_sha256,
            )
            self.assertEqual(
                results[0].records[0].json_sha256,
                results[1].records[0].json_sha256,
            )
            self.assertEqual(
                results[0].manifest_sha256,
                results[1].manifest_sha256,
            )
            self.assertEqual(
                results[0].manifest_path.read_bytes(),
                results[1].manifest_path.read_bytes(),
            )

    def test_proof_helper_rejects_wrong_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mesh = _native_mesh()
            build = _native_build(root / "disc", (mesh,))
            config = PsxNativeBatchConfig(
                output_root=root / "out", width=26, height=26,
                views=("Front",))
            result = PsxNativeBatchExporter(build, config).run()
            png_path = root / "out" / result.records[0].relative_png
            identity = build_native_snapshot_identity(
                build,
                mesh,
                None,
                view_name="Back",
                view_angles=tuple(VIEW_PRESET_ANGLES["Back"]),
                width=26,
                height=26,
                zoom_percent=100,
            )
            with self.assertRaises(PsxNativeBatchCollisionError):
                prove_existing_native_snapshot_pair(
                    png_path, expected_identity=identity)
            sidecar = json.loads(
                png_path.with_suffix(".png.json").read_text("utf-8"))
            wrong_profile = sidecar["identity"]
            wrong_profile["capture_profile_id"] = PSX_NATIVE_MANUAL_PROFILE_ID
            with self.assertRaises(PsxNativeBatchCollisionError):
                prove_existing_native_snapshot_pair(
                    png_path, expected_identity=wrong_profile)

    def test_invalid_configuration_and_restarted_run_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = _native_build(
                Path(temporary) / "disc", (_native_mesh(),))
            with self.assertRaisesRegex(
                    PsxNativeBatchError, "not deterministic"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=Path(temporary) / "out",
                        views=("Custom moving camera",),
                    ),
                )
            with self.assertRaisesRegex(PsxNativeBatchError, "bool"):
                PsxNativeBatchExporter(
                    build,
                    PsxNativeBatchConfig(
                        output_root=Path(temporary) / "invalid-bool",
                        views=("Front",),
                        skip_existing=1,  # type: ignore[arg-type]
                    ),
                )
            exporter = PsxNativeBatchExporter(
                build,
                PsxNativeBatchConfig(
                    output_root=Path(temporary) / "valid",
                    width=24,
                    height=24,
                    views=("Front",),
                ),
            )
            exporter.run()
            with self.assertRaisesRegex(
                    PsxNativeBatchError, "already been started"):
                exporter.run()


if __name__ == "__main__":
    unittest.main()
