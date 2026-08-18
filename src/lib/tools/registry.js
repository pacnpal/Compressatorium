// Frontend tool registry, mirrors docs/DESIGN_tool_plugin_architecture.md §3.7
// field-for-field, extended with the ModeSpec metadata defined in
// app/services/tools/spec.py.
//
// The registry is the single source of truth for tool identity. All
// call sites (UI store, sidebar, SSE URL building, group labels, mode
// metadata) look up via the helpers below, there is no hardcoded
// `if (tool === 'chdman')` branching, and no map at the top of this
// file that needs editing when adding a 4th tool. To add a tool:
// declare one entry in TOOLS, register its plugin on the backend,
// add the binary to the Dockerfile. That's it.

import { api } from '$lib/api/endpoints.js';

const CHDMAN_SOURCE_EXTS = ['.gdi', '.iso', '.cue', '.bin'];
const CHDMAN_VERIFY_EXTS = ['.chd'];

const DOLPHIN_SOURCE_EXTS = ['.iso', '.gcz', '.wia', '.rvz', '.wbfs'];
// `.iso` is intentionally absent from DOLPHIN_VERIFY_EXTS even
// though dolphin-tool itself can verify GC/Wii ISOs. Most ISOs in
// any given directory are CHDMAN CD/DVD sources, and
// `toolForVerifyPath()` returns the first tool whose verify list
// matches, adding `.iso` here would route every CD ISO row's
// Info/Verify action into /api/dolphin-verify (which fails for
// non-Dolphin ISOs and confuses users). Users who want to verify a
// GC/Wii ISO have two paths today: compress to `.rvz` first, then
// verify the `.rvz`; or use RowActionsMenu's verify-from-output
// flow, which targets any Dolphin output already declared in
// `entry.outputs` (added in round 16). A future per-tool picker
// in RowActionsMenu would let the user disambiguate directly.
const DOLPHIN_VERIFY_EXTS = ['.gcz', '.wia', '.rvz', '.wbfs'];

// 3DS (z3ds). Compress takes a raw ROM; decompress takes a compressed Z3DS
// container. Both directions are "sources" depending on the chosen mode.
const Z3DS_COMPRESS_EXTS = ['.cci', '.cia', '.3ds', '.cxi', '.3dsx'];
const Z3DS_VERIFY_EXTS = ['.zcci', '.zcia', '.z3ds', '.zcxi', '.z3dsx'];
const Z3DS_SOURCE_EXTS = [...Z3DS_COMPRESS_EXTS, ...Z3DS_VERIFY_EXTS];
const Z3DS_OUT_MAP = {
  '.cci': '.zcci', '.cia': '.zcia', '.3ds': '.z3ds',
  '.cxi': '.zcxi', '.3dsx': '.z3dsx',
  '.zcci': '.cci', '.zcia': '.cia', '.z3ds': '.3ds',
  '.zcxi': '.cxi', '.z3dsx': '.3dsx',
};

// Switch (nsz). Both directions are "sources" depending on the chosen mode.
const NSZ_COMPRESS_EXTS = ['.nsp', '.xci'];
const NSZ_VERIFY_EXTS = ['.nsz', '.xcz'];
const NSZ_SOURCE_EXTS = [...NSZ_COMPRESS_EXTS, ...NSZ_VERIFY_EXTS];
const NSZ_OUT_MAP = {
  '.nsp': '.nsz', '.xci': '.xcz', '.nsz': '.nsp', '.xcz': '.xci',
};

// CSO (maxcso). Compress takes a raw .iso; decompress takes any maxcso
// container (.cso/.zso/.dax). Both directions are "sources" by mode.
const CSO_COMPRESS_EXTS = ['.iso'];
const CSO_VERIFY_EXTS = ['.cso', '.zso', '.dax'];
const CSO_SOURCE_EXTS = [...CSO_COMPRESS_EXTS, ...CSO_VERIFY_EXTS];

// Handheld ROM packer (7z). Compress takes a loose GB/GBC/GBA/NDS ROM;
// extract takes one of the .7z/.zip archives it writes. Both directions are
// "sources" by mode. Output names preserve the ROM extension (Game.gba.7z).
const ROMZ_COMPRESS_EXTS = ['.gb', '.gbc', '.gba', '.nds'];
const ROMZ_VERIFY_EXTS = ['.7z', '.zip'];
const ROMZ_SOURCE_EXTS = [...ROMZ_COMPRESS_EXTS, ...ROMZ_VERIFY_EXTS];

// Wii U (JWUDTool). Compress takes the raw .wud dump; decompress takes the
// .wux container. Both directions are "sources" depending on the chosen mode.
const JWUD_COMPRESS_EXTS = ['.wud'];
const JWUD_VERIFY_EXTS = ['.wux'];
const JWUD_SOURCE_EXTS = [...JWUD_COMPRESS_EXTS, ...JWUD_VERIFY_EXTS];

