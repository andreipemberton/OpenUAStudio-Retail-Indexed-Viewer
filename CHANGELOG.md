# Changelog

All notable changes to the Retail Indexed Viewer fork are recorded here.

This project follows the structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The repository's historical `Stable` tag predates the Retail Indexed Viewer
release line.

## [Unreleased]

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

[Unreleased]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v3.1.0...v4.0.0
[3.1.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/andreipemberton/OpenUAStudio-Retail-Indexed-Viewer/compare/Stable...v2.0.0
