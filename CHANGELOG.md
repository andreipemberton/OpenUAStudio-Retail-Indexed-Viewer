# Changelog

All notable changes to the Retail Indexed Viewer fork are recorded here.

This project follows the structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The repository's historical `Stable` tag predates the Retail Indexed Viewer
release line.

## [Unreleased]

## [6.0.0] - 2026-08-21

### Added

- A separate, read-only native PlayStation source pipeline for explicitly
  selected extracted prototype disc trees. It strictly decodes PSW/PSV/PW3
  meshes, sector-aligned `UNIT.BIN` archives, native vertices, UVs, texture
  selectors, corner-shade fields, and portable source hashes without
  consulting any PC/OpenUA asset root.
- A **PSX Archive** workspace that inventories packed meshes by executable-
  proven runtime model slot when available and by their separate dense archive
  ordinal, sector, offset, body hash, and counts. It preserves loose native
  filenames and keeps the unrelated `VEHICLE.TXT` roster visibly unmapped
  instead of guessing unit identities.
- Parser v3 executable-gated runtime identity. Packed meshes retain their dense
  archive ordinal while exact boot-executable allocation tables independently
  prove runtime model slots, including empty placeholders and any trailing
  non-slot sentinel. Friendly names remain unmapped, and loose numeric filenames
  are not assumed to be packed slots.
- Strict read-only inventory for the recovered 352-byte PV2 version-1 static
  effect grammar. PV2 geometry, UV, selector, shade, and source hashes are
  preserved without claiming timing, playback, attachment, or model-slot
  binding. Only the exact June executable/overlay/asset evidence triplet
  records V56B as a conditional near-view model override rather than animation;
  that conclusion is not generalized to other builds.
- Strict decoding for the compact 15/16 June `SET1GFX.BIN` through
  `SET6GFX.BIN` material tables: 128 selector CLUTs, 32 native 128 x 128 4bpp
  pixel banks, low-nibble-first indices, PSX BGR555/STP colors, selector
  `S -> CLUT S + bank S&31`, and resolved-zero-word transparency.
- Strict decoding for the separate December-through-May sector-padded
  `SETnGFX.BIN` tables. Their 128 selectors use direct per-slot CLUT/pixel
  payloads, exact `0xBA` allocation padding, and explicit empty-sector markers;
  only the three complete recovered sizes are accepted.
- Native-source renderer provenance covering the boot executable, mesh/archive
  and stream hashes, an optional explicitly selected native texture set when
  one is chosen, mapping rules, parser/profile versions, and explicit remaining
  reconstruction limits. Manual native snapshots now commit an atomic PNG plus
  `.png.json` sidecar under `psx_native_manual_snapshot_v2`. Snapshot schema v3
  retains and validates parser-v3 primitive censuses, separate runtime-slot and
  dense-ordinal identity, executable evidence, friendly-name status, and static
  PV2 inventory rather than leaving that proof only in session memory.
- A dedicated native mesh exporter and **Batch Native PSX Meshes** panel in
  Snapshot Studio's **PSX Archive** workspace. It freezes a selected subset of
  ten fixed views, topology-only or the current explicitly selected native SET
  pack, transparent PNG output, paused animation, and hidden guides. Each image
  and sidecar is a transactional pair; a root
  `psx_native_batch_manifest.json` records complete and safely cancelled runs.
  Profile `psx_native_mesh_batch_v2` and manifest schema v3 copy primitive
  censuses and the distinct runtime-slot/dense-ordinal evidence into every mesh
  record.
  **Skip verified existing** resumes only from complete pairs whose frozen
  identity, parser-v3 censuses, and byte hashes still match.
- Pure, evidence-bounded GPU/GTE helpers for PW3 direct/tinted packet colors,
  zero-word and STP-conditioned fragment semantics, all four saturated ABR
  equations, integer GTE screen projection/NCLIP/AVSZ, dithering, and recovered
  ordering-table insertion. They remain dormant in the viewer wherever the UA
  descriptor, transform, dither-enable, or full traversal binding is unresolved.

### Changed

- The selectable v5 `textured_psx_prototype` PC-asset styling preset is
  removed. Its identifiers remain reserved only for historical manifest and
  output-collision recognition; they never alias the corrected `psx_native` /
  `psx_native_asset_v1` source pipeline.