// makeps3iso (PS3 folder -> ISO). The unit of work is a DIRECTORY, not a file
// with a suffix, so it declares no source/verify extensions; the mode carries
// `inputKinds: ['directory']` and selection runs through the backend's
// per-folder `convertible_by` annotation instead of an extension match.
const PS3_SOURCE_EXTS = [];
const PS3_VERIFY_EXTS = [];

// nkit2iso (NKit-shrunk GC/Wii image -> full .iso). COMPOUND extensions: the
// meaningful part is `.nkit`, but the trailing `.iso`/`.gcz` is what CHDMAN and
// Dolphin already own. `endsWithAny` (below) is a suffix match, so declaring the
// full `.nkit.iso` keeps the two apart — a plain `.iso` never matches nkit, and
// an NKit image matches both (the user picks). The backend mirrors this via
// utils.path_utils.match_extension. One direction only: this app never *writes*
// NKit, so there is no verify class and no compress mode.
const NKIT_SOURCE_EXTS = ['.nkit.iso', '.nkit.gcz'];
const NKIT_VERIFY_EXTS = [];

/**
 * @typedef {Object} ModeEntry
 * @property {string} mode
 * @property {'create'|'extract'|'copy'|'compress'} kind
 * @property {string} label
 * @property {string} group
 * @property {string|null} outputExt
 * @property {string[]} inputExtensions
 * @property {string[]} [inputKinds]   ['directory'] for a folder-input mode; omitted ⇒ file
 * @property {boolean} supportsCompression
 * @property {boolean} supportsCompressionLevel
 * @property {boolean} supportsDeleteOnVerify
 * @property {boolean} allowsArchiveInput
 * @property {boolean} [supportsSplit] makeps3iso -s 4 GB FAT32 split (opt-in)
 */

/**
 * @typedef {Object} ToolDescriptor
 * @property {string} id
 * @property {string} label
 * @property {string} hint
 * @property {string} verifyPrefix              URL segment for /api/{prefix}-verify (or empty for /api/verify)
 * @property {string[]} sourceExts
 * @property {string[]} verifyExts
 * @property {string[]} modeGroups
 * @property {Record<string,string>} groups     group id → human label
 * @property {string} defaultMode               wire-mode the workspace selects when this tool is activated
 * @property {string} [glyph]                   short text affordance for the sidebar / dashboard (1-2 chars)
 * @property {string} [accent]                  CSS color or token for badges / chips (falls back to --accent)
 * @property {ModeEntry[]} modes
 * @property {(path: string) => Promise<object>} getInfo
 * @property {(path: string, opts?: object) => Promise<object>} verify
 * @property {(paths: string[], opts?: object) => Promise<object>} verifyBatch
 * @property {(path: string) => string} productPath
 */

/** Replace the trailing extension on a path, preserving the rest. */
function swapExt(path, newExt) {
  return path.replace(/\.[^./\\]+$/, newExt);
}

/**
 * Fallback compression-level range for the slider when a tool declares no
 * `compressionLevelRange`. Every level-capable tool sets its own (Dolphin
 * default 19, nsz 18), so this is only a safety net — but it lives here once
 * rather than re-inlined as `{ min: 1, max: 22, default: 19 }` in the
 * conversion store and the CompressionPicker.
 */
export const DEFAULT_COMPRESSION_LEVEL_RANGE = Object.freeze({ min: 1, max: 22, default: 19 });

