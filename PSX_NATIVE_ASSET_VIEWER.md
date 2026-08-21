# Native PlayStation prototype asset viewer

The experimental PlayStation mode is a separate, read-only source pipeline.
It decodes assets from an explicitly selected, extracted Urban Assault
PlayStation prototype disc tree. It never restyles a loaded PC model and never
borrows PC/OpenUA `BASE`, `SKLT`, `ILBM`, `VANM`, `SET.BAS`, palette,
`SHADERMP`, or `TRACYRMP` data.

The selectable mode is `psx_native`. A completed native render reports
`profile_id=psx_native_asset_v1` and
`source_asset_pipeline=psx_native_disc_assets`. The former v5 identifiers
`textured_psx_prototype` and `psx_prototype_visual_v1` remain reserved for
reading historical manifests; they are not aliases for this corrected mode.

## Opening a prototype source

Use **File > Import > Open PlayStation Prototype Assets...** or the
**PSX Archive** tab in Snapshot Studio. Choose one extracted disc tree that
contains all of the following in the same filesystem:

- `SYSTEM.CNF`;
- the valid `PS-X EXE` boot target named by `SYSTEM.CNF`;
- `UNITMODL/UNIT.BIN` or one or more loose `.PSW`, `.PSV`, or `.PW3` meshes.

Discovery is bounded to the location explicitly selected by the user. It does
not scan arbitrary drives, follow directory symlinks, download builds, combine
files from different prototypes, or modify the source tree.

Before an extracted build can enter the viewport or native exporter, a shared
frozen-object contract validates the complete public inventory. It requires
safe relative logical paths and exact lowercase hashes; canonical packed and
loose mesh ordering; coherent archive ordinals, offsets, sectors, and
`UNIT.BIN` identity; format-specific raw face records and decoded fields;
bounded PSW/PSV material-local UVs; static/unbound PV2 identity; exact compact
or sector-padded texture-pack structure; and a complete bijection between
executable table slots, empty entries, and packed meshes when model-slot
evidence is present. The parsers remain independent of this validation layer;
the interactive renderer and exporter apply it at the boundaries that can make
or serialize provenance claims.

Packed `UNIT.BIN` meshes retain their dense archive ordinal, sector, offset,
body hash, vertex count, and face count. Parser v3 also exposes a separate
runtime model slot when the exact boot executable contains one unique,
byte-matching `(start_sector, body_size)` allocation table. Runtime slots count
empty allocation placeholders; dense archive ordinals do not. A final archive
marker is classified as a non-slot sentinel only when that executable table
proves the distinction. If the executable evidence is absent or ambiguous,
model slots remain unavailable rather than being inferred from archive order.

`VEHICLE.TXT` is displayed only as an unmapped roster and is never used to
guess friendly unit names. A loose `V<number><variant>` filename supplies only
its authored asset number and variant; counterexamples prove that number is not
generally interchangeable with a packed runtime model slot. Loose native model
filenames are otherwise retained as authored.

## Native mesh decoder

The strict parser v3 currently supports the independently cross-validated PSW
version 1 and PW3 version 3 layouts:

- 80-byte little-endian mesh header;
- signed 16.16 vertex coordinates, three 32-bit values per vertex;
- 76-byte PSW v1 or 26-byte PW3 v3 face records;
- four authored vertex indices, UV pairs, one raw texture selector, corner
  shades, and a face prefix;
- immutable four-slot index, UV, and shade storage for PW3, including repeated
  corners; the older unique-corner fields remain only as a compatibility and
  diagnostic view;
- strict stream bounds, count, offset, index, sector-alignment, and `0xBA`
  archive-padding validation.

The June executable establishes the PW3 v3 primitive behavior used here:

- GPU packet corners are submitted in raw-file order `1,0,2,3`;
- the equivalent viewer decomposition in raw file slots is the reverse fan
  `(0,2,1)` plus `(0,3,2)`;
