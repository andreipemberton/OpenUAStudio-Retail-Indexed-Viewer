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
prompted, and choose one of the textured renderer modes:

- **Textured - OpenUA preview** keeps the existing renderer and remains the
  default.
- **Textured - Retail indexed (reconstructed)** enables the indexed path.
- **Textured - PSX prototype visualization (experimental)** applies the fixed
  affine/nearest/hard-edge v1 presentation profile documented in
  [PSX_PROTOTYPE_PROFILE.md](PSX_PROTOTYPE_PROFILE.md); it does not decode PSX
  assets or emulate the PSX renderer.

Snapshot Studio's **Background** group contains a separate **Flat/LUM-TRACY**
policy control:

- **Live framebuffer - retail** is the canonical default. The frame begins at
  palette index zero and each effect texel reads the actual current destination
  beneath it.
- **Force palette row - diagnostic** makes every flat/LUM-TRACY lookup read one
  selected row from `0..255`. It is useful for table inspection but deliberately
  disables live destination reads, so the UI and export metadata mark it
noncanonical. It does not affect clear-TRACY.

The adjacent swatch is resolved from the indexed display palette. The numeric
palette index remains authoritative even when two entries share the same RGB.
Manual export suggestions append `_TRACY_FORCE_NNN_DIAGNOSTIC` while forcing is
active; users can still choose another filename explicitly.

Snapshot Studio also exposes **Retail AREA distance fade (1400/600)** when the
reconstructed indexed renderer is selected. It is an explicit opt-in and is
off by default, preserving the v2 close-camera appearance. When enabled, it
applies only to mapped gradient-shaded faces carrying the authored
`AREA_FLAG_DPTHFADE` flag. The standard gameplay profile starts at 800 source
model units, fades over 600 units, and reaches palette index zero at the
1400-unit visibility limit. Panning and lens zoom do not change that source
distance.

The flag is all the asset stores; it does not contain a visibility profile.
The runtime supplies those values for the active context. The original BSA
class-initialization default is a 4096-unit visibility limit with a 600-unit
fade (start 3496), and mission-brief or other runtime contexts may supply a
different profile. The checkbox intentionally selects the normal 1400/600
gameplay profile rather than pretending that one value is intrinsic to the
model. The asset viewer evaluates it at its current auto-fit camera distance;
that is reproducible source-model space, but it is not a recovered mission or
world placement.

The top toolbar's **Enable animations** checkbox controls continuous VANM and
effect-frame playback for all textured renderer modes. Unchecking it freezes the
displayed frame; step and reset remain explicit. Manual exports capture that
displayed state. Complete-model batch export stays deterministic: it captures
the renderer controls once at batch start, applies them to a hidden viewport,
keeps animation playback paused, and resets each source to its initial frame.

The reconstructed path requires a legally obtained local 256-entry palette and
the matching `REMAP/SHADERMP.ILB[M]` and `REMAP/TRACYRMP.ILB[M]` tables. These
files are read-only inputs. They are neither embedded in nor distributed by
this repository.

## What the indexed path does

For the model pass, the renderer retains unsigned eight-bit palette indices
until the final conversion to an RGBA image:

1. Clear the model framebuffer to palette index zero, matching the recovered
   retail world-frame path.
2. Resolve the raw source ILBM or active VANM frame and its source indices.
3. Sample source indices without RGB interpolation.
4. Resolve only material-flag combinations actually published by the retail
   AMESH dispatch table. Its untextured NNN routine is an opaque ZeroSpan that
   writes palette index zero; ATTS color does not select a solid color there.
5. For ordinary mapped faces, reproduce the source's constant-brightness
   conversion: authored shade 0..2 bypasses `SHADERMP`, 3..253 selects row
   `(253 * authored_shade + 0x180) >> 8`, and 254..255 becomes opaque index
   zero. Numeric source index zero is not implicitly transparent.
6. For clear-TRACY faces, reject numeric source index zero, then apply that same
   converted shade row and write the remaining samples.
7. For flat/LUM-TRACY faces, bypass both the chroma test and `SHADERMP`, and
   composite each raw texel as
   `TRACYRMP[current_background_index][raw_source_index]`.
8. Reconstruct the retail glass-stack pass: the viewer's opaque BSP pieces
   establish a deterministic visible background and occlusion order, then each
   transparent source face is replayed contiguously by descending
   source-derived publish depth (LIFO), rather than interleaving nonlinear BSP
   fragments.
9. Enforce the source-compatible pre-clip, whole-source-face visibility test in
   exact mode. This prevents a later fan from reversing the source-face cull
   and normally selects one winding of an authored two-sided effect card;
   exactly edge-on zero-area faces may enter and rasterize no samples.
10. If the optional AREA distance-fade profile is enabled, add the radial
    source-model distance term to each eligible vertex's authored shade before
    screen-linear interpolation selects the `SHADERMP` row. Faces without
    `AREA_FLAG_DPTHFADE` retain their ordinary constant-shade path.
