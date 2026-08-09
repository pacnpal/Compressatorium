// Curated prose for the Help view's per-mode reference table. The registry
// (src/lib/tools/registry.js) owns the *facts* — which modes exist, how they
// group under each tool, and each mode's output extension — so HelpView.svelte
// generates the table rows from `registry.all()` and pulls only the two things
// the registry can't express from here:
//
//   MODE_BLURBS — the one-line human description per mode.
//   MODE_OUTPUT — a display override for the Output column, used where a single
//                 `outputExt` can't tell the whole story: reversible modes
//                 whose output depends on the input (the registry stores
//                 `null`), or a companion-file pair (extractcd → .cue + .bin).
//                 Every other mode shows its registry `outputExt` directly.
//
// The guard test tests/test_frontend_registry_derives_186.py fails if these
// maps drift from the registry's mode set (a new mode with no blurb, a stale
// key, or a null-output mode missing its override), so the Help table can never
// silently omit or invent a mode.

export const MODE_BLURBS = {
  // CHDMAN — Create
  createcd: 'CD images. The default for most disc consoles.',
  createdvd: 'DVD-sized media. This is the one for PSP and PS2.',
  createhd: 'Hard-disk images.',
  createraw: 'Raw data with no special disc handling.',
  createld: 'LaserDisc.',
  // CHDMAN — Extract
  extractcd: 'Pull the CD back out of a CHD. Gives you a cue/bin pair.',
  extractdvd: 'Pull a DVD image back out.',
  extractraw: 'Pull the raw image back out.',
  extracthd: 'Pull the hard-disk image back out.',
  extractld: 'Pull a LaserDisc back out.',
  // CHDMAN — Copy
  copy: 'Recompress an existing CHD with different codecs, no re-rip needed.',
  // Dolphin
  dolphin_rvz: 'Compress to RVZ. Takes a codec plus a level.',
  dolphin_wia: 'Compress to WIA. Takes a codec plus a level.',
  dolphin_gcz: 'Compress to GCZ. Fixed compression, ignores codec and level.',
  dolphin_iso: 'Decompress back to a plain ISO.',
  // 3DS
  z3ds_compress: 'No settings. Fixed Seekable Zstandard.',
  z3ds_decompress: 'Restore the original ROM from a Z3DS file.',
  // Switch
  nsz_compress: 'Compress to NSZ/XCZ. Pick a layout (Solid or Block) and a level.',
  nsz_decompress: 'Decompress back to the original NSP/XCI.',
  // CSO
  cso_compress:
    'Compress a PSP/PS2 ISO to CSO v1, the universally-supported default. '
    + 'Pick an effort preset (Fast/Default/Max).',
  cso2_compress:
    'Compress to CSO v2 (better block alignment; needs a recent PPSSPP/PCSX2). '
    + 'Same effort presets.',
  zso_compress:
    'Compress a PSP/PS2 ISO to ZSO (lz4, faster to decode). Same effort presets.',
  dax_compress:
    'Compress to DAX, the legacy PSP format some older tools expect. Same effort presets.',
  cso_decompress: 'Decompress CSO/ZSO/DAX back to a plain ISO.',
  cso_to_chd:
    'Convert a CSO/ZSO/DAX straight to CHD in one step (maxcso decompresses to a '
    + 'temp ISO, then chdman packs it). Uses chdman default compression.',
  // Handheld ROM
  romz_7z:
    'Compress a GB/GBC/GBA/DS ROM to a .7z archive (smallest). '
    + 'Pick an effort preset (Fast/Default/Max).',
  romz_zip: 'Compress to a .zip archive (broadest compatibility). Same effort presets.',
  romz_extract: 'Extract the ROM back out of a .7z/.zip archive.',
  // NKit
  nkit_restore:
    'Rebuild the full GameCube/Wii ISO from an NKit-shrunk .nkit.iso/.nkit.gcz. '
    + 'No per-job settings; the result is CRC32-checked against the NKit header. '
    + 'That '
    + 'check is skipped for a Wii image restored without its removed update '
    + 'partition — playable, but not bit-exact, and the job says so.',
  nkit_to_rvz:
    'Restore an NKit image and compress it straight to RVZ in one job (nkit2iso '
    + 'to a temp ISO, then dolphin-tool). The full-size ISO never lands in your '
    + 'library. Uses dolphin default compression.',
  // PS3
  folder_to_iso:
    'Pack a decrypted PS3 folder into an .iso RPCS3 mounts. An optional toggle '
    + 'splits the output into 4 GB parts for FAT32 drives.',
};

export const MODE_OUTPUT = {
  extractcd: '.cue + .bin',
  z3ds_compress: '.zcci / .zcia / .z3ds / .zcxi / .z3dsx',
  z3ds_decompress: '.cci / .cia / .3ds / .cxi / .3dsx',
  nsz_compress: '.nsz / .xcz',
  nsz_decompress: '.nsp / .xci',
  romz_extract: '.gb / .gbc / .gba / .nds',
};

/**
 * Build the Help view's mode-reference sections from the registry, one section
 * per (tool, mode-group) in registry order. Each row carries the raw mode name,
 * its curated blurb, and its display output (override or registry `outputExt`).
 *
 * @param {{ all: () => Array<any> }} reg the tool registry
 * @returns {Array<{ title: string, rows: Array<{ mode: string, note: string, out: string }> }>}
 */
export function helpModeSections(reg) {
  const sections = [];
  for (const tool of reg.all()) {
    const multiGroup = tool.modeGroups.length > 1;
    for (const group of tool.modeGroups) {
      const modes = tool.modes.filter((m) => m.group === group);
      if (modes.length === 0) continue;
      const title = multiGroup ? `${tool.label} · ${tool.groups[group]}` : tool.label;
      sections.push({
        title,
        rows: modes.map((m) => ({
          mode: m.mode,
          note: MODE_BLURBS[m.mode] ?? '',
          out: MODE_OUTPUT[m.mode] ?? m.outputExt ?? '',
        })),
      });
    }
  }
  return sections;
}
