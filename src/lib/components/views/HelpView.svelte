<script>
  // Repo links come from package.json so they live in one place. `bugs`
  // is the issues URL; `repository.url` is the npm git form
  // (git+https://….git), normalized to a browsable https URL.
  import { repository, bugs } from '../../../../package.json';
  import { registry } from '$lib/tools/registry.js';
  import { helpModeSections } from '$lib/tools/helpModes.js';

  const repoUrl = repository.url.replace(/^git\+/, '').replace(/\.git$/, '');
  const issuesUrl = bugs?.url ?? `${repoUrl}/issues`;

  // Static curated help. The tool/format/mode facts mirror the registry
  // (src/lib/tools/registry.js) and the README; kept as prose here on
  // purpose so the page reads like a guide, not a config dump.
  const tools = [
    {
      glyph: 'CD',
      name: 'CHDMAN',
      blurb: 'CD, DVD, and LaserDisc images to CHD, the format MAME and most modern emulators read. This is the one most people want: PS1, PS2, PSP, Saturn, Sega CD, Dreamcast, Neo Geo CD. Feed it a cue/bin pair, a .gdi, or a plain .iso and you get one .chd that is smaller and verifiable.',
      io: '.gdi / .cue / .bin / .iso  →  .chd',
    },
    {
      glyph: 'GC',
      name: 'Dolphin',
      blurb: 'GameCube and Wii discs. Three outputs: RVZ is the modern one, compresses best, and Dolphin reads it natively. WIA is the older format RVZ replaced, so use it only if something specifically wants it. GCZ is older still and uses fixed compression. When in doubt, RVZ.',
      io: '.iso / .gcz / .wia / .rvz / .wbfs  →  .rvz / .wia / .gcz / .iso',
    },
    {
      glyph: '3DS',
      name: '3DS',
      blurb: 'Nintendo 3DS ROMs to a seekable-Zstandard file that Azahar reads directly (release 2123 and up). Roughly half the size with no compatibility loss, and now fully reversible — decompress back to the original ROM any time. The .cci and .3ds dumps are solid; .cia, .cxi and .3dsx work but treat them as experimental. The ROM has to be decrypted first.',
      io: '.cci / .cia / .3ds / .cxi / .3dsx  ↔  .zcci / .zcia / .z3ds / .zcxi / .z3dsx',
    },
    {
      glyph: 'NSW',
      name: 'Switch',
      blurb: 'Nintendo Switch dumps to NSZ/XCZ and back, using nsz, the format Tinfoil and DBI read. Usually 40 to 80 percent smaller, fully reversible. Switch content is encrypted, so this needs your own prod.keys, dumped from a console you own. Without keys the Switch tool is hidden entirely. Set SWITCH_KEYS to the folder holding them, or drop them under a mounted volume and the app finds them.',
      io: '.nsp / .xci  ↔  .nsz / .xcz',
    },
    {
      glyph: 'CSO',
      name: 'CSO',
      blurb: 'PSP and PS2 game images to CSO, CSO v2, ZSO, or DAX, the compressed ISO formats PPSSPP and PCSX2 read directly, so the compressed file plays without a separate decompress step. The CSO formats are lossless and fully reversible, using maxcso. CSO v1 is the deflate-based, universally-supported default; CSO v2 improves block alignment for recent emulators; ZSO uses lz4 for faster decoding; DAX is a legacy PSP format. No keys needed. An ISO can also go to CHDMAN or Dolphin instead; the tool picker decides. There is also a one-step Convert to CHD mode (cso_to_chd); that one changes container, so extracting the CHD later gives an ISO, not the original CSO/ZSO/DAX.',
      io: '.iso  ↔  .cso / .zso / .dax',
    },
    {
      glyph: 'ROM',
      name: 'Handheld ROM',
      blurb: 'Game Boy, Game Boy Color, Game Boy Advance, and Nintendo DS ROM dumps to a standard .7z or .zip archive, and back. Archive-quality lossless compression with the 7z tool: GBA dumps shrink by roughly half, GB/GBC by around two thirds. Reverting is just extraction. .7z gives the smallest file (LZMA2); .zip trades some size for the broadest compatibility. No keys needed.',
      io: '.gb / .gbc / .gba / .nds  ↔  .7z / .zip',
    },
    {
      glyph: 'PS3',
      name: 'PS3 ISO',
      blurb: 'A decrypted PS3 disc or JB folder packed into a single .iso that RPCS3 mounts directly, using makeps3iso. This is the one tool that takes a folder instead of a file, and the only tool with no reverse mode at all: it repackages a folder you already decrypted, and never deletes it. Most PS3 discs are over 4 GB, so for a FAT32 drive there is a per-job toggle that splits the image into 4 GB parts (RPCS3 mounts the .0). No keys, and no decryption here.',
      io: 'the folder holding PS3_GAME/  →  .iso',
    },
    {
      glyph: 'NKT',
      name: 'NKit',
      blurb: 'NKit-shrunk GameCube and Wii images back to a plain, full-size .iso, using nkit2iso. NKit shrinks a disc by dropping everything a program can recreate — the junk padding, the gaps, and for Wii the encryption and hash tree — so restoring means rebuilding all of it and checking the result against the CRC32 stored in the NKit header. There are no per-job settings to choose, and the output is bit-exact and CRC32-verified — except for one case: a Wii image whose update partition was removed at shrink time has no way back to bit-exact, because that data is not in the file. By default that region is zero-filled and the CRC32 check is skipped, so the ISO is playable but not redump-verifiable, and the job says so plainly. One direction only: this app never writes NKit. There is also a one-step Convert to RVZ mode (nkit_to_rvz) that restores and compresses in a single job, keeping the full-size ISO in a scratch directory instead of your library — usually what you want, since no emulator reads NKit and Dolphin reads RVZ natively. Info reads the NKit header, so you can see the console, game and restored size before spending the restore. Set NKIT2ISO_RECOVERY=download if you want the tool to fetch the publicly archived recovery partition for those images instead and get a bit-exact, redump-verified restore — the only case where it uses the network.',
      io: '.nkit.iso / .nkit.gcz  →  .iso  (or  →  .rvz  in one step)',
    },
    {
      glyph: 'WUD',
      name: 'Wii U',
      blurb: 'Wii U disc dumps to WUX and back, using JWUDTool. Every Wii U disc image is exactly 25 GB regardless of how much of it the game uses, so WUX deduplicates the repeated sectors and typically lands a fraction of that; Cemu reads the .wux directly. Lossless and fully reversible. No keys needed either — this only repacks the image, it never decrypts it, so a WUX stays as encrypted as the WUD it came from. JWUDTool compares the result against the source byte for byte before the job reports success, which is why a compress job takes about twice as long as the write itself; the dropdown can turn that off if you want the speed instead. Split dumps from wudump (game_part1.wud through game_part12.wud) work too — pick part 1 and the whole set converts as one image.',
      io: '.wud  ↔  .wux',
    },
  ];

  // Per-tool mode reference. Generated from the registry (one section per
  // tool + mode-group, in registry order) so the mode set and output columns
  // can't drift from src/lib/tools/registry.js; the human blurbs and any
  // multi-output display strings live in src/lib/tools/helpModes.js.
  const modeGroups = helpModeSections(registry);
