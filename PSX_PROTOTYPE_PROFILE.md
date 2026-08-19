# PSX prototype visualization profile

OpenUAStudio's `PSX prototype visualization (experimental)` mode is an
opt-in presentation profile for a PC/OpenUA asset family that is already
loaded in the viewer. It exists so affine, hard-edged PlayStation-style image
behavior can be compared with the normal OpenUA preview without replacing the
source asset or the retail-indexed renderer.

The v1 profile is intentionally narrow. It applies exactly three output
policies:

- affine texture-coordinate interpolation;
- nearest-neighbor texture sampling;
- polygon antialiasing disabled.

The mode ID is `textured_psx_prototype`. A completed render reports profile
and effective-renderer ID `psx_prototype_visual_v1`, profile version `1`, and
`source_asset_pipeline=pc_openua_asset_family`.

## What the profile does not claim

The selected geometry, textures, mappings, and VANM animation remain the
loaded PC/OpenUA data. This first profile does **not** decode or silently
reinterpret PlayStation `UNIT.BIN`, PW3, PSW, PSV, `.GFX`, `mhwanh`,
`DAT`/`IND`, PV2, executable, or overlay data. It is not an emulator and it is
not cycle-accurate.

The following behaviors remain unvalidated and are not applied by v1:

- a prototype camera, field of view, near/far plane, fog, or draw-distance
  profile;
- forced NTSC/PAL native resolution or aspect behavior;
- PlayStation GTE vertex snapping or fixed-point edge walking;
- BGR555 framebuffer quantization, texture CLUT/STP/color-zero rules, or ABR
  semitransparency;
- PlayStation dithering;
- FT4, GT4, and F2 primitive queues or ordering-table behavior;
- prototype-specific mesh, texture, material, animation, or unit-name data.

Those non-claims are recorded directly in `renderer_info` and complete-model
batch manifests. Manual snapshots carry the distinct v1 filename suggestion
described below. These are part of the profile contract rather than informal
caveats.

## Viewer and export behavior

The toolbar and Photo Studio renderer selectors remain synchronized. Selecting
the PSX profile disables Retail Indexed-only TRACY and AREA distance-fade
controls without changing their saved values. Returning to Retail Indexed
restores those configured values.

Manual snapshot suggestions use the `_PSX_PROTO_VISUAL_V1` suffix so an
experimental PSX-profile image is not confused with an OpenUA or Retail
Indexed snapshot. A requested PSX snapshot fails closed unless the completed
renderer identity is exactly `psx_prototype_visual_v1`.

Complete-model batch export records the requested and effective profile,
profile version, source-asset pipeline, and every applied/non-applied PSX
policy. `Skip existing` accepts a prior PSX batch only when its profile ID,
version, and output-affecting policy record match. A later improved profile
therefore cannot silently reuse v1 output.

## Evidence-backed next phase

The prototype audit established enough structure for a later, separate,
read-only PSX asset browser without guessing at game behavior:

- compact PW3 uses an 80-byte little-endian header, signed 32-bit vertices
  consistent with 16.16 coordinates, and 26-byte face records;
- each compact face preserves four vertex indices, four UV byte pairs, a raw
  material/texture selector, and four corner-shade bytes;
- `UNIT.BIN` stores structurally validated PW3 bodies at 2,048-byte boundaries;
- `mhwanh` images have a complete indexed RGB-palette representation;
- sampled `.GFX` images use a 16-word BGR555/STP CLUT and low-nibble-first
  four-bit indices, although dimensions and runtime alpha behavior are
  external;
- `.IND` files provide bounded offsets into heterogeneous `.DAT` records.

That future importer should begin with ordinal, two-sided PW3 topology and raw
field inspection. Capture-validated texture-page binding, face winding,
primitive choice, animation, lighting, and blending must precede any claim of
faithful textured PSX unit rendering.

## Validation boundary

Tests require the PSX mode to remain affine through both the normal transformed
texture path and the degenerate-UV software fallback, keep antialiasing and
smooth sampling disabled, produce deterministic snapshots, report the exact
profile scope, and abort mislabeled snapshot fallbacks. Existing OpenUA
projective rendering and Retail Indexed canonical outputs remain separate
regression gates.