- Native PSX mode now clears PC families, materials, Retail Indexed resources,
  and VANM state before loading a native scene. Returning to a PC renderer
  requires a real PC/OpenUA family and restores its independent preferences.
- Snapshot Studio now has a dedicated **PSX Archive** tab. PC complete-VP batch
  export and PC VANM controls are disabled for native sources rather than
  generating mislabeled or nondeterministic output; native meshes use their
  separate mesh batch controller.
- Native compact and sector-padded texture packs now default to topology-only
  until the operator explicitly selects an available SET variant. Each
  validated selector-table mapping is recorded separately from the still-
  unproven mesh/environment-to-SET affinity.
- Initial native mesh loads and explicit camera resets now frame the complete
  decoded model in the current viewport. Reloading the same mesh for an
  operator-selected native texture pack preserves the operator's current
  orbit, zoom, pan, center, and scale instead of silently fitting again.

### Fixed

- Corrected the v5 feature interpretation: the requested PlayStation mode now
  looks for and renders PlayStation prototype assets rather than applying a
  PlayStation-like presentation to PC/OpenUA assets.
- Extracted-build discovery now requires an actual `SYSTEM.CNF` file and
  `UNITMODL` directory; a missing directory can no longer be mistaken for the
  process working directory.
- PW3 parser v3 preserves all four raw file slots and implements the recovered
  June executable's primitive contract: GPU packet order `1,0,2,3`, equivalent
  raw-slot reverse fan triangles `(0,2,1)` and `(0,3,2)`, and little-endian
  prefix bit `0x4000`. Bit-clear primitives pass only when NCLIP on raw slots
  `1,0,2` is strictly positive; bit-set primitives bypass that source cull and
  remain two-sided.
- PSW/PSV no longer falls back to guessed two-sided presentation. Recovered
  December/March packet order `1,0,2,3` and unconditional strict-positive NCLIP
  are applied before clipping. With an explicitly selected native SET pack,
  the bounded affine renderer also applies the executable-backed direct
  grayscale corner colors, exact `(texel5 * shade8) >> 4` modulation, nearest
  sampling, and zero-word discard. Integral signed 16.16 UV component `signed`
  uses the executable-exact material-local quotient
  `q = ((signed >> 16) + (1 if signed < 0 else 0)) >> 1`, sampled directly as
  a pre-origin `0..127` texel coordinate. Descriptor origin and absolute
  VRAM/byte-wrap equivalence are not inferred, and the selected SET remains an
  operator-selected variant with no proven mesh affinity. Preserved prefix
  words still receive no unsupported culling, blending, or material meaning.
- PW3 direct grayscale and `(shade * tint_channel) >> 8` tinted formulas are
  preserved as recovered evidence, but effective UNIT-render dispatch, caller
  tint, and PW3 shade application remain explicit hard gates rather than
  guessed renderer behavior. Runtime descriptor origins, TPage and CLUT-offset
  provenance, and STP/ABR application remain unresolved for both PSW/PSV and
  PW3.
- PW3 and PSW/PSV NCLIP now share their correctly documented numeric boundary:
  the recovered raw-corner order and strict-positive policies are applied on
  the viewer's floating-point projection, without claiming exact GTE
  fixed-point rounding at the edge-on boundary.
- Native viewer and exporter entry points now apply one central fail-closed
  frozen-object contract before rendering or serializing provenance. It
  cross-checks safe logical paths and hashes, decoded vertices and raw face
  records, PSW/PW3 format-specific UV/flag fields, canonical mesh/PV2/texture
  inventories, exact compact or sector-padded SET layout, dense archive
  ordering, and a complete executable model-slot/empty-slot bijection. Relabeled
  PW3-as-PSW objects, mutated PV2 metadata, duplicate slots, partial allocation
  tables, fabricated texture layouts, and decoded fields inconsistent with raw
  records are rejected before viewport mutation or output.
- Native manual snapshot suggestions no longer inherit a remembered directory
  inside the extracted prototype source. The suggestion is moved outside that
  tree, and any manually chosen output path that resolves inside it is refused
  before rendering or writing. If either the source or candidate path cannot be
  resolved safely, manual export fails closed without rendering, writing, or
  creating the candidate directory.