</script>

<section class="view" aria-labelledby="help-title">
  <header class="header">
    <h1 id="help-title">Help</h1>
    <p class="hint">Tools, formats, compression, verification, and the workflow around them.</p>
  </header>

  <article class="panel">
    <h2 class="panel-title">Pick a tool</h2>
    <p class="lead">
      Start by choosing the tool that matches your files. The rest of the interface
      filters itself to that tool, so you only ever see modes and formats that apply.
      The file list also greys out anything the current tool can't touch. Most tools
      take a file; PS3 ISO is the exception and takes a decrypted PS3 folder.
    </p>
    <ul class="tool-list">
      {#each tools as tool (tool.name)}
        <li class="tool">
          <span class="glyph" aria-hidden="true">{tool.glyph}</span>
          <div class="tool-body">
            <div class="tool-name">{tool.name}</div>
            <p class="tool-blurb">{tool.blurb}</p>
            <code class="io">{tool.io}</code>
          </div>
        </li>
      {/each}
    </ul>
  </article>

  <article class="panel">
    <h2 class="panel-title">Browsing and finding files</h2>
    <p class="lead">
      Your mounted volumes show up in the left panel. Click a folder to go in, click a
      breadcrumb to come back out.
    </p>
    <p>
      Two things make big libraries manageable. <strong>Search All</strong> walks the
      whole volume and pulls every convertible file into one list, so you don't have to
      dig through folders by hand. The <strong>filter dropdown</strong> narrows the list
      by extension. Both update automatically as tools are added, so the filter always
      knows every format the app handles.
    </p>
    <p>
      Archives (ZIP, 7z, RAR) are browsable like folders. Click one and you're inside it.
      More on that below.
    </p>
    <p>
      System clutter is hidden automatically, so listings stay clean:
      <code>.DS_Store</code>, AppleDouble (<code>._*</code>) files,
      <code>Thumbs.db</code>, <code>desktop.ini</code>, <code>@eaDir</code>,
      <code>#recycle</code>, <code>lost+found</code>, and the like across macOS,
      Windows, and NAS systems.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">Modes</h2>
    <p class="lead">
      Create makes a compressed file, extract gives you the original back, copy
      recompresses in place. Pick the create mode that matches the media. If you compress
      a PSP or PS2 image with createcd instead of createdvd it will not come out right.
    </p>
    {#each modeGroups as group (group.title)}
      <div class="mode-tool">
        <h3 class="mode-tool-name">{group.title}</h3>
        <table class="mode-table">
          <thead>
            <tr><th scope="col">Mode</th><th scope="col">What it does</th><th scope="col">Output</th></tr>
          </thead>
          <tbody>
            {#each group.rows as { mode, note, out } (mode)}
              <tr>
                <th scope="row"><code>{mode}</code></th>
                <td class="note">{note}</td>
                <td><code>{out}</code></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/each}
  </article>

  <article class="panel">
    <h2 class="panel-title">Compression</h2>
    <p class="lead">
      CHD create and copy modes take a list of codecs. Dolphin RVZ/WIA take one codec plus a
      level, Switch compress takes a layout plus a level, and CSO/CSO v2/ZSO/DAX compress
      take an effort preset (Fast / Default / Max). GCZ, the decompress/extract modes, and
      3DS have no settings at all.
    </p>
    <p>
      Smaller is not always better. Some emulators only read certain codecs, and a file
      that won't load is worse than one that's a bit bigger. Here's what each codec is for.
    </p>
    <table class="codec-table">
      <thead>
        <tr><th scope="col">Codec</th><th scope="col">Use it for</th></tr>
      </thead>
      <tbody>
        <tr><th scope="row">zlib</th><td>Best compatibility. If an emulator refuses a CHD, reconvert with zlib only.</td></tr>
        <tr><th scope="row">zstd</th><td>Fast and small. The modern default, but older software may not support it.</td></tr>
        <tr><th scope="row">lzma / lzma2</th><td>Smallest files, slowest to make. Good for archival.</td></tr>
        <tr><th scope="row">flac</th><td>CD audio tracks.</td></tr>
        <tr><th scope="row">cdzl / cdzs / cdlz / cdfl</th><td>CD-specific variants of the above. CD images only.</td></tr>
        <tr><th scope="row">huff / avhu</th><td>Huffman and audio/video Huffman entropy coders.</td></tr>
        <tr><th scope="row">No compression</th><td>Uncompressed. Maximum compatibility, no size win.</td></tr>
      </tbody>
    </table>
    <p>
      For CHD you can stack up to four codecs in one pass. chdman tries them per chunk and
      keeps whichever wins, so a CD image can end up using cdfl for the audio and cdlz for
      the data on its own. Order doesn't matter. If you don't know what to pick, the
      defaults are fine.
    </p>
    <p class="callout">
      AetherSX2 and NetherSX2 only read zlib-compressed CHDs. If you see
      "Failed to initialize cdvd," that's the cause. Reconvert with zlib only, or use the
      copy mode to recompress without redoing the whole thing.
    </p>
    <p>
      For Dolphin, zstd at level 19 is the sweet spot and matches what MAME Redump uses.
      Levels run to 22 but the gains taper off fast and the time cost doesn't.
    </p>
    <p>
      For Switch, the layout is Solid or Block. Solid packs tightest but the file must be
      fully decompressed before it runs; Block is a little larger but stays installable and
      playable without unpacking first. Level runs 1 to 22; the default 18 is a good balance.
      Your choice is remembered per tool between sessions.
    </p>
    <p>
      CSO works differently: you don't pick a codec, you pick a <strong>format</strong> (which
      <em>is</em> the mode) and an <strong>effort</strong> preset. The format decides which
      container you get — <strong>CSO</strong> (v1, deflate, the universally-supported default
      that every PPSSPP/PCSX2 reads), <strong>CSO v2</strong> (better block alignment, needs a
      recent PPSSPP/PCSX2), <strong>ZSO</strong> (lz4, decodes the fastest), or
      <strong>DAX</strong> (legacy PSP format some older tools expect). When in doubt, use plain
      CSO. CSO v1 and CSO v2 both produce a <code>.cso</code> file; the version differs inside.
    </p>
    <p>
      The effort preset trades speed for size: <strong>Fast</strong> for the quickest result,
      <strong>Default</strong> for a balanced trade-off, or <strong>Max</strong> for the
      smallest file at the cost of speed (extra Zopfli + libdeflate trials for CSO/CSO v2/DAX,
      brute-forced lz4 for ZSO). There is no numeric level — maxcso has no level concept, so the
      preset is the only knob. Every preset is lossless, so the decompressed ISO comes back
      byte-for-byte identical; only the compressed file's size and the time to make it change.
      CSO ships a strong default — the <strong>Max</strong> preset — for the smallest files out
      of the box. Your choice is remembered per tool between sessions.
    </p>
    <p>
      Every tool that takes compression settings (CHD, Dolphin, Switch, CSO) has a
      <strong>Reset to default</strong> button under its picker: one click puts that tool's
      codec / layout / level / effort back to its default and confirms with a toast. It's
      greyed out when you're already on the defaults.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">Where output goes and duplicates</h2>
    <p class="lead">
      By default the converted file lands next to its source. You can set a custom output
      directory instead, and it gets created for you as long as it's inside one of your
      volumes.
    </p>
    <p>
      If a matching output already exists, the app stops and asks rather than clobbering
      it. You get three choices: <strong>skip</strong> leaves the existing file alone,
      <strong>rename</strong> writes the new one under a different name, and
      <strong>overwrite</strong> replaces it. Nothing is overwritten without you saying so.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">The job queue</h2>
    <p class="lead">
      Every conversion and verify runs through one queue, first in first out. By default
      it runs one job at a time, which keeps the host responsive during big batches.
    </p>
    <p>
      Progress updates live as jobs run. The queue panel has tabs for active, completed,
      and failed jobs. <strong>Cancel All</strong> stops everything queued and in flight,
      and <strong>Clear Done</strong> wipes the finished list. Both ask for confirmation
      first, since you can't undo them. If the queue ever wedges, there's a recovery action
      to unstick it.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">Verifying and deleting safely</h2>
    <p class="lead">
      Converting never deletes anything on its own. The safe order is convert, verify, then
      delete the source.
    </p>
    <p>
      Verify reads the whole output back and checks it against the hashes baked into the
      file. If you turn on delete-on-verify, the source is removed only after the output
      passes. If verify fails, the source stays exactly where it was. You get a confirmation
      list of everything that will be deleted before anything happens, including the
      <code>.cue</code> and <code>.gdi</code> track files that ride along with a
      <code>.bin</code>, and the whole archive if the source came from one.
    </p>
    <p>
      Bulk Verify checks a whole selection at once across CHD, Dolphin, 3DS, Switch, and
      CSO files. Verification status sticks around between sessions, so a file you verified last
      week still shows verified today. Long verifies can be given a timeout so a stalled one
      doesn't sit forever.
    </p>
    <p>
      CSO verifies a little differently: it decompresses the whole <code>.cso</code>,
      <code>.zso</code>, or <code>.dax</code> container and logs its CRC32, so a clean run
      proves the file decodes end to end. Because the proof is on the <em>compressed</em>
      output, delete-on-verify is offered on the CSO compress modes (CSO, CSO v2, ZSO, DAX) but
      not on decompress, where the plain <code>.iso</code> output has nothing to check it
      against.
    </p>
    <p>
      Deleting from the file list is a separate action. A file or an empty folder takes one
      confirmation. A <strong>non-empty folder</strong> takes two: the second spells out
      that it removes everything inside, permanently. Either way the app refuses the
      delete if a conversion is using anything under it.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">File info</h2>
    <p class="lead">
      Click a <code>.chd</code> to open the inspector. It shows the CHD version,
      compression, size, and both the SHA1 and data SHA1, plus the raw chdman output if you
      want the gory detail.
    </p>
    <p>
      For PS1, PS2, PSP, and Dreamcast discs it also digs the game serial out of the sector
      data and shows a human title when it can recognize one. Dolphin files have their own
      info view with game ID, region, format, and compression. A background scan caches this
      so the badges in the file list don't have to recompute every time you open a folder.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">DAT matching</h2>
    <p class="lead">
      Sync the MAMERedump DATs from the DAT Library and converted files get checked against
      known-good hashes. A blue DAT badge means the file matches a Redump entry.
    </p>
    <p>
      One click pulls roughly 69 DATs straight from the MAMERedump GitHub. You can also
      import your own <code>.dat</code> or <code>.xml</code> from No-Intro or Redump. These
      matches feed tools like Hasheous and RomM, so a verified set carries over.
    </p>
    <p>
      CHDs match on the header SHA1, which is codec-independent. Dolphin RVZ/WIA/GCZ match on
      the game image's content SHA1, reconstructed on the fly by
      <code>dolphin-tool verify --algorithm sha1</code> — the same hash Redump records — so any
      compression setting still matches for both. A missing badge usually means the DATs aren't
      synced, the title isn't in the DAT, or the file is over <code>MATCH_MAX_FILE_SIZE</code>
      (which skips the expensive full-disc reconstruction). It's about hash matching, not file
      health, so verify is the real proof your file is good.
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">Archives</h2>
    <p class="lead">
      You can browse straight into a ZIP, 7z, or RAR and convert a file from inside it. No
      need to extract first.
    </p>
    <p>
      The archive is unpacked to a temp directory for the conversion and cleaned up after.
      Any convertible source works this way — 3DS ROMs, Dolphin discs, Switch dumps
      (<code>.nsp</code>/<code>.xci</code>, your own <code>prod.keys</code> still required), and PSP/PS2
      images (both the <code>.iso</code> you compress and the <code>.cso</code>/<code>.zso</code>/<code>.dax</code>
      you decompress). CHDMAN's extract modes can even pull a <code>.chd</code> out of an
      archive and decompress it back to a game image. CHDMAN's copy/recompress mode is
      the one exception: recompressing an already-finished <code>.chd</code> straight out of
      an archive is a pointless round trip, so it isn't offered there. Handheld ROM doesn't
      take archive members either: a loose <code>.gb</code>/<code>.gba</code>/<code>.nds</code>
      inside an archive is shown but not converted in place, since its <code>.7z</code>/<code>.zip</code>
      are the packed product (unpack one by selecting the archive and running
      <code>romz_extract</code>). PS3 ISO sits outside this entirely: it takes a folder, not a
      file, so a zipped <code>PS3_GAME</code> tree is never an archive input.
    </p>
    <p>
      When a <code>.cue</code> or <code>.gdi</code> sits next to its <code>.bin</code>
      inside an archive, the loose <code>.bin</code> entries are hidden and batch jobs are
      deduplicated by output, so you don't end up with two jobs fighting over the same file.
    </p>
    <h3 class="sub-title">Why only certain files show inside an archive</h3>
    <p>
      Browsing into an archive doesn't list everything in it — it lists the file types the
      app <em>knows</em>. The listing is <strong>global, scoped to known extensions</strong>:
      a member appears if and only if its extension is one some tool recognizes as a
      convertible source (<code>.iso</code>, <code>.cue</code>/<code>.bin</code>,
      <code>.gdi</code>, the Dolphin and 3DS formats, <code>.nsp</code>/<code>.xci</code>,
      <code>.cso</code>/<code>.zso</code>/<code>.dax</code>, the handheld ROMs
      <code>.gb</code>/<code>.gbc</code>/<code>.gba</code>/<code>.nds</code>,
      …), plus a <code>.chd</code> you can decompress in place — no matter which tool you
      currently have selected.
    </p>
    <p>
      Everything else is hidden on purpose: <strong>unknown files</strong> (read-me text,
      <code>.nfo</code>/<code>.sfv</code>, box art, manuals, save states) that the app can't
      convert or verify, <strong>nested archives</strong> (a <code>.zip</code> inside a
      <code>.zip</code>), and <strong>OS/NAS clutter</strong> (<code>__MACOSX/…</code>,
      <code>.DS_Store</code>, <code>Thumbs.db</code>). So if a file you expected isn't in the
      list, it's almost always because its extension isn't one of the convertible/verifiable
      types — the same rule the on-disk file list follows.
    </p>
    <p>
      A few members that <em>do</em> show are view-only and badged non-convertible. A handheld
      ROM this app packed (<code>Game.gba</code> inside <code>Game.gba.7z</code>) is shown so
      you can see and verify it, but it isn't offered for re-conversion — recompressing an
      already-archived ROM would be recursive; to unpack it, select the archive itself and run
      <code>romz_extract</code>. A <code>.chd</code> inside an archive can be decompressed in
      place but not recompressed (copy/recompress acts on a finished output).
    </p>
  </article>

  <article class="panel">
    <h2 class="panel-title">When something goes wrong</h2>
    <dl class="trouble">
      <dt>An emulator won't read the CHD</dt>
      <dd>It's almost always the codec. Recompress with zlib only using the copy mode.</dd>

      <dt>"Output already exists"</dt>
      <dd>The app found a file with the same name. Pick skip, rename, or overwrite. It won't replace anything unless you choose overwrite.</dd>

      <dt>A conversion stalls or sits forever</dt>
      <dd>
        Big files take a while, and there's an adaptive stall timeout that scales with input
        size before the app gives up. If a job genuinely hangs, the queue has a recovery
        action. Check that your temp directory has room and ideally lives on a fast disk.
      </dd>

      <dt>Verify is taking ages</dt>
      <dd>Verify reads the entire file, so a large disc is slow by nature. You can set a timeout if you'd rather it bail than block the queue.</dd>

      <dt>A file didn't show up in the list</dt>
      <dd>Make sure the right tool is selected, since the list filters to it. Try Search All for a recursive sweep, or clear the extension filter.</dd>

      <dt>An ISO went to the wrong tool</dt>
      <dd>ISOs can belong to CHDMAN or Dolphin. The ISO handling toggle decides which one handles info, verify, and conversion. Set it to match the disc.</dd>

      <dt>A 3DS ROM failed to compress</dt>
      <dd>The ROM has to be decrypted first. Encrypted dumps won't work.</dd>

      <dt>The Switch tool isn't there, or jobs fail asking for prod.keys</dt>
      <dd>Switch needs your own <code>prod.keys</code>. Set <code>SWITCH_KEYS</code> to the folder that holds them, or drop <code>prod.keys</code> under a mounted volume and the app finds it. Without keys the Switch tool is hidden. The keys come from a console you own; none ship with the app.</dd>

      <dt>The host got sluggish during a batch</dt>
      <dd>
        Conversions are CPU and I/O heavy. The defaults run one job at a time on purpose. If
        you raised the concurrency, lower it back, or nice the process down. The README has
        the full tuning table.
      </dd>
    </dl>
  </article>

  <article class="panel">
    <h2 class="panel-title">FAQ and edge cases</h2>
    <dl class="qa">
      <dt>Will compression hurt the game?</dt>
      <dd>No. Every format here is lossless. The data you get back after decompression is bit-for-bit identical to what went in. Smaller file, same game.</dd>

      <dt>Does converting touch my original file?</dt>
      <dd>No. The output is a new file and the source is left alone, unless you explicitly turn on delete-on-verify. Even then the source is only removed after the output passes verification.</dd>

      <dt>If I close the browser mid-conversion, does the job die?</dt>
      <dd>No. Jobs run on the server, not in your tab. Close the browser, reboot your laptop, come back later, and the queue is still chugging. Reopen the page and it reconnects to whatever's running.</dd>

      <dt>Can I undo a conversion?</dt>
      <dd>There's no undo button, but you usually don't need one. Your source is still there. If you already deleted it, you can run an extract mode to rebuild the original game image from a CHD, or dolphin_iso to get a plain ISO back from an RVZ/WIA/GCZ.</dd>

      <dt>I converted to CHD and it barely got smaller. Why?</dt>
      <dd>Some content just doesn't compress. Already-compressed audio or video, encrypted data, and a lot of PS2 DVDs are close to incompressible. A weak result usually means the disc was already dense, not that something went wrong. Double-check you used the right create mode too.</dd>

      <dt>What's the difference between .3ds and .cci?</dt>
      <dd>Nothing. They're the same cartridge-dump format with two different extensions. Rename one to the other freely. The tool keeps the naming convention, so .3ds becomes .z3ds and .cci becomes .zcci.</dd>

      <dt>Do I actually have to verify?</dt>
      <dd>It's optional, but it's the only way to know the output is good before you delete the source. If you're keeping the source anyway, skip it. If you're using delete-on-verify, the verify is the whole point of the safety net.</dd>

      <dt>Can I batch different formats and tools at once?</dt>
      <dd>You can select a pile of files and queue them together. They all run through the same queue. Each file is converted by whichever tool and mode you set, so plan a batch around one tool at a time.</dd>

      <dt>How do I select every file in a huge folder?</dt>
      <dd>
        The checkbox in the table header covers the page you're on. When the folder runs to
        more than one page, ticking it puts a banner just under the selection bar offering the
        rest — <em>"Select all 2,140 items in this folder"</em> — which takes the whole folder in
        one click. Shift-clicking the header checkbox does the same thing directly. It picks up
        only what the current tool and mode accept, and it honours the extension filter, so
        filtering to <code>.iso</code> first selects just the ISOs. It works the same inside an
        archive and on <strong>Search all</strong> results.
      </dd>

      <dt>Can I show more files per page?</dt>
      <dd>
        Yes — the <strong>Rows</strong> picker in the bottom-right of the file list takes
        25, 50, 100, 250, or 500, and it's remembered for next time. Changing it keeps your
        place instead of jumping back to page 1. It stops at 500 on purpose: every row on
        screen gets checked for archive contents, CHD details, and a DAT match, so a single
        page holding a whole 2,000-file folder would set all of that going at once. If you
        just want everything selected, use <strong>Select all</strong> instead — it spans
        every page.
      </dd>

      <dt>What happens to a job that fails?</dt>
      <dd>It moves to the failed tab with the error attached, and your source is untouched. Read the error, fix the cause (usually a codec, a bad dump, or no disk space), and requeue it.</dd>

      <dt>I have a multi-disc game. How do I handle it?</dt>
      <dd>Convert each disc as its own file. There's no multi-disc container here. Most front-ends and emulators handle disc swapping from the individual files or an .m3u you make yourself.</dd>

      <dt>Can I change a CHD's codec without re-ripping the disc?</dt>
      <dd>Yes. That's exactly what copy mode is for. Point it at the existing CHD, pick new codecs, and it recompresses without needing the original disc.</dd>

      <dt>The DAT badge didn't appear on a file I'm sure is good.</dt>
      <dd>RVZ/WIA/GCZ and CHD both match on the reconstructed disc / header hash (not the container bytes), so recompressing doesn't break matching. A missing badge usually means the DATs aren't synced, the title isn't in the DAT, or the file is over <code>MATCH_MAX_FILE_SIZE</code> (which skips the full-disc reconstruction). It's about hash matching, not file health, so verify is the real proof your file is good.</dd>
    </dl>
  </article>

  <article class="panel">
    <h2 class="panel-title">Getting help and asking for features</h2>
    <p class="lead">
      If something's broken or you want a feature, the project lives on
      <a href={repoUrl} target="_blank" rel="noopener noreferrer">GitHub</a>.
      Open an issue. That's the place, not a fork in a comment somewhere.
    </p>
    <p>
      Before you file a bug, take a quick look through the
      <a href={issuesUrl} target="_blank" rel="noopener noreferrer">existing issues</a>
      in case someone already hit it. If not, a good report saves a round trip. Include:
    </p>
    <ul class="bullets">
      <li>What you did and what you expected, versus what actually happened.</li>
      <li>The app version, your OS, and the Docker tag you're running.</li>
      <li>The exact tool, mode, and codecs involved.</li>
      <li>Logs if you have them. Set <code>LOGLEVEL=DEBUG</code> and grab the output around the failure.</li>
    </ul>
    <p>
      Feature requests are welcome and the same rule applies: describe the use case, not
      just the feature. "I want X" is fine, but "I'm trying to do Y and X would make it
      possible" is far more likely to get built, because it explains the problem behind
      the ask.
    </p>
    <p>
      This is a fork of MarcTV's original CHD Converter with a web UI bolted on, plus
      Dolphin, 3DS, and Switch support. It's maintained in spare time, so be patient and be kind.
      A clear, reproducible report gets fixed a lot faster than a vague one.
    </p>
  </article>
</section>

<style>
  .view { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); max-width: var(--container-max); margin: 0 auto; width: 100%; min-width: 0; }
  .header h1 { margin: 0; font-size: var(--text-2xl); font-weight: var(--weight-semibold); color: var(--text-1); }
  .hint { color: var(--text-2); margin-top: var(--space-1); }

  .panel { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--space-4); box-shadow: var(--elev-1); }
  .panel-title { margin: 0 0 var(--space-3); font-size: var(--text-base); font-weight: var(--weight-semibold); color: var(--text-1); text-transform: uppercase; letter-spacing: 0.05em; }
  .sub-title { margin: var(--space-4) 0 var(--space-2); font-size: var(--text-sm); font-weight: var(--weight-semibold); color: var(--text-1); }

  .panel p { color: var(--text-2); margin: 0 0 var(--space-2); line-height: 1.6; max-width: 72ch; }
  .panel p:last-child { margin-bottom: 0; }
  .panel strong { color: var(--text-1); font-weight: var(--weight-semibold); }
  .lead { color: var(--text-1); }

  code { font-family: var(--font-mono); font-size: var(--text-xs); color: var(--text-1); background: var(--surface-2); padding: 1px 6px; border-radius: var(--radius-sm); }

  .tool-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .tool { display: flex; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--surface-2); }
  .glyph { flex: none; display: inline-flex; align-items: center; justify-content: center; min-width: 44px; height: 28px; padding: 0 var(--space-2); border-radius: var(--radius-sm); background: var(--surface-1); border: 1px solid var(--border-subtle); color: var(--text-1); font-family: var(--font-mono); font-size: var(--text-xs); font-weight: var(--weight-semibold); }
  .tool-body { min-width: 0; }
  .tool-name { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-semibold); margin-bottom: 2px; }
  .tool-blurb { margin: 0 0 var(--space-2); }
  .io { display: inline-block; white-space: pre-wrap; }

  .mode-tool + .mode-tool { margin-top: var(--space-4); }
  .mode-tool-name { margin: 0 0 var(--space-2); font-size: var(--text-sm); font-weight: var(--weight-semibold); color: var(--text-1); }
  .mode-table, .codec-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  .mode-table th[scope="row"], .codec-table th[scope="row"] { text-align: left; vertical-align: top; white-space: nowrap; color: var(--text-1); font-weight: var(--weight-medium); padding: var(--space-2) var(--space-3) var(--space-2) 0; }
  .mode-table td, .codec-table td { vertical-align: top; color: var(--text-2); padding: var(--space-2) 0; line-height: 1.5; }
  .mode-table td:not(:last-child), .codec-table td:not(:last-child) { padding-right: var(--space-3); }
  .mode-table tbody tr, .codec-table tbody tr { border-top: 1px solid var(--border-subtle); }
  .mode-table .note { min-width: 28ch; }
  .mode-table thead th, .codec-table thead th { text-align: left; color: var(--text-3); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em; font-weight: var(--weight-medium); padding-bottom: var(--space-1); }
  .codec-table { margin: var(--space-2) 0 var(--space-3); }

  .callout { color: var(--text-1); background: var(--surface-2); border-left: 3px solid var(--accent); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); margin: var(--space-2) 0; }

  .trouble, .qa { margin: 0; }
  .trouble dt, .qa dt { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-semibold); margin-top: var(--space-3); }
  .trouble dt:first-child, .qa dt:first-child { margin-top: 0; }
  .trouble dd, .qa dd { color: var(--text-2); margin: var(--space-1) 0 0; line-height: 1.6; max-width: 72ch; }

  .panel a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
  .panel a:hover { color: var(--accent-strong, var(--accent)); }

  .bullets { margin: var(--space-2) 0; padding-left: var(--space-4); max-width: 72ch; }
  .bullets li { color: var(--text-2); line-height: 1.6; margin-bottom: var(--space-1); }
</style>