11. Convert the completed index buffer through the selected palette for display
   or PNG export.

This distinction matters. A flat effect lookup is generally nonlinear and
order-sensitive, so ordinary alpha or additive RGB blending cannot reproduce
it. Clear-TRACY propeller cards also need an exact raw-index-zero test and
nearest indexed sampling; smoothing their RGBA edges creates colors the game
never authored.

These operand and ordering rules are now traced directly to the original x86
and 68k VFM routines. Both `span_lnf` implementations form the flat-TRACY
offset as `(background << 8) | raw_texture_texel`, with no shade or chroma
branch. The software spangine pushes transparent spans during the front-to-back
publish pass and pops its glass stack LIFO at `End3D`.

The implementation is split into three layers:

- `indexed_renderer.py` is a Qt-free deterministic rasterizer with NumPy and
  pure-Python backends.
- `indexed_family_adapter.py` resolves exact source materials, animation frames,
  palette/remap provenance, and unsupported combinations.
- `assembly_viewer.py` supplies BSP visibility/occlusion information and the
  whole-face source-derived publish-depth key, then replays normal editor
  overlays.

## Fail-closed export and provenance

Interactive display may fall back to the OpenUA preview so the user can still
inspect an asset. The status text and renderer metadata identify that fallback.
Manual and batch exports requested as Retail Indexed do **not** save the
fallback as if it were an exact result.
Forced-row exports are allowed, but their effective renderer and destination
class are recorded as diagnostic rather than `retail_indexed_reconstructed`.

Exact export refuses, among other cases:

- absent, malformed, or structurally incompatible palette/remap tables;
- ambiguous palette candidates unless every candidate has a complete,
  byte-identical palette/SHADERMP/TRACYRMP profile;
- incomplete ATTS polygon-to-material mappings under the default
  `fail_closed` policy;
- incomplete source UV mappings;
- polygon-flag combinations that the retail AMESH dispatcher did not publish;
- mapped-TRACY materials, whose retail semantics are not yet established; and
- output dimensions above the active backend's bounded memory budget.

### Source-ATTS-only polygon submission

Retail AMESH iterates its ATTS records and asks the skeleton for each recorded
polygon ID, while AREA publishes its explicitly mapped ADE polygons. A
skeleton polygon absent from every source material mapping is not submitted to
the rasterizer. The viewport exposes `source_atts_only` as an explicit opt-in
policy for reproducing that behavior on verified source assets. Its default
remains `fail_closed`, because an incomplete editor parse or accidental
material loss must not silently erase geometry from an ordinary exact export.

The opt-in path removes unmapped polygons before camera BSP construction, so a
face that retail never submitted cannot split or reorder mapped geometry. It
does not assign NNN, copy a neighboring material, invent UVs, or export an
OpenUA-preview fallback. Renderer metadata and raster statistics record the
policy, omitted count, and exact `(owner, polygon_id)` inventory.

Batch PNGs are written to a sibling temporary file and atomically promoted. An
older output that survives a failed overwrite is explicitly classified as
retained and unverified. ZIP creation uses the manifest's authorized file list,
not a recursive sweep of the output directory. Per-image records distinguish
requested renderer, effective renderer, fallback/error reason, source hashes,
and—when an indexed image was actually written—the exact index-buffer hash.
They also distinguish the requested and effective flat-TRACY destination
policy, the fixed initial framebuffer index, and any forced diagnostic row and
palette swatch RGB. Distance-fade provenance records the requested and
effective state plus the visibility limit, start, length, distance space, and
formula used for an exact written image. OpenUA-preview rows normalize these
fade fields to inactive/null, and existing or retained files never inherit
effective claims. Destination and distance-fade settings do not alter batch
filenames. Manual Snapshot filename suggestions, separately, append `_DFADE`
when fade is active.

For enabled fade, the exporter requires the profile ID, parameters, and formula
reported by the completed indexed raster pass to match the frozen batch
request before the PNG is committed. Static UI descriptors are not accepted as
a substitute. Skip-existing likewise requires native JSON numeric fields;
numeric strings and incomplete enabled-profile records fail closed.

Here, an effective distance-fade state means that eligible render paths were
processed with the selected runtime profile. It does not assert that the final
frame differs from a matched fade-disabled render: close views can resolve to
the same final palette indices even after intermediate shade lookups changed.

When **Skip existing** is active and the output tree already contains PNGs,
the exporter therefore requires the prior `run_info.json` to prove the same
renderer, destination mode, active forced row, and—when enabled—the complete
requested distance-fade profile ID, limits, distance space, and formula. An
indexed run written before the fade field existed is treated as the backward-
compatible historical default, fade disabled; an enabled legacy record without
the full profile fails closed. Other missing, malformed, or mismatched
provenance is refused before asset scanning. Use a separate output folder or
disable **Skip existing** to overwrite intentionally.