- The native mesh exporter's core API, not only its Snapshot Studio panel,
  resolves and enforces the outside-source boundary before planning jobs or
  creating directories. Direct callers cannot target the source root, a
  descendant, a lexical `..` alias, or a filesystem alias resolving into the
  extracted tree; boundary-resolution failures also fail closed.
- Direct native archive/executable reads reject symlink or junction ancestry.
  Manual and batch output transactions re-resolve the source/output boundary
  before committing manifests or PNG/JSON pairs and after staging, so an
  absent path swapped to a filesystem alias cannot redirect a completed
  transaction into the recovered source tree.
- Native atomic output now uses bounded, target-hash-derived temporary
  basenames instead of repeating a potentially long final filename. On
  Windows, final PNG, `.png.json`, and batch-manifest paths are preflighted
  against a conservative 248-character budget. Temporary-file allocation
  failures are wrapped as native atomic-write errors, and failure cleanup
  removes any partially reserved staging files, including the first stage when
  allocation of its partner fails. Empty rollback reservations are tracked and
  cleaned even if their pre-move unlink fails; a rollback file containing the
  user's previous bytes is retained only when restoration itself fails.
- Loading a PC/OpenUA family after a native PSX source now restores the saved PC
  snapshot format, hides the native notice, and re-enables and resynchronizes
  the applicable Retail TRACY and distance-fade controls. The native notice
  distinguishes applied PW3 bit-14/NCLIP behavior, applied PSW/PSV packet and
  direct-grayscale behavior, the still-hard-gated PW3 shade dispatch, and the
  unresolved descriptor TPage/CLUT-offset/STP/ABR bindings for both formats;
  legacy PC batch guidance directs native-source users to the
  **PSX Archive** mesh batch instead.
- A fitted or operator-controlled native view that contains no visible model
  pixels now receives a non-blocking status hint suggesting **Back**,
  **Bottom**, or **Reset camera** to fit. The clean-alpha advisory probe keeps
  the active PW3 bit-14/NCLIP culling policy and renderer provenance unchanged.
- Snapshot source replacement now commits one source-activation transaction.
  It refreshes **Current View** and the camera/renderer restored on Snapshot
  exit, synchronizes title, Object Info, renderer and preset selectors, Reset
  availability, passive-resize state, and native visibility truth, and clears
  those source-derived states over an empty viewport. Renderer-only re-entry
  to an already installed native scene preserves its wireframe/materials
  diagnostic camera rather than reloading or fitting the mesh again.

### Validation

- Full canonical-enabled headless discovery on the v6.0.0 source tree ran
  749 tests with `OK`: zero failures or errors and one optional skip. The
  corpus-enabled PSX discovery passes 210/210, the central native-contract
  suite passes 30/30, and the focused contract/viewer/export set passes 83/83.
  `compileall -q .` and `git diff --check` also pass.
- The frozen native-viewer QA v3 gate renders all 1,789 build-local
  mesh-by-available-pack combinations and resolves all 3,901 used selectors in
  116.041 seconds, with zero render failures, PC-source uses, fallbacks, or
  frame-edge contacts. All 60 transparent Current View outputs have successful
  source-correct Back or Bottom follow-ups, and manual review of 12 current,
  forced-two-sided, and difference-mask representatives finds no unexplained
  half-quad hole, winding tear, or inside-out surface.
- The corpus gate separately inventories all 33 December PSW/PSV files,
  including `TEST/TANK.PSV` and `TEST_ART/DEFAULT.PSV` outside the 31-mesh
  `UNITMODL` viewer inventory: 1,346 faces, 5,384 UV pairs, and 10,768 signed
  fixed components, whose recovered material-local quotients all remain in
  `0..127`.
- Native-source validation uses an explicitly supplied local corpus. No
  prototype executable, archive, mesh, texture pack, or other native
  PlayStation source bytes are committed.

## [5.0.0] - 2026-08-18

### Added

- An opt-in **PSX prototype visualization (experimental)** viewer and Photo
  Studio mode for loaded PC/OpenUA asset families. Its fixed v1 contract uses
  affine texture interpolation, nearest-neighbor sampling, and hard polygon
  edges while explicitly declining to claim PSX asset decoding or
  cycle-accurate emulation.
