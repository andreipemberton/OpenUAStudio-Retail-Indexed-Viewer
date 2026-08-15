# Retail indexed renderer (reconstructed)

This fork adds an optional, read-only rendering path for source-faithful Urban
Assault asset inspection. It exists because the normal OpenUA/OpenUAStudio
preview is a modern RGB approximation: that path is useful for editing, but it
cannot reproduce effects whose authored result depends on palette-index lookup
tables and the destination pixel already in the framebuffer.

The feature is deliberately labeled **reconstructed**. It is derived from the
available game data, file formats, OpenUA source, and reproducible visual
oracles; it is not presented as cycle-accurate emulation of the 1998 retail
rasterizer.

## Use

Install a 64-bit Python runtime and the viewer dependencies, then launch the
viewer-only entry point:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-viewer.txt
.venv\Scripts\python.exe viewer_main.py
```

Open a local `.base` asset or `SET.BAS`, select a set-specific data root when
prompted, and choose one of the two textured modes:

- **Textured - OpenUA preview** keeps the existing renderer and remains the
  default.
- **Textured - Retail indexed (reconstructed)** enables the indexed path.

The reconstructed path requires a legally obtained local 256-entry palette and
the matching `REMAP/SHADERMP.ILB[M]` and `REMAP/TRACYRMP.ILB[M]` tables. These
files are read-only inputs. They are neither embedded in nor distributed by
this repository.

## What the indexed path does

For the model pass, the renderer retains unsigned eight-bit palette indices
until the final conversion to an RGBA image:

1. Resolve the raw source ILBM or active VANM frame and its source indices.
2. Reject the source palette's yellow chroma before any lookup.
3. Sample the source indices without RGB interpolation.
4. Apply authored face shade as `SHADERMP[shade][source_index]`.
5. Write opaque and clear-TRACY samples in palette space.
6. Defer flat-TRACY pieces and composite them in stable source order as
   `TRACYRMP[source_index][destination_index]`.
7. Convert the completed index buffer through the selected palette for display
   or PNG export.

This distinction matters. A flat effect lookup is generally nonlinear and
order-sensitive, so ordinary alpha or additive RGB blending cannot reproduce
it. Clear-TRACY propeller cards also need source-index chroma rejection and
nearest indexed sampling; smoothing their RGBA edges creates colors the game
never authored.

The implementation is split into three layers:

- `indexed_renderer.py` is a Qt-free deterministic rasterizer with NumPy and
  pure-Python backends.
- `indexed_family_adapter.py` resolves exact source materials, animation frames,
  palette/remap provenance, and unsupported combinations.
- `assembly_viewer.py` inserts the indexed model pass after the existing
  camera-space BSP ordering, then replays normal editor overlays.

## Fail-closed export and provenance

Interactive display may fall back to the OpenUA preview so the user can still
inspect an asset. The status text and renderer metadata identify that fallback.
Manual and batch exports requested as Retail Indexed do **not** save the
fallback as if it were an exact result.

Exact export refuses, among other cases:

- absent, malformed, or structurally incompatible palette/remap tables;
- ambiguous palette candidates unless every candidate has a complete,
  byte-identical palette/SHADERMP/TRACYRMP profile;
- incomplete ATTS polygon-to-material mappings;
- incomplete source UV mappings;
- mapped-TRACY materials, whose retail semantics are not yet established; and
- output dimensions above the active backend's bounded memory budget.

Batch PNGs are written to a sibling temporary file and atomically promoted. An
older output that survives a failed overwrite is explicitly classified as
retained and unverified. ZIP creation uses the manifest's authorized file list,
not a recursive sweep of the output directory. Per-image records distinguish
requested renderer, effective renderer, fallback/error reason, source hashes,
and—when an indexed image was actually written—the exact index-buffer hash.

## Current limits

- The lookup orientation is strongly constrained by local table invariants and
  source-derived render oracles, but the original retail scanline instruction
  sequence has not been traced directly.
- A non-black custom RGB background is a presentation composite after indexed
  rendering. It is not used as the destination index for TRACY lookups.
- NumPy exports are capped at 16,777,216 pixels (4096 x 4096). The deterministic
  pure-Python fallback is capped at 1,048,576 pixels.
- VANM durations are interpreted as ticks of the game's 1024 Hz clock. Live
  playback uses monotonic elapsed time and skips complete animation cycles
  after long pauses instead of performing an unbounded catch-up loop.

## Reproducible checks

The synthetic backend, adapter, UI-integration, and opt-in canonical tests can
be run with:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest -v `
  tests.test_indexed_renderer `
  tests.test_indexed_family_adapter `
  tests.test_indexed_viewer_integration
```

The canonical test distributes no proprietary content. If local game data and
the separately extracted test assets are available, set
`OPENUA_CANONICAL_PROJECT_ROOT` and `OPENUA_GAME_SET1_ROOT`, then add
`tests.test_indexed_canonical_assets` to the command. It validates the
Hauptstation lightning, Mnosjetz flat-TRACY propellers, and Zeppelin
clear-TRACY propellers against frozen index-buffer hashes.

## Scope and licensing

The full OpenUAStudio tree is retained because Snapshot Studio shares parsers,
assembly logic, and UI components with the broader workbench. `viewer_main.py`
is the viewer-only launch surface; the original tool selector remains available
through `main.py`.

Code remains under the repository's GPL-3.0-only license and upstream notices.
Urban Assault models, textures, palettes, remap tables, names, and other game
content remain third-party property and are not redistributed here.
