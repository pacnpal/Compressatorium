// File-type → Lucide icon mapping. Re-used by FileRow, JobRow, info modals,
// and anywhere else a single file/entry needs a visual affordance.
//
// Returns a Svelte component reference. Use as:
//   <script>
//     import { iconForEntry } from '$lib/util/fileIcon.js';
//     const Icon = $derived(iconForEntry(entry));
//   </script>
//   <Icon size={16} />

import Folder from '@lucide/svelte/icons/folder';
import Archive from '@lucide/svelte/icons/archive';
import Disc3 from '@lucide/svelte/icons/disc-3';
import Disc from '@lucide/svelte/icons/disc';
import Gamepad2 from '@lucide/svelte/icons/gamepad-2';
import File from '@lucide/svelte/icons/file';

import { registry } from '$lib/tools/registry.js';

// The registry (src/lib/tools/registry.js) owns *which* extensions each tool
// handles. It has no notion of how a file should *look*, so this is the one
// presentation fact the icons add on top: whether a tool's media reads as an
// optical disc or a cartridge/handheld "game". Adding a tool means adding one
// entry here — the coverage guard (tests/test_frontend_registry_derives_186.py)
// fails until every registered tool is classified — not re-typing its whole
// extension list, which is derived from the registry below.
export const TOOL_MEDIA = {
  chdman: 'disc', // CD / DVD / LaserDisc images
  cso: 'disc', // compressed PSP / PS2 ISOs
  makeps3iso: 'disc', // PS3 disc images (folder → .iso; no file exts today)
  nkit: 'disc', // NKit-shrunk GameCube / Wii disc images
  dolphin: 'game', // GameCube / Wii disc images, shown as game media
  z3ds: 'game', // 3DS ROMs
  nsz: 'game', // Switch dumps
  romz: 'game', // handheld ROMs (.7z/.zip outputs handled as archives below)
};

// Loose archive containers get the archive glyph even when they arrive as a
// plain file entry (a browsed archive already comes through as type 'archive',
// handled before the extension check). These are the handheld-ROM packer's
// outputs, and they win over the game bucket their tool would otherwise assign.
export const ARCHIVE_EXTS = new Set(['.7z', '.zip']);

// Build the disc / game extension sets from the registry so a new tool's inputs
// and outputs are classified automatically from one `TOOL_MEDIA` entry. `.chd`
// keeps its own glyph (Disc3) in `iconForEntry` below.
const { DISC_EXTS, GAME_EXTS } = (() => {
  const disc = new Set();
  const game = new Set();
  const buckets = { disc, game };
  for (const tool of registry.all()) {
    const target = buckets[TOOL_MEDIA[tool.id]];
    if (!target) continue;
    for (const ext of [...tool.sourceExts, ...tool.verifyExts]) target.add(ext);
  }
  // A disc classification wins over game for shared extensions (e.g. `.iso`, a
  // CHDMAN CD/DVD source that Dolphin also accepts) so the common case reads as
  // a disc — matching how `.iso` verify routing already prefers CHDMAN.
  for (const ext of disc) game.delete(ext);
  return { DISC_EXTS: disc, GAME_EXTS: game };
})();

// Exported for the registry-coverage guard test: every extension the registry
// can surface must resolve to one of these buckets (or `.chd`), never the
// generic File glyph.
export const ICON_EXT_CATEGORIES = {
  disc: [...DISC_EXTS],
  game: [...GAME_EXTS],
  archive: [...ARCHIVE_EXTS],
};

/**
 * @param {{ type?: string, extension?: string } | null | undefined} entry
 */
export function iconForEntry(entry) {
  if (!entry) return File;
  if (entry.type === 'directory') return Folder;
  if (entry.type === 'archive') return Archive;
  const ext = entry.extension?.toLowerCase() ?? '';
  if (ext === '.chd') return Disc3;
  if (ARCHIVE_EXTS.has(ext)) return Archive;
  if (DISC_EXTS.has(ext)) return Disc;
  if (GAME_EXTS.has(ext)) return Gamepad2;
  return File;
}