- Renderer-neutral provenance for the PSX visual profile, including its stable
  ID/version, PC/OpenUA source-asset pipeline, applied raster policies, and
  explicit status for native resolution, fog, BGR555/STP, dithering, vertex
  snapping, and primitive queues that remain unvalidated and unapplied.
- A matched Hauptstation comparison sheet generated from the release
  implementation with identical cameras, animation states, output dimensions,
  and presentation background. The bounded public package records its source
  hash, exact profile identity, measurements, and SHA-256 without publishing
  local paths or raw game assets.

### Changed

- Manual PSX-profile snapshots fail closed and receive a distinct
  `_PSX_PROTO_VISUAL_V1` suggested filename. Complete-model batch export keeps
  PSX output separate from OpenUA and Retail Indexed provenance, and
  `Skip existing` requires the complete versioned PSX profile to match.
- Renderer documentation now records the evidence-backed boundary for a later
  read-only PW3/`UNIT.BIN`, `mhwanh`, `.GFX`, and `DAT`/`IND` browser without
  implying that the v1 presentation mode already loads those assets.

### Validation

- Canonical-enabled headless discovery covers 517 tests: 516 pass, with one
  optional `UA_RC1` fixture test skipped and no failures or errors.
- The focused PSX profile, UI, Snapshot Studio, batch-integrity, OpenUA
  projective, Retail Indexed integration/canonical, and depth-renderer gate
  passes 118/118 tests; all 95 tracked/candidate Python files compile.
- The bounded comparison package contains four files totaling 2,034,037 bytes.
  All three non-self files match `SHA256SUMS.txt`; the 3072 x 1678 PNG contains
  only `IHDR`, `IDAT`, and `IEND` chunks and carries no embedded local-path or
  text metadata.

## [4.0.0] - 2026-08-18

### Added

- The indexed viewport now has an explicit `source_atts_only` unmapped-polygon
  policy for source-forensic captures. It reproduces source submission by
  retaining AMESH ATTS and AREA ADE mappings while omitting skeleton polygons
  absent from every source material mapping, inventories every omission in
  renderer provenance, and never invents a material or substitutes the OpenUA
  preview.
- A bounded public reference corpus of **51** animated 1024 x 1024
  retail-indexed unit turntables: 47 deduplicated combat models from the five
  shipped faction scripts and four host/command models from `ROBOS.SCR`.
- A 51-unit contact index, navigable per-unit GIF catalog, capture-provenance
  manifest, and exact SHA-256 list. The Git boundary includes the final GIFs
  and index but excludes all 3,060 lossless PNG masters and proprietary game
  source assets.

### Changed

- Incomplete ATTS coverage still fails closed by default. The source-ATTS-only
  path is opt-in so an editor parsing or extraction error cannot silently
  remove geometry from an ordinary exact export.
- Animated reference loops use deterministic cycle fitting. Authored image/UV
  order and frame-duration proportions are retained, but timelines are retimed
  through integral cycles so camera and material state both close exactly at
  2.4 seconds; they are not represented as native-speed gameplay captures.

### Validation

- Canonical-enabled headless discovery after the source-ATTS policy change:
  **500 tests run, 499 passed, zero failures, zero errors, and one skipped**.
  The skip is the optional legacy `UA_RC1` corpus, which is not installed on
  this machine. The gate includes a real-data `VP_BRGRO` framebuffer oracle
  proving that only authored orphan polygon `root/36` is omitted.
- All 51 GIFs passed exact palette and temporal-closure validation. Pillow and
  FFmpeg independently decoded all **3,060** GIF frames byte-identically to
  their lossless PNG masters, and all published media bytes were rehashed
  before packaging.

## [3.1.0] - 2026-08-17

### Fixed

- Verified asset-family exports now clear saved VANM UV dirty baselines by the
  actual `(animation_name, group_index)` key after the corresponding animation
  is successfully written and reparsed. Baselines for unrelated, non-exported
  animations remain dirty instead of being discarded.