/** @type {ToolDescriptor[]} */
export const TOOLS = [
  {
    id: 'chdman',
    // Per-tool default compression seed (replaces the old defaultCompressionFor
    // `if (toolId === ...)` ladder in conversion.svelte.js): the first selection
    // when this tool becomes primary, before the user opens the codec picker.
    defaultCompression: ['zlib'],
    label: 'CHDMAN',
    hint: 'Convert CD / DVD / LaserDisc images to and from CHD.',
    // chdman is the original endpoint set, so it has no URL prefix:
    // /api/verify, /api/verify-batch. Other tools use /api/<prefix>-verify.
    verifyPrefix: '',
    sourceExts: CHDMAN_SOURCE_EXTS,
    verifyExts: CHDMAN_VERIFY_EXTS,
    modeGroups: ['create', 'extract', 'copy'],
    groups: { create: 'Create', extract: 'Extract', copy: 'Copy' },
    defaultMode: 'createcd',
    glyph: 'CD',
    accent: 'var(--badge-cd)',
    // Compression options. chdman accepts a comma-separated codec list;
    // CompressionPicker offers the union of these as toggleable chips.
    compressionCodecs: [
      // Full codec catalog from the README + legacy UI. Backend
      // forwards arbitrary tokens to `chdman -c`, so adding entries
      // here only widens what the user can pick; chdman itself
      // ignores incompatible combinations for the active mode.
      { value: 'zlib',  label: 'zlib',  hint: 'Fast, baseline compatibility' },
      { value: 'zstd',  label: 'zstd',  hint: 'Modern general-purpose codec' },
      { value: 'lzma',  label: 'lzma',  hint: 'Slower, better ratio' },
      { value: 'lzma2', label: 'lzma2', hint: 'Best ratio, slowest' },
      { value: 'huff',  label: 'huff',  hint: 'Huffman entropy coder' },
      { value: 'flac',  label: 'flac',  hint: 'For CD audio tracks' },
      { value: 'cdzl',  label: 'cdzl',  hint: 'CD-zlib variant' },
      { value: 'cdzs',  label: 'cdzs',  hint: 'CD-zstd variant' },
      { value: 'cdlz',  label: 'cdlz',  hint: 'CD-lzma variant' },
      { value: 'cdfl',  label: 'cdfl',  hint: 'CD-FLAC variant' },
      { value: 'avhu',  label: 'avhu',  hint: 'Audio/video Huffman' },
    ],
    compressionStyle: 'multi',  // multi-codec list joined with commas
    // chdman's `-c` takes at most four codecs. Declared on the tool, not in
    // whichever picker happens to be on screen: the manual panel and the RomM
    // automation editor both build codec selections, and a cap only one of
    // them knows about is a rule the other quietly breaks.
    maxCodecs: 4,
    modes: [
      { mode: 'createraw', kind: 'create',  label: 'Create Raw', group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createhd',  kind: 'create',  label: 'Create HD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createcd',  kind: 'create',  label: 'Create CD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createdvd', kind: 'create',  label: 'Create DVD', group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createld',  kind: 'create',  label: 'Create LD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      // Extract modes accept a .chd straight out of an archive (decompress it
      // back to a disc image); the backend extracts the member first. Keep
      // copy/recompress below at false — re-CHD'ing a .chd from a zip is a
      // pointless round trip. Mirrors allows_archive_input in chdman.py.
      { mode: 'extractraw', kind: 'extract', label: 'Extract Raw', group: 'extract',
        outputExt: '.raw', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      { mode: 'extracthd',  kind: 'extract', label: 'Extract HD',  group: 'extract',
        outputExt: '.raw', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      { mode: 'extractcd',  kind: 'extract', label: 'Extract CD',  group: 'extract',
        outputExt: '.cue', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      { mode: 'extractdvd', kind: 'extract', label: 'Extract DVD', group: 'extract',
        outputExt: '.iso', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      { mode: 'extractld',  kind: 'extract', label: 'Extract LD',  group: 'extract',
        outputExt: '.avi', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      { mode: 'copy', kind: 'copy', label: 'Copy / Recompress', group: 'copy',
        outputExt: '.chd', inputExtensions: ['.chd'],
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: false },
    ],
    getInfo: (path) => api.getCHDInfo(path),
    verify: (path, opts) => api.verifyCHD(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchCHDs(paths, opts),
    productPath: (path) => swapExt(path, '.chd'),
  },
  {
    id: 'dolphin',
    defaultCompression: ['zstd'],
    label: 'Dolphin',
    hint: 'Compress GameCube / Wii discs to RVZ, WIA, or GCZ.',
    verifyPrefix: 'dolphin',
    sourceExts: DOLPHIN_SOURCE_EXTS,
    verifyExts: DOLPHIN_VERIFY_EXTS,
    modeGroups: ['dolphin'],
    groups: { dolphin: 'Dolphin' },
    defaultMode: 'dolphin_rvz',
    glyph: 'GC',
    accent: 'var(--badge-dat-match)',
    // Dolphin RVZ / WIA accept exactly one codec + a compression level.
    // GCZ ignores both; dolphin_iso is an extract op.
    compressionCodecs: [
      { value: 'zstd',  label: 'zstd',  hint: 'Recommended, level 19 matches MAME Redump' },
      { value: 'bzip2', label: 'bzip2', hint: 'Good ratio, slower' },
      { value: 'lzma',  label: 'lzma' },
      { value: 'lzma2', label: 'lzma2' },
      { value: 'none',  label: 'No compression' },
    ],
    compressionStyle: 'single-with-level',  // one codec + numeric level (0-22)
    compressionLevelRange: { min: 1, max: 22, default: 19 },
    modes: [
      { mode: 'dolphin_rvz', kind: 'compress', label: 'Dolphin RVZ', group: 'dolphin',
        outputExt: '.rvz', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_wia', kind: 'compress', label: 'Dolphin WIA', group: 'dolphin',
        outputExt: '.wia', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_gcz', kind: 'compress', label: 'Dolphin GCZ', group: 'dolphin',
        outputExt: '.gcz', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_iso', kind: 'extract', label: 'Dolphin ISO', group: 'dolphin',
        outputExt: '.iso', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
    ],
    getInfo: (path) => api.getDolphinInfo(path),
    verify: (path, opts) => api.verifyDolphin(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchDolphin(paths, opts),
    productPath: (path) => swapExt(path, '.rvz'),
  },
  {
    id: 'z3ds',
    defaultCompression: ['zlib'],
    label: '3DS',
    hint: 'Compress and decompress Nintendo 3DS ROMs (.3ds/.cci/.cia/.cxi/.3dsx ↔ Z3DS).',
    verifyPrefix: 'z3ds',
    sourceExts: Z3DS_SOURCE_EXTS,
    verifyExts: Z3DS_VERIFY_EXTS,
    modeGroups: ['z3ds'],
    groups: { z3ds: '3DS' },
    defaultMode: 'z3ds_compress',
    glyph: '3DS',
    accent: 'var(--badge-dvd)',
    // z3ds_compressor uses fixed compression; no codec or level controls.
    compressionCodecs: [],
    compressionStyle: 'none',
    modes: [
      { mode: 'z3ds_compress', kind: 'compress', label: 'Compress 3DS', group: 'z3ds',
        outputExt: null, inputExtensions: Z3DS_COMPRESS_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'z3ds_decompress', kind: 'extract', label: 'Decompress 3DS', group: 'z3ds',
        outputExt: null, inputExtensions: Z3DS_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
    ],
    getInfo: (path) => api.getZ3DSInfo(path),
    verify: (path, opts) => api.verify3DS(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchZ3DS(paths, opts),
    productPath: (path) => {
      const m = /\.(3ds|cci|cia|cxi|3dsx|zcci|zcia|z3ds|zcxi|z3dsx)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, Z3DS_OUT_MAP[ext] ?? ext);
    },
  },
  {
    id: 'nsz',
    defaultCompression: ['solid'],
    label: 'Switch',
    hint: 'Compress and decompress Nintendo Switch dumps (NSP/XCI ↔ NSZ/XCZ). Needs your own prod.keys.',
    verifyPrefix: 'nsz',
    sourceExts: NSZ_SOURCE_EXTS,
    verifyExts: NSZ_VERIFY_EXTS,
    modeGroups: ['nsz'],
    groups: { nsz: 'Nintendo Switch' },
    defaultMode: 'nsz_compress',
    glyph: 'NSW',
    accent: 'var(--badge-dat-match)',
    // nsz exposes a zstandard level plus a solid/block layout. We model the
    // layout as the "codec" dropdown and the level as the slider, reusing the
    // single-with-level picker. The wire value is "<solid|block>:<level>".
    compressionStyle: 'single-with-level',
    compressionCodecs: [
      { value: 'solid', label: 'Solid (smaller)',
        hint: 'Best ratio; must be decompressed to run. nsz default for NSZ.' },
      { value: 'block', label: 'Block (random access)',
        hint: 'Slightly larger; supports playing/installing without full decompression.' },
    ],
    compressionLevelRange: { min: 1, max: 22, default: 18 },
    modes: [
      { mode: 'nsz_compress', kind: 'compress', label: 'Compress (NSP/XCI → NSZ/XCZ)',
        group: 'nsz',
        outputExt: null, inputExtensions: NSZ_COMPRESS_EXTS,
        supportsCompression: false, supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'nsz_decompress', kind: 'extract', label: 'Decompress (NSZ/XCZ → NSP/XCI)',
        group: 'nsz',
        outputExt: null, inputExtensions: NSZ_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
    ],
    getInfo: (path) => api.getNszInfo(path),
    verify: (path, opts) => api.verifyNsz(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchNsz(paths, opts),
    productPath: (path) => {
      const m = /\.(nsp|xci|nsz|xcz)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, NSZ_OUT_MAP[ext] ?? ext);
    },
  },
  {
    id: 'cso',
    defaultCompression: ['max'],
    label: 'CSO',
    hint: 'Compress PSP / PS2 ISO images to CSO / CSO v2 / ZSO / DAX (and back).',
    verifyPrefix: 'cso',
    sourceExts: CSO_SOURCE_EXTS,
    verifyExts: CSO_VERIFY_EXTS,
    modeGroups: ['cso', 'chain'],
    groups: { cso: 'CSO / ZSO / DAX', chain: 'CSO → CHD' },
    defaultMode: 'cso_compress',
    glyph: 'CSO',
    accent: 'var(--badge-cso)',
    // The output format is fixed by the mode (CSO vs ZSO); the codec dropdown
    // picks a compression *effort* preset (no numeric level). 'single-with-level'
    // shows just the dropdown because the compress modes set
    // supportsCompressionLevel:false (the picker hides the slider then).
    compressionCodecs: [
      { value: 'default', label: 'Default', hint: 'Balanced (maxcso default trials)' },
      { value: 'fast', label: 'Fast', hint: 'Fastest, larger output (--fast)' },
      { value: 'max', label: 'Max', hint: 'Smallest, slowest (extra deflate/lz4 trials)' },
    ],
    compressionStyle: 'single-with-level',
    modes: [
      { mode: 'cso_compress', kind: 'compress', label: 'Compress ISO → CSO', group: 'cso',
        outputExt: '.cso', inputExtensions: CSO_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'cso2_compress', kind: 'compress', label: 'Compress ISO → CSO v2', group: 'cso',
        outputExt: '.cso', inputExtensions: CSO_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'zso_compress', kind: 'compress', label: 'Compress ISO → ZSO', group: 'cso',
        outputExt: '.zso', inputExtensions: CSO_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dax_compress', kind: 'compress', label: 'Compress ISO → DAX', group: 'cso',
        outputExt: '.dax', inputExtensions: CSO_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'cso_decompress', kind: 'extract', label: 'Decompress CSO/ZSO/DAX → ISO',
        group: 'cso',
        outputExt: '.iso', inputExtensions: CSO_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      // Composite pipeline (maxcso decompress -> chdman createdvd). The .chd is
      // verified/badged by the chdman tool entry; the final CHD uses chdman's
      // default compression, so no codec picker is shown here.
      { mode: 'cso_to_chd', kind: 'create', label: 'Convert to CHD (→ ISO → CHD)',
        group: 'chain',
        outputExt: '.chd', inputExtensions: CSO_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
    ],
    getInfo: (path) => api.getCsoInfo(path),
    verify: (path, opts) => api.verifyCso(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchCso(paths, opts),
    // mode lets ISO sources resolve to the right target (.cso vs .zso); it
    // defaults to the primary CSO product when the caller has no mode in hand.
    productPath: (path, mode = 'cso_compress') => {
      if (/\.iso$/i.test(path)) {
        // cso_compress and cso2_compress both produce the .cso container.
        const ext = mode === 'zso_compress' ? '.zso'
          : mode === 'dax_compress' ? '.dax'
          : '.cso';
        return swapExt(path, ext);
      }
      if (/\.(cso|zso|dax)$/i.test(path)) {
        // The chain packages a compressed source straight to .chd; plain
        // decompress yields the .iso.
        return swapExt(path, mode === 'cso_to_chd' ? '.chd' : '.iso');
      }
      return path;
    },
  },
  {
    id: 'romz',
    defaultCompression: ['max'],
    label: 'Handheld ROM',
    hint: 'Compress Game Boy / GBC / GBA / DS ROM dumps to .7z / .zip (and back).',
    verifyPrefix: 'romz',
    sourceExts: ROMZ_SOURCE_EXTS,
    verifyExts: ROMZ_VERIFY_EXTS,
    modeGroups: ['romz'],
    groups: { romz: 'GB / GBC / GBA / DS' },
    defaultMode: 'romz_7z',
    glyph: 'ROM',
    accent: 'var(--badge-romz)',
    // The output container is fixed by the mode (.7z vs .zip); the dropdown
    // picks a compression *effort* preset (no numeric level), like CSO.
    compressionCodecs: [
      { value: 'max', label: 'Max', hint: 'Smallest (-mx9 -md=256m -mfb=273)' },
      { value: 'default', label: 'Default', hint: 'Balanced (-mx7)' },
      { value: 'fast', label: 'Fast', hint: 'Fastest, larger output (-mx1)' },
    ],
    compressionStyle: 'single-with-level',
    // NOTE: keep allowsArchiveInput: false on every romz mode. The ROM packed
    // inside a romz archive still shows when you browse in, but that's the
    // backend's doing — archive browsing lists every known source extension
    // (registry.convertible_extensions), no frontend mirror needed.
    // Recompressing a ROM that's already archived would be plumb recursive, so
    // the backend rejects it; flippin' this flag here would offer up a
    // conversion the convert route refuses (ARCHIVE_INPUT_NOT_ALLOWED).
    modes: [
      { mode: 'romz_7z', kind: 'compress', label: 'Compress ROM → 7z', group: 'romz',
        outputExt: '.7z', inputExtensions: ROMZ_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: false },
      { mode: 'romz_zip', kind: 'compress', label: 'Compress ROM → zip', group: 'romz',
        outputExt: '.zip', inputExtensions: ROMZ_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: false },
      { mode: 'romz_extract', kind: 'extract', label: 'Extract ROM ← 7z/zip', group: 'romz',
        outputExt: null, inputExtensions: ROMZ_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
    ],
    getInfo: (path) => api.getRomzInfo(path),
    verify: (path, opts) => api.verifyRomz(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchRomz(paths, opts),
    // Compress preserves the ROM extension in front of the archive suffix
    // (Game.gba -> Game.gba.7z); extract strips the archive suffix back off.
    productPath: (path, mode = 'romz_7z') => {
      if (/\.(gb|gbc|gba|nds)$/i.test(path)) {
        return path + (mode === 'romz_zip' ? '.zip' : '.7z');
      }
      if (/\.(7z|zip)$/i.test(path)) return path.replace(/\.(7z|zip)$/i, '');
      return path;
    },
  },
  {
    id: 'makeps3iso',
    defaultCompression: ['zlib'],
    label: 'PS3 ISO',
    hint: 'Build a PS3 .iso from a decrypted disc/JB folder (a PS3_GAME/ root). No keys, no decryption.',
    // No verify endpoint: makeps3iso's only check is a backend PARAM.SFO
    // TITLE_ID readback, not a user-facing /api verify route.
    verifyPrefix: '',
    sourceExts: PS3_SOURCE_EXTS,
    verifyExts: PS3_VERIFY_EXTS,
    modeGroups: ['makeps3iso'],
    groups: { makeps3iso: 'PS3 Folder → ISO' },
    defaultMode: 'folder_to_iso',
    glyph: 'PS3',
    accent: 'var(--badge-dvd)',
    compressionCodecs: [],
    compressionStyle: 'none',
    modes: [
      // The only directory-input mode: its `inputKinds` flips fileBrowser
      // selection from "files only" to "convertible folders only".
      { mode: 'folder_to_iso', kind: 'create', label: 'PS3 Folder → ISO', group: 'makeps3iso',
        outputExt: '.iso', inputExtensions: [], inputKinds: ['directory'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false,
        // makeps3iso -s: split the output into ~4 GB parts for FAT32 targets.
        supportsSplit: true },
    ],
    // No file-path Info/Verify: a folder has no extension to route on. Left
    // undefined so infoToolsForPath / toolForVerifyPath skip this tool.
    getInfo: undefined,
    verify: undefined,
    verifyBatch: undefined,
    // The product is a sibling "<folder>.iso" (folder name, not a stem swap).
    productPath: (path) => `${(path ?? '').replace(/[/\\]+$/, '')}.iso`,
  },
  {
    id: 'nkit',
    label: 'NKit',
    hint: 'Restore an NKit-shrunk GameCube / Wii image back to a full .iso.',
    // No verify endpoint: nkit2iso has no verify subcommand. Integrity is the
    // NKit header CRC32 the binary checks inline during the restore, so a
    // completed job IS the verification.
    verifyPrefix: '',
    sourceExts: NKIT_SOURCE_EXTS,
    verifyExts: NKIT_VERIFY_EXTS,
    modeGroups: ['nkit', 'chain'],
    groups: { nkit: 'NKit → ISO', chain: 'One-step pipeline' },
    defaultMode: 'nkit_restore',
    glyph: 'NKT',
    accent: 'var(--badge-dvd)',
    // Fixed restore: the output is the original disc image, byte for byte.
    // Nothing to pick, so no codec list and no level slider.
    compressionCodecs: [],
    compressionStyle: 'none',
    modes: [
      { mode: 'nkit_restore', kind: 'extract', label: 'Restore ISO from NKit', group: 'nkit',
        outputExt: '.iso', inputExtensions: NKIT_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        // No delete-on-verify: this tool declares no verifyExts, so the
        // restored .iso can't be confirmed before dropping the source.
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
      // Composite pipeline (nkit2iso restore -> dolphin_rvz). The .rvz is
      // verified/badged by the Dolphin tool entry, and the full-size ISO stays
      // in a scratch dir instead of landing in the library. The final RVZ uses
      // dolphin's default compression, so no codec picker is shown here.
      { mode: 'nkit_to_rvz', kind: 'compress', label: 'Convert to RVZ (→ ISO → RVZ)',
        group: 'chain',
        outputExt: '.rvz', inputExtensions: NKIT_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        // NO delete-on-verify, even though Dolphin can verify the .rvz: that
        // check is structural (the container), not a match against the original
        // disc. A Wii source whose update partition was removed restores
        // zero-filled with its CRC32 check skipped, yet still yields a cleanly
        // verifying RVZ — deleting the NKit source then would destroy the only
        // file a recovery-enabled restore could ever use. Mirrors the backend
        // ChainSpec; parity is enforced by tests/test_frontend_parity_186.py.
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
    ],
    // Info reads the NKit header (console, game, restored size, CRC32). No
    // verify: nkit2iso has none, and claiming `.iso` would hijack every CD/DVD
    // ISO's Verify action — the same trap DOLPHIN_VERIFY_EXTS documents.
    getInfo: (path) => api.getNkitInfo(path),
    verify: undefined,
    verifyBatch: undefined,
    // Compound extension: strip the whole ".nkit.iso" / ".nkit.gcz", not just
    // the trailing suffix, or the "product" would be the source path itself.
    // `mode` picks the chain's .rvz over the plain restore's .iso.
    productPath: (path, mode = 'nkit_restore') => (path ?? '').replace(
      /\.nkit\.(iso|gcz)$/i, mode === 'nkit_to_rvz' ? '.rvz' : '.iso',
    ),
  },
  {
    id: 'jwud',
    defaultCompression: ['verify'],
    label: 'Wii U',
    hint: 'Compress and decompress Wii U disc images (WUD ↔ WUX). No keys needed.',
    verifyPrefix: 'jwud',
    sourceExts: JWUD_SOURCE_EXTS,
    verifyExts: JWUD_VERIFY_EXTS,
    modeGroups: ['jwud'],
    groups: { jwud: 'Wii U' },
    defaultMode: 'jwud_compress',
    glyph: 'WUD',
    accent: 'var(--badge-wiiu)',
    // WUX is a fixed sector-dedup format, so there is no codec and no level.
    // The dropdown carries JWUDTool's own verification pass instead — the same
    // way nsz's carries solid/block rather than a codec. 'single-with-level'
    // renders just the dropdown because both modes set
    // supportsCompressionLevel: false.
    compressionCodecs: [
      { value: 'verify', label: 'Verify against source',
        hint: 'JWUDTool re-reads both images and compares them byte for byte. Doubles the runtime.' },
      { value: 'noverify', label: 'Skip verification (faster)',
        hint: 'Passes -noVerify: about half the runtime, but the result is not checked against the source.' },
    ],
    compressionStyle: 'single-with-level',
    modes: [
      { mode: 'jwud_compress', kind: 'compress', label: 'Compress Wii U (WUD → WUX)',
        group: 'jwud',
        outputExt: '.wux', inputExtensions: JWUD_COMPRESS_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'jwud_decompress', kind: 'extract', label: 'Decompress Wii U (WUX → WUD)',
        group: 'jwud',
        outputExt: '.wud', inputExtensions: JWUD_VERIFY_EXTS,
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: true },
    ],
    getInfo: (path) => api.getJwudInfo(path),
    verify: (path, opts) => api.verifyJwud(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchJwud(paths, opts),
    productPath: (path) => {
      if (/\.wud$/i.test(path)) return swapExt(path, '.wux');
      if (/\.wux$/i.test(path)) return swapExt(path, '.wud');
      return path;
    },
  },
];

const byId = new Map(TOOLS.map((t) => [t.id, t]));
const byMode = new Map(
  TOOLS.flatMap((t) => t.modes.map((m) => [m.mode, { tool: t, spec: m }])),
);

function endsWithAny(path, exts) {
  return matchedExtLength(path, exts) > 0;
}

/**
 * Length of the LONGEST extension in `exts` that `path` ends with, else 0.
 * Mirrors the backend's `utils.path_utils.match_extension`, and the length is
 * what lets callers rank a compound claim (`.nkit.iso`) above the generic tail
 * (`.iso`) that other tools own.
 */
function matchedExtLength(path, exts) {
  if (!path) return 0;
  const lower = path.toLowerCase();
  let best = 0;
  for (const ext of exts) {
    if (lower.endsWith(ext) && ext.length > best) best = ext.length;
  }
  return best;
}

/**
 * Tools claiming `path` as a source, MOST SPECIFIC first (declared order
 * breaks ties). An NKit image is claimed by `nkit` via the compound
 * `.nkit.iso` and by chdman / Dolphin / CSO via the plain `.iso` tail; the
 * specific claim has to win, or the Info modal walks the generic tools first
 * and Dolphin's header reader "succeeds" on an NKit file (it has a real GC/Wii
 * disc header) and reports the shrunk image as if it were a plain disc.
 */
function sourceToolsBySpecificity(path) {
  return TOOLS
    .map((t) => ({ t, n: matchedExtLength(path, t.sourceExts) }))
    .filter((x) => x.n > 0)
    .sort((a, b) => b.n - a.n)
    .map((x) => x.t);
}

/** Verify-class tool whose verifyExts match the path (extension only). */
function matchVerifyTool(path) {
  if (!path) return null;
  return TOOLS.find((t) => endsWithAny(path, t.verifyExts)) ?? null;
}

/** Build the single/batch verify URL from the tool's verifyPrefix. */
function deriveVerifyUrl(tool, kind) {
  if (!tool) return null;
  const seg = tool.verifyPrefix ? `${tool.verifyPrefix}-verify` : 'verify';
  return kind === 'batch' ? `/api/${seg}-batch/events` : `/api/${seg}/events`;
}

export const registry = {
  /** All registered tools, in declared order. */
  all: () => TOOLS,

  /** Set of registered tool ids, replaces hardcoded VALID_TOOLS in callers. */
  ids: () => new Set(TOOLS.map((t) => t.id)),

  /** Lookup by tool id (`'chdman'`, `'dolphin'`, `'z3ds'`). */
  forTool: (id) => byId.get(id),

  /** Mode metadata (ModeEntry) for a wire-mode value. */
  specFor: (mode) => byMode.get(mode)?.spec,

  /** The owning tool for a wire-mode value. */
  toolForMode: (mode) => byMode.get(mode)?.tool,

  /** How many codecs this mode's tool accepts at once (Infinity if unbounded). */
  maxCodecsFor: (mode) => byMode.get(mode)?.tool?.maxCodecs ?? Infinity,

  /** Pick the tool whose verify_extensions match a given file path, or null.
   *  Pure extension match — use `verifyToolForPath` when an entry's backend
   *  `verifiable_by` refinement should also apply (e.g. romz archive rows). */
  toolForVerifyPath: (path) => matchVerifyTool(path),

  /**
   * Verify tool for a concrete file, narrowed by the backend's per-file
   * `verifiable_by` refinement when provided. The extension match stays the
   * source of truth — it encodes deliberate UX exclusions the backend's
   * broader verify_extensions don't (e.g. `.iso` is intentionally NOT routed
   * to Dolphin verify) — and `verifiable_by` may only NARROW it, never broaden
   * it (romz claims `.7z`/`.zip` by extension but only single-ROM archives are
   * actually verifiable). Pass the listing's `entry.verifiable_by`; omit it
   * (undefined) for paths the listing doesn't annotate, leaving the plain
   * extension match. Shared by the row menu and the bulk-verify paths so they
   * gate identically.
   */
  verifyToolForPath: (path, verifiableBy) => {
    const tool = matchVerifyTool(path);
    if (!tool) return null;
    if (Array.isArray(verifiableBy) && !verifiableBy.includes(tool.id)) {
      return null;
    }
    return tool;
  },

  /** Tools whose source extensions match the given path (convertible sources),
   *  most-specific claim first — see `sourceToolsBySpecificity`. */
  toolsForSourcePath: (path) => sourceToolsBySpecificity(path),

  /**
   * Info-capable tools for a path, richest-first, deduped by id. A path
   * can be claimed by more than one tool (a raw .iso is both a chdman
   * create source and a Dolphin disc) and only one actually reads it, so
   * callers try these in order and keep the first getInfo() that returns.
   * Order: most-specific claim first, with the verify-path owner winning ties.
   * Tools without a getInfo binding are skipped.
   */
  infoToolsForPath: (path) => {
    if (!path) return [];
    // Rank EVERY claim — source or verify — by matched-extension length, so the
    // tool that understands the container comes first. The verify owner must
    // not simply be prepended: for `Game.nkit.gcz` that is Dolphin (via its
    // generic `.gcz`), whose reader parses the GCZ and its embedded disc header
    // successfully, so the modal would stop there and never reach /nkit-info.
    // Verify ownership only breaks ties at equal specificity, which preserves
    // the old order for every single-suffix path (e.g. a `.chd` still starts at
    // chdman). Dedup by reference; skip tools without a getInfo binding.
    const verifyOwner = matchVerifyTool(path);
    const ordered = TOOLS
      .map((t) => ({
        t,
        n: Math.max(
          matchedExtLength(path, t.sourceExts),
          matchedExtLength(path, t.verifyExts),
        ),
        v: t === verifyOwner ? 1 : 0,
      }))
      .filter((x) => x.n > 0)
      .sort((a, b) => b.n - a.n || b.v - a.v)
      .map((x) => x.t);
    const out = [];
    for (const t of ordered) {
      if (t && typeof t.getInfo === 'function' && !out.includes(t)) {
        out.push(t);
      }
    }
    return out;
  },

  /** Verify URL, single or batch, for a tool id. Derived from verifyPrefix. */
  verifyUrl: (toolId, kind) => deriveVerifyUrl(byId.get(toolId), kind),

  /** Group a tool's modes by `group` for menu rendering. */
  modesByGroup: (toolId) => {
    const tool = byId.get(toolId);
    if (!tool) return new Map();
    const out = new Map();
    for (const mode of tool.modes) {
      const list = out.get(mode.group) ?? [];
      list.push(mode);
      out.set(mode.group, list);
    }
    return out;
  },

  /**
   * Group label lookup. Each tool owns its group → label map via the
   * `groups` field; falls back to the raw group id so a new tool with
   * an unknown group still renders something readable.
   */
  groupLabel: (group, toolId) => {
    if (toolId) {
      const tool = byId.get(toolId);
      if (tool?.groups?.[group]) return tool.groups[group];
    }
    // Fallback: search every tool for the first matching group label
    // (useful when the caller doesn't know which tool owns the group).
    for (const tool of TOOLS) {
      if (tool.groups?.[group]) return tool.groups[group];
    }
    return group;
  },

  /** Default wire-mode for a tool, used when the workspace switches tools. */
  defaultMode: (toolId) => byId.get(toolId)?.defaultMode
    ?? byId.get(toolId)?.modes[0]?.mode
    ?? null,

  /**
   * Distinct source extensions across all registered tools, in
   * declaration order. Used by FileList's filter dropdown so adding a
   * new tool surfaces its inputs automatically, no hardcoded list.
   */
  allSourceExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.sourceExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },

  /** Distinct verify (output-class) extensions across all tools. */
  allVerifyExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.verifyExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },

  /**
   * Union of source + verify extensions, every extension the user
   * might want to filter by in a directory listing. Adding a tool
   * automatically surfaces its inputs and outputs in the filter
   * dropdown; no UI edits required.
   */
  allFilterableExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.sourceExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
      for (const ext of tool.verifyExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },
};