- the little-endian 16-bit value at face-prefix offset zero supplies flag
  `0x4000`;
- when that bit is clear, NCLIP is evaluated on raw slots `1,0,2` before
  clipping or quad decomposition, and the primitive is accepted only for a
  strictly positive result;
- when that bit is set, the NCLIP rejection is bypassed and the primitive is
  treated as two-sided.

The December and March executable paths establish the corresponding PSW/PSV
submission contract:

- authored corners are submitted to the `POLY_GT4` packet in order `1,0,2,3`;
- NCLIP consumes authored slots `1,0,2` before clipping or quad decomposition;
- NCLIP is unconditional and only a strictly positive MAC0 survives; neither
  preserved PSW/PSV prefix word provides a two-sided bypass; and
- with an explicitly selected native texture pack, the direct packet shade is
  written as grayscale at each packet corner, interpolated affinely in screen
  space, and applied with the evidenced textured-channel equation
  `(texel5 * shade8) >> 4`. Sampling is nearest-neighbor, a resolved CLUT word
  `0x0000` discards the fragment, and the bounded raster helper rejects unsafe
  or unbounded patch requests.

The bounded PSW/PSV texture path applies the executable-exact local part of the
recovered signed, integral 16.16 UV narrowing. For one signed 32-bit fixed
component `signed`, the descriptor-relative local coordinate is
`q = ((signed >> 16) + (1 if signed < 0 else 0)) >> 1`. The validated source
domain produces `q` in `0..127`; the selected 128 x 128 material is sampled
directly at that pre-origin texel coordinate rather than treating `q` as a
normalized UV byte. The runtime descriptor origin is deliberately omitted.
This material-local preview therefore makes no claim of equivalent absolute
VRAM placement, byte-wrap behavior, or interpolation across a wrapped packet
coordinate.

Both PW3 and PSW/PSV NCLIP decisions currently use the viewer's floating-point
projection, so they do not claim exact GTE fixed-point rounding at the edge-on
boundary. The parser continues to preserve both PSW/PSV prefix words without
assigning either one an unsupported culling, blending, or material meaning.

The June overlay also proves two PW3 packet-color routines. The direct routine
maps disk shade bytes to packet corners `1,0,2,3` as grayscale RGB. The tinted
routine evaluates each channel exactly as `(shade * tint_channel) >> 8`, with
integer floor semantics. What normal UNIT rendering dispatches to either
routine, and the effective caller tint when the tinted routine is selected,
remain unresolved hard gates. PW3 corner shading is therefore preserved and
reported but is not applied by the viewer.

The same overlay proves that a resolved runtime texture descriptor supplies
the PW3 packet TPage and that nonzero TPage ABR bits `5..6` select command
`0x3E` instead of opaque `0x3C`. It does not prove how an on-disc PW3 selector
obtains that descriptor TPage. The older PSW/PSV path independently proves its
local pre-origin UV quotient and descriptor-facing packet fields, but the
selected SET payload is not claimed to be an absolute runtime descriptor or
VRAM allocation. Runtime descriptor origins, TPage and CLUT-offset provenance,
and STP/ABR application therefore remain unresolved and dormant for both mesh
formats. Exact zero-word, STP-conditioned semitransparency, and all four
saturated ABR equations are available as pure GPU-color helpers. The exact
GTE/GPU helpers for integer division and SXY snapping, NCLIP, AVSZ, dithering,
and ordering-table insertion are likewise not wired to the viewer while the
game-to-viewer transform, dithering enable state, and full ordering-table
traversal remain unresolved.

Malformed or unsupported native data fails closed. No partial mesh and no PC
fallback is displayed under a native PSX renderer identity.

## Validated native texture packs

The 15/16 June 1999 compact `GFX/SET1GFX.BIN` through `SET6GFX.BIN` files are
decoded directly from the selected prototype source. Each exact `0x41400`-byte
pack contains 128 selector-indexed CLUT slots and 32 four-bit pixel banks:

- selectors `0..31` each contain an 8-byte header, 16-word little-endian PSX
  BGR555/STP CLUT, and `0x2000` bytes of packed pixels;
- selectors `32..127` each contain a header and explicit CLUT;
- PW3 selector `S` resolves CLUT slot `S` and pixel bank `S & 31`;
- each bank is 128 x 128, low nibble first;
- PW3 authored UV bytes retain their bounded viewer `u/256 * width` preview;
  PSW/PSV instead uses the recovered signed 16.16 quotient directly as a
  `0..127` material-local, pre-origin texel coordinate;
- a resolved CLUT word `0x0000` is transparent; palette index zero is not
  universally transparent;
- BGR555 channels and the STP bit are retained. A nonzero STP-set word is still
  shown opaque: descriptor TPage provenance and STP/ABR application are not
  inferred. The applied PSW/PSV grayscale path obeys the same zero-word rule.

Late builds open in **Topology only**. The user may then explicitly choose any
of the six native environmental texture sets exposed by that same prototype
source. The selector-to-CLUT/pixel-bank layout inside each pack is validated,
but no mesh-to-environment or mesh-to-SET association has been recovered. A
chosen pack is therefore recorded as an operator-selected variant, never as an
inferred authoritative texture set for that mesh. Earlier sector-padded
`SETnGFX.BIN` layouts are decoded through a separate strict grammar rather
than coerced into the late format:

- every build still has exactly 128 selector slots;
- a populated direct slot contains an 8-byte zero header, its own 16-word
  BGR555/STP CLUT, `0x2000` packed 4bpp bytes, and exact `0xBA` allocation
  padding to `0x2800` bytes;
- an unavailable selector is one `0x800`-byte sector beginning with
  `4e0d0a1a` and otherwise filled with `0xBA`;
- selector `S` uses CLUT `S` and the pixel payload in direct slot `S`, not
  the late `S & 31` bank;
- only the three recovered complete sizes are accepted: December's
  `0xDA000` (77 populated slots), March's `0xDC000` (78), and May's
  `0xE2000` (81).

Malformed recognized layouts fail closed. Unknown texture-pack sizes remain
unavailable and are never substituted. All builds still open topology-only;
an older or late native pack becomes active only through the same explicit
operator selection.

## Source isolation and lifecycle

Loading a PSX mesh clears the active PC `AssetFamily`, PC material images,
indexed renderer adapter, and VANM playback state before installing the native
scene. Switching back to an OpenUA or Retail Indexed renderer requires a real
PC/OpenUA family and restores that independent source session. Retail TRACY and
AREA distance-fade preferences are retained but disabled while a native PSX
scene is active.

Snapshot Studio treats each successful PC or native scene replacement as one
source-activation transaction. If a replacement occurs while Snapshot is
open, the newly fitted camera becomes both its saved **Current View** and the
regular camera restored on exit; the active source renderer becomes the exit
mode. An independently selected named Snapshot preset is then reapplied for the
preview without overwriting that fitted **Current View** baseline. The window
title, Object Info source lines, renderer selectors, toolbar preset, **Reset
camera** availability, passive-resize state, and native visibility probe are
synchronized to the same committed scene. Restoring a PC or empty viewport
clears native-only visibility state rather than leaving a stale hint or Reset
action.

Renderer-only changes are not source replacements. When the exact native
build, mesh, and texture pack are already installed, returning from wireframe
or materials mode changes only the renderer and keeps the operator's camera.
Snapshot may temporarily force the truthful native renderer for preview/export,
but exit restores that same-source diagnostic camera and mode. A texture-pack
change similarly preserves the camera while validating that the pack still
belongs to the active build.

An initial native mesh load and an explicit **Reset camera** fit the canonical
view to the complete decoded model and current viewport. A native texture-pack
change reloads the same mesh while preserving the operator's subsequent orbit,
zoom, pan, center, and scale. Named camera presets continue to fit their chosen
view to the applicable viewport or snapshot output size.