- Supported SET.BAS resource rows now expose **Preview** in their context menu.
  The command previews the row under the pointer without changing the current
  tree selection, including explicit embedded-skeleton routing.
- AREA structural export coverage now supplies a complete animation and texture
  dependency graph for its successful save/reload case, while a separate test
  proves that a missing `MODEL.ANM` still fails closed, writes no partial
  output, and leaves source bytes unchanged.
- Startup-selector tests now exercise the fixed, non-scrolling tool-card panel
  through its current API and real Qt input events. Snapshot Studio tests now
  verify its intentional width-aware scroll wrapper, and standalone-SKLT tests
  respect the viewport's read-only edit-state properties.

### Validation

- Full canonical-enabled headless discovery: **497 tests run, 496 passed, zero
  failures, zero errors, and one skipped**. The skip is the optional legacy
  `UA_RC1` corpus, which is not installed on this machine.
- The focused export, AREA, window-contract, startup-selector, and Snapshot
  Studio maintenance gate passes **76/76** tests.

## [3.0.0] - 2026-08-17

### Added

- A public 4K comparison sheet for the Hauptstation, Zeppelin, and Mnosjetz,
  showing the normal OpenUAStudio RGB preview beside the source-traced
  retail-indexed reconstruction with matched cameras and animation states.
- An opt-in Snapshot Studio **Retail AREA distance fade (1400/600)** control,
  off by default, which applies the source-traced radial per-vertex fade only
  to mapped gradient-shaded faces carrying `AREA_FLAG_DPTHFADE`.
- An explicit toolbar **Enable animations** checkbox for continuous VANM and
  effect-frame playback, with the existing frame-step and reset controls kept
  available while playback is paused.
- Per-image and per-run distance-fade provenance, including requested/effective
  state, runtime visibility limit, fade start/length, distance space, and
  formula for exact indexed output. Enabled faded PNGs are committed only after
  the raster statistics prove that complete profile, and resume metadata uses
  strict native JSON numeric types.
- Upstream's expanded Collision Editor workspaces for collision spheres,
  vanilla Fire Points, OpenUA/vanilla Gun Points, and opt-in Cockpit View
  camera-offset preview, including runtime model/aspect controls.

### Changed

- The distance-fade selector uses the normal retail gameplay profile (1400-unit
  visibility, 600-unit fade, start 800). Documentation now distinguishes the
  asset's eligibility flag from runtime configuration, including the original
  BSA class-initialization 4096/600 default and potentially different
  mission-brief values. The viewer applies 1400/600 at its auto-fit camera
  distance; it does not claim a recovered mission/world placement.
- Complete-model batch export freezes the fade selection at batch start but
  remains animation-deterministic: its hidden viewport is paused and every
  source is reset to the initial frame.
- Batch filenames remain stable across fade choices. **Skip existing** now
  requires the prior renderer/destination profile and, when enabled, the full
  fade profile ID, limits, distance space, and formula to match; legacy indexed
  provenance with no fade field is interpreted as fade disabled.
- Upstream `main` through `74bea393` is merged with both parent histories
  preserved. The fork retains its explicit **OpenUA preview** / **Retail
  indexed (reconstructed)** selector, diagnostic TRACY controls, and strict
  export provenance rather than adopting upstream's automatic renderer policy.
- The shared assembly viewer now exposes the cockpit-facing winding hook and a
  configurable near clip used by the upstream Cockpit View without changing
  the indexed renderer's retail whole-face culling path. The fork also retains
  `depth_renderer.py` and `projective_texture_coefficients()` for its selectable
  OpenUA preview path.
- The Windows packaging specification includes NumPy as an explicit hidden
  import for indexed rendering; this is build metadata, not a claim that a new
  binary package was produced or validated. Version 3.0.0 remains a source-only
  fork release: the stale executable introduced on the merged upstream line is
  absent from the v3 tree and source archive, although its historical blob
  remains reachable in the preserved merge ancestry. No proprietary game data
  is attached.

### Fixed

- Selecting a Gun Point now activates its property workspace before gizmo
  movement, and cockpit controls refresh consistently without silently enabling
  authored camera output.
- Collision-editor context submenus retain their PySide wrappers for the full
  popup lifetime instead of exposing deleted native `QMenu` objects.