## Current limits

- The flat-TRACY operand order, shade bypass, raw-zero handling, transparent
  LIFO pass, and whole-face culling are directly source-traced. The remaining
  raster approximation is not instruction-identical: the viewer unions
  barycentric fan/BSP fragments with a per-source-face seam guard instead of
  reproducing the retail fixed-point whole-polygon edge walker span by span.
- Retail globally sorted whole clipped opaque polygons by their maximum
  viewer-space Z, then let the first accepted opaque spans claim later overlap.
  The viewer still BSP-splits opaque geometry to obtain locally geometric
  visibility. Intersecting opaque polygons can therefore produce a different
  destination index beneath TRACY; replacing this requires a larger
  indexed-only whole-polygon span pipeline, not another blend-mode adjustment.
- The retail C `qsort` did not define an equal-depth order. The viewer uses
  source order as an explicit deterministic tie-break and records the replayed
  face order and publish depths in renderer metadata.
- Retail quantized a polygon's post-clip viewer-space maximum Z before sorting
  and clipped against the complete view frustum. The viewer uses a continuous
  source-face depth computed before BSP clipping and explicitly near-clips only;
  side bounds are handled later by raster bounds. This preserves the verified
  Hauptstation orders, but a transparent face crossing any frustum plane, lying
  wholly outside a side plane, or nearly tying another face remains a documented
  reconstruction rather than bit-exact retail scheduling.
- Retail could downgrade a perspective-mapped polygon to affine mapping when
  both projected extents were below its fixed-point 48-pixel threshold. The
  current indexed path retains the authored mapping mode; that general texture
  optimization is independent of the corrected flat-TRACY lightning path and
  is not yet emulated.
- For larger perspective polygons, retail evaluated fixed-point UV/depth at
  16-, 32-, or 64-pixel cluster endpoints and stepped linearly inside each
  cluster. The viewer currently evaluates floating reciprocal depth per pixel,
  so strong depth gradients can still select different texels.
- AREA depth fade reconstructs the source's radial per-vertex distance term and
  screen-linear fixed-brightness interpolation for eligible mapped faces. The
  supplied 1400/600 values represent the normal gameplay profile, not an
  asset-embedded constant. Other engine contexts can select different runtime
  visibility profiles and are not inferred automatically by the asset viewer.
- Retail applied its all-white (`b < 0.01`) and all-black (`b > 0.99`)
  shortcuts to one clipped source polygon. The viewer groups every BSP fragment
  carrying the same source-face identity before making that decision, so a
  split clear face cannot develop an opaque-black fragment seam. Its earlier
  fan/near-clipping pipeline still differs from retail's whole-polygon clipper,
  and floating barycentric brightness interpolation approximates the retail
  fixed-point edge walker. Threshold and edge pixels are therefore
  source-derived but are not claimed to be instruction-identical.
- A custom RGB background is a presentation composite after indexed rendering.
  It is never used as a numeric destination index for TRACY lookups. The
  forced-row control is the only UI path that substitutes a lookup operand, and
  that path is explicitly diagnostic.
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
  tests.test_indexed_viewer_integration `
  tests.test_snapshot_batch_integrity
```

The canonical test distributes no proprietary content. If local game data and
the separately extracted test assets are available, set
`OPENUA_CANONICAL_PROJECT_ROOT` and `OPENUA_GAME_SET1_ROOT`, then add
`tests.test_indexed_canonical_assets` to the command. It validates the
Hauptstation lightning, Mnosjetz flat-TRACY propellers, and Zeppelin
clear-TRACY propellers against frozen index-buffer hashes. It also locks one
Hauptstation forced-row-13 diagnostic oracle without replacing the canonical
live-framebuffer result, plus an opt-in Hauptstation distance-fade oracle that
checks the complete 1400/600 profile and interpolated vertex-brightness path.
The `VP_BRGRO` oracle separately proves that the opt-in `source_atts_only`
submission policy omits exactly unmapped `root/36` and invents no replacement
material; ordinary incomplete mappings continue to fail closed.

## Scope and licensing

The full OpenUAStudio tree is retained because Snapshot Studio shares parsers,
assembly logic, and UI components with the broader workbench. `viewer_main.py`
is the viewer-only launch surface; the original tool selector remains available
through `main.py`.

Code remains under the repository's GPL-3.0-only license and upstream notices.
Urban Assault models, textures, palettes, remap tables, names, and other game
content remain third-party property. No raw or extractable game archives,
palettes, remap tables, meshes, textures, or animations are redistributed here.
Version 4 does include bounded derived [animated reference
renders](docs/retail-indexed-turntables-v4/README.md); their catalog contains a
specific provenance and rights notice, and they are not relicensed as GPL code.