If the fitted or current native view produces no model pixels, the status bar
non-blockingly suggests **Back**, **Bottom**, or **Reset camera** to fit. This
advisory uses a transparent clean-alpha pass that excludes the background,
grid, axes, and diagnostics, preserves renderer provenance, and retains normal
PW3 bit-14/NCLIP culling. It does not substitute a guessed two-sided render.

Native PlayStation unit animation is not decoded, so **Enable animations** is
disabled. PC VANM data is never applied to a PSX mesh. Strict 352-byte PV2
version-1 files are inventoried as immutable, unbound static effect meshes: the
recovered files contain geometry, UV, selector, and shade fields, but no frame
timing, playback, attachment, or model-slot record. PV2 inventory is therefore
not presented as animation.

The exact June executable, overlay, and `V56B.PW3` evidence triplet proves that
the loose V56B body is a conditional near-view override for runtime model slot
56. It is not an animation frame. The evidence helper does not generalize that
binding to other revisions, and the viewer does not invent a runtime override
when the exact dispatch conditions are unavailable.

The existing complete-VP batch exporter is PC/`SET.BAS`-specific and is
disabled in native mode. Native output instead uses the dedicated mesh exporter
in the **PSX Archive** tab.

The suggested filename for a manual native snapshot includes
`_PSX_NATIVE_V1`. Snapshot completion is accepted only when the native scene
and selected texture-pack identity are unchanged through the render and the
effective profile is exactly `psx_native_asset_v1`. Native manual export is
PNG-only and transactionally commits the PNG with a neighboring `.png.json`
sidecar under capture profile `psx_native_manual_snapshot_v2`. If either target
already exists, overwrite approval covers the pair; a failed pair commit is
rolled back rather than reporting a partial snapshot as complete.

Opening a prototype may leave the file chooser's remembered directory inside
the extracted source. In that case the manual snapshot suggestion is moved to
the source tree's parent. Regardless of the suggestion, a manually chosen
output path that resolves inside the extracted tree is refused before any
render or write. If either the source root or selected output cannot be
resolved safely, export also fails closed before rendering, writing, or
creating the candidate directory.

Manual and batch sidecars use `openuastudio.psx_native_snapshot` schema v3.
Alongside source hashes and renderer identity, schema v3 retains and validates
the parser-v3 per-mesh primitive-cull and raw-corner-shade censuses, runtime
model slot plus executable evidence when available, the still-distinct dense
archive ordinal, the friendly-name binding status, and the static PV2
inventory. An older or otherwise incomplete sidecar cannot be treated as
parser-v3 resume proof.

## Native mesh batch export

The **Batch Native PSX Meshes** panel enumerates every mesh in the active
native build and the selected subset of ten fixed camera views: front, back,
left, right, top, bottom, and four isometric corners. It never treats a PSX
runtime model slot or dense archive ordinal as a PC VP entry. The batch capture
profile is `psx_native_mesh_batch_v2`.

The batch freezes its output-affecting choices before rendering:

- square image size and zoom;
- selected fixed views;
- **Topology only**, the safe default, or **Current explicitly selected pack**,
  which freezes the exact native SET pack already chosen by the operator;
- transparent PNG output, guides off, and animation disabled at the initial
  frame.

Each timer step can commit at most one transactional PNG plus `.png.json`
sidecar. The sidecar is the commit proof: it contains sanitized logical paths,
source and stream hashes, parser and renderer profiles, executable-proven
runtime model slot when available, separate dense archive ordinal or native
filename, texture-selection status, deterministic camera state, output
settings, and the PNG hash. Friendly names remain explicitly unmapped. **Skip
verified existing** accepts a prior pair only
when both files exist, the complete frozen identity matches, and the current
PNG bytes match the recorded hash. That identity check includes the parser-v3
primitive-cull and immutable raw four-slot corner-shade censuses; partial,
mismatched, older-schema, or unsafe targets fail closed.

