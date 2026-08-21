# Historical v5 PSX presentation profile

This document is retained so v5.0.0 links and historical manifests remain
understandable. The published v5 profile `psx_prototype_visual_v1` did **not**
decode PlayStation assets: it applied affine texture mapping, nearest-neighbor
sampling, and hard polygon edges to a loaded PC/OpenUA `AssetFamily`.

That interpretation did not match the intended feature. Beginning with v6.0.0,
it is no longer a selectable renderer and is never aliased to native PSX
output. Its identifiers remain reserved only for legacy manifest and
output-collision recognition:

- requested mode: `textured_psx_prototype`;
- effective/profile ID: `psx_prototype_visual_v1`;
- source pipeline: `pc_openua_asset_family`.

The corrected implementation is the separate
[Native PlayStation prototype asset viewer](PSX_NATIVE_ASSET_VIEWER.md). It
loads native PSW/PSV/PW3 and `UNIT.BIN` geometry from an explicitly selected
prototype source. Validated native compact and sector-padded `SETnGFX.BIN`
texture tables are available for explicit operator selection when present;
topology-only is the default, and no mesh/environment-to-SET affinity is
inferred. PC/OpenUA assets
are never substituted. Native parser v3 keeps executable-proven runtime model
slots separate from dense archive ordinals, counts empty allocation slots only
in the runtime namespace, and leaves friendly names unmapped.

Its PW3 path preserves all four raw file slots and uses the June
executable-backed packet order, reverse-fan decomposition, and
bit-14/strict-positive NCLIP decision. PSW/PSV now uses its independently
recovered packet order `1,0,2,3`, unconditional strict-positive NCLIP, and,
when a native texture pack is explicitly selected, bounded affine
direct-grayscale modulation. Its integral signed 16.16 UV component `signed`
uses the executable-exact material-local quotient
`q = ((signed >> 16) + (1 if signed < 0 else 0)) >> 1`, sampled directly in
the selected 128 x 128 payload as a pre-origin `0..127` texel coordinate.
Descriptor origin and absolute VRAM/wrap equivalence are not inferred. PW3
direct/tinted shade formulas are recovered, but their effective dispatch
remains hard-gated. Runtime descriptor TPage/CLUT-offset provenance and
STP/ABR application remain unresolved for both PSW/PSV and PW3. Both NCLIP
paths use the viewer's floating projection rather than exact GTE edge-on
rounding, and exact GPU/GTE helpers remain dormant where the game binding is
unresolved.

PV2 files are recorded only as strict static, unbound effect inventory, not as
animation. Only the exact June executable/overlay/asset evidence triplet
identifies the recovered loose V56B body as a conditional near-view model
override; it is not animation and is not generalized to other builds. Native
manual snapshots use
`psx_native_manual_snapshot_v2`; mesh batches use
`psx_native_mesh_batch_v2`; snapshot and batch-manifest schemas are v3. These
PNG/JSON provenance contracts do not reuse the historical v5 PC-profile
manifest.

The v5 comparison under `docs/psx-prototype-profile-v5/` remains an immutable
historical release artifact. It must not be described as a PSX asset capture or
used as evidence for the corrected native renderer.

No native PlayStation executable, archive, mesh, texture pack, or other source
bytes are committed as part of the corrected implementation.