- Upstream collision-window tests now describe the current compact layout,
  bottom-left selection overlay, Windows-safe package paths, and explicit
  cockpit opt-in behavior.

## [2.0.0] - 2026-08-15

### Added

- An optional **Textured - Retail indexed (reconstructed)** viewport and
  Snapshot renderer that preserves source palette indices through the model
  pass before final RGBA display conversion.
- Source-table discovery, validation, and provenance recording for the active
  256-color palette plus matching `SHADERMP` and `TRACYRMP` remap tables.
- A Qt-independent indexed raster backend with deterministic NumPy and pure
  Python implementations.
- Source-derived material adaptation for static textures, animated materials,
  indexed shading, clear-TRACY, and flat/LUM-TRACY effects.
- A Snapshot Studio **Flat/LUM-TRACY destination** selector with two explicit
  policies:
  - **Live framebuffer - retail**, the canonical default.
  - **Force palette row - diagnostic**, a noncanonical `0..255` table-inspection
    mode with a resolved-palette swatch.
- Canonical regression oracles for the Taerkasten Hauptstation, Zeppelin, and
  Mnosjetz, including a separate forced-row-13 Hauptstation diagnostic oracle.
- Per-image and per-run renderer provenance for Snapshot batch exports,
  including source hashes, index-buffer hashes, requested/effective TRACY
  destination policy, fixed initial framebuffer index, and diagnostic row.
- Source-traced documentation in `RETAIL_INDEXED_RENDERER.md` describing the
  recovered behavior, reconstruction boundaries, validation commands, and
  current fidelity limits.

### Changed

- Ordinary indexed mapped faces now use the recovered constant-brightness
  `SHADERMP` row conversion rather than treating authored shade as a direct
  table row.
- Valid source material codes are restricted to combinations published by the
  retail AMESH dispatcher; unsupported combinations fail closed.
- Untextured NNN polygons use the retail opaque index-zero `ZeroSpan` behavior
  instead of inventing a color from unused ATTS metadata.
- Clear-TRACY now skips numeric source index zero before shading and preserves
  nearest-neighbor effect-card sampling.
- Flat/LUM-TRACY now bypasses chroma and `SHADERMP`, then evaluates raw texels
  as `TRACYRMP[current_destination][raw_source]`.
- Transparent indexed faces are replayed using a source-derived whole-face
  publish-depth/LIFO schedule while respecting opaque occlusion.
- Exact indexed mode performs retail-style whole-source-face culling before
  fan triangulation and stabilizes exact cardinal camera presets.
- Animation catch-up uses bounded cycle resolution instead of an unbounded
  per-frame loop after long pauses.

### Fixed

- Hauptstation lightning no longer uses OpenUAStudio's ordinary RGB additive
  approximation in reconstructed indexed mode.
- Zeppelin clear-TRACY propellers retain their distinct transparency behavior;
  Mnosjetz flat-TRACY propellers remain on the flat effect path.
- Textured faces no longer bypass indexed shade-table processing.
- Indexed render failures and fallback output can no longer be mislabeled as a
  successful reconstructed-retail export.
- Snapshot lifecycle changes no longer expose stale indexed hashes or renderer
  state after returning to the OpenUA preview.
- Live-framebuffer manifests record no inactive forced-row operand.
- **Skip existing** refuses a populated batch folder unless its prior
  `run_info.json` proves the same renderer and TRACY destination profile.
- Batch PNG promotion is atomic, retained older files are classified as
  unverified, and ZIP creation follows the manifest whitelist rather than a
  recursive directory sweep.

### Security and provenance

- Installed game archives, palettes, textures, animations, and remap tables
  remain read-only local inputs and are never embedded in this repository.
- Ambiguous palette candidates fail closed unless every candidate resolves to
  a complete, byte-identical palette/`SHADERMP`/`TRACYRMP` profile.
- Diagnostic forced-row output receives an explicit filename suffix and
  renderer classification so it cannot be mistaken for canonical retail
  reconstruction output.

[Unreleased]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v6.0.0...HEAD
[6.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v5.0.0...v6.0.0
[5.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v4.0.0...v5.0.0
[4.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v3.1.0...v4.0.0
[3.1.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/Stable...v2.0.0