The root `psx_native_batch_manifest.json` is written atomically for both a
complete run and a safe cancellation. Its
`openuastudio.psx_native_batch_manifest` schema v3 retains both censuses,
runtime-slot evidence, distinct ordinal identity, and the source's static PV2
inventory together with mesh and stream hashes. Cancellation is
observed between image transactions, so already committed or independently
verified pairs remain listed while unfinished work is reported as remaining.
Batch output must be outside the extracted prototype source tree, which is
never modified. The core mesh exporter repeats this check independently of
the UI before it plans jobs or creates directories. It resolves both roots and
rejects the source itself, descendants, lexical `..` aliases, and existing
filesystem aliases that resolve into the source; an inability to resolve the
boundary is itself a fail-closed error. Manual and batch exports therefore
cannot add screenshots, sidecars, manifests, rollback files, or partial staging
files to the selected source tree.

Source reads reject symlink or junction ancestry before opening the selected
archive or executable. Output commits re-resolve the source/output boundary
before manifest or image-pair installation and again after staging. If an
otherwise absent output path is replaced with a filesystem alias during the
transaction, the commit is cancelled or rejected rather than following the
new target.

On Windows, the atomic writer also preflights every final native PNG, JSON
sidecar, and batch-manifest path against a conservative 248-character safety
budget before file output begins. Its temporary basenames are bounded and
derived from a short target-name hash, so staging does not repeat and lengthen
an already long final filename. Temporary-file allocation failures are wrapped
as native atomic-write errors. If allocation or a later transaction step
fails, reserved stages are cleaned; in particular, failure to allocate the
second member of a PNG/JSON pair removes the first reserved stage rather than
leaving zero-byte debris. Overwrite transactions also track an empty rollback
reservation before its first fallible unlink/move. A pre-move failure cleans
that reservation and leaves the previous PNG/JSON pair untouched. If an old
final was moved successfully but cannot be restored after a later failure, its
nonempty rollback copy is retained for manual recovery and the error reports
the incomplete restoration.

## Provenance

Renderer metadata is derived from the bytes actually loaded and includes:

- boot executable, `SYSTEM.CNF`, `UNIT.BIN`, and optional `VEHICLE.TXT`
  logical paths and SHA-256 values;
- native mesh format/version, dense archive ordinal/offset/sector, body and
  stream hashes, and vertex/face counts;
- the executable-proven runtime model slot and its allocation-table evidence
  when available, including empty slots and any proven trailing sentinel;
- an explicit friendly-name-unmapped status rather than a guessed
  `VEHICLE.TXT` binding;
- the strict static, unbound PV2 inventory and its source/stream hashes;
- texture-selector census;
- optional, explicitly selected native texture-pack logical path and SHA-256
  when a pack is chosen;
- whether output is topology-only or uses an explicitly operator-selected
  native pack whose mesh/environment affinity remains unproven;
- exact selector/CLUT/pixel-bank layout, the format-specific bounded UV
  preview profile, and the zero-word transparency rule;
- PW3 raw four-slot storage, GPU packet order, raw reverse-fan triangles,
  `native_primitive_cull_census`, `native_raw_corner_shade_census`,
  strict-positive NCLIP policy, and floating-point numeric domain;
- PSW/PSV packet order, unconditional strict-positive NCLIP, floating-point
  NCLIP numeric domain, executable-exact material-local pre-origin UV quotient,
  and the applied bounded affine direct-grayscale modulation profile;
- recovered-but-dormant PW3 direct/tinted shade formulas, descriptor TPage and
  STP/ABR binding status, GPU/GTE helper status, and explicit negative claims
  for PC source use, unit animation, primitive queues, vertex snapping,
  dithering, and cycle accuracy.

Portable renderer metadata contains logical paths and hashes, never an
absolute local prototype path.

## Remaining reconstruction limits

This is native asset decoding, not complete PlayStation emulation. The viewer
still does not claim:

- friendly unit-name bindings for runtime slots, dense archive ordinals, or
  loose native filenames;
- PSX unit animation;
- PV2 playback, attachment, or model-slot bindings; its strict static inventory
  is not animation;
- automatic application of V56B; its conditional near-view override status is
  proven only by the exact June executable/overlay/asset evidence triplet;
- the effective PW3 direct-versus-tinted shade dispatch or the tinted caller
  color, even though both local formulas are recovered;
- absolute runtime descriptor origins, TPage and CLUT offsets, VRAM placement
  and byte-wrap equivalence, and therefore STP/ABR semitransparency application
  for either PSW/PSV or PW3;
- the remaining meaning of face-prefix bits other than the executable-proven
  PW3 `0x4000` culling flag;
- exact GTE fixed-point rounding for either PW3 or PSW/PSV NCLIP, especially at
  the edge-on boundary;
- GTE vertex snapping, native PAL/NTSC viewport behavior, dithering, ordering
  tables, fixed-point edge walking, or cycle-accurate rasterization;
- a proven model/environment-to-SET association for any decoded texture pack.

Those gaps remain visible in metadata and UI rather than being filled with PC
assets or guessed behavior.

## Validation

Public tests build synthetic PSW, PW3, `UNIT.BIN`, PV2, compact, and
sector-padded `SETnGFX` fixtures and prove strict parser-v3 behavior,
executable-gated runtime-slot identity, source isolation, native snapshot
identity, hard-edged affine sampling, PW3 and PSW/PSV primitive decisions,
bounded direct-grayscale PSW/PSV modulation, dormant-helper evidence gates,
manual and mesh-batch PNG/JSON transactions, cancellation/resume behavior,
fail-closed output, fit-on-load/reset behavior, operator-camera preservation,
and zero-pixel presentation hints without a culling bypass.
The corpus-enabled suite also parses every recursively discovered December
PSW/PSV file, including `TEST/TANK.PSV` and `TEST_ART/DEFAULT.PSV` outside the
normal `UNITMODL` viewer inventory: 33 files, 1,346 faces, 5,384 authored UV
pairs, and 10,768 signed fixed components, all producing recovered bounded
quotients in `0..127`.
Optional canonical tests run when `OPENUA_PSX_CORPUS_ROOT` points to the local
prototype audit corpus; they verify representative December, March, May, and
June mesh/texture structure and exact source hashes. The June presentation gate
also covers the two ring sentinels at ordinals 15/16 and the one-quad sentinels
at ordinals 112/113/128/134: Back exposes the former and Bottom exposes the
latter while their source-correct default face remains culled. Prototype
executables, archives, meshes, texture packs, and all other native source bytes
remain local and are not committed to the repository.

The v6.0.0 source tree completes 749 full-repository tests with `OK` and
one optional skip. The corpus-enabled PSX discovery passes 210/210, the central
native-contract suite passes 30/30, and the focused contract/viewer/export set
passes 83/83; bytecode compilation and `git diff --check` also pass.

The frozen local `native_viewer_qa_v3` gate renders all 1,789 build-local
mesh-by-available-pack combinations and resolves all 3,901 used selectors with
zero failures, PC-source uses, fallbacks, or frame-edge contacts. All 60 blank
Current View combinations succeed from a source-correct Back or Bottom
follow-up. Manual review of 12 representatives across all four recovered builds
finds coherent whole-face cull differences and no unexplained diagonal
half-quad loss, winding tear, or inside-out surface. The audit package binds its
portable manifest, 36 representative renders, five contact sheets, review, and
runner with SHA-256 checksums while excluding all recovered raw source assets.
Historical release counts remain labeled in `CHANGELOG.md`; they are not used
as evidence for this parser-v3/schema-v3 contract.
