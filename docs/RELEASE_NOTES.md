# Release Notes

## 4.4.2 (unreleased)

### Fixed

- **The Convert and Jobs panel no longer collapses into a narrow strip on
  mid-width screens.** Between 900 and 1279 CSS pixels wide — the range a 4K
  monitor at 300% display scaling, a laptop at high zoom, or a half-snapped
  window lands in — the workspace switches to a two-column layout where the
  convert/jobs panel is meant to stack full-width below the file list. It was
  never told to span the row, so the grid auto-placed it into the 220px
  volumes-rail column: a squeezed sliver of a panel next to a huge empty cell,
  with the page scrolling forever. The panel now spans the full row in that
  range, and a laptop-width (1100px) screenshot was added to the automated set
  so the in-between breakpoint stays covered.
- **A library scan or DAT match no longer strands one stuck process per file.**
  When a hash helper had to be abandoned — it survived `SIGKILL` and kept
  running — matching reported it exactly like a disc with no embedded hash, so a
  scan recorded a miss and opened the next file on the same unresponsive share.
  Each step left another live process behind, invisible to the app because it had
  already stopped tracking them. Every pass that walks your library now **stops**
  on the first one and says why: the metadata, disc-ID and DAT-match phases of a
  scan, the background match job, and `/dat/match-batch`. A single-file match
  reports it rather than answering with a silent "no match".
- **A stuck read can no longer be mistaken for "this disc has no ID".** If
  reading a CHD's GAME tag had to be abandoned, the answer came back identical to
  a disc that genuinely has no tag — so the tagger wrote a new tag to a file the
  stuck reader was still holding, and the scan fell through to reading the whole
  disc from the same unresponsive share. Both now stop instead, and the skip is
  reported rather than buried. A DAT match likewise no longer starts a full-disc
  verify on a share that already failed to answer a simple size check.
- **Reading CHD metadata can no longer hang a scan.** `chdman info` and the
  GAME/NAME tag helpers spawned their own processes and, after giving up on a
  stuck one, waited for it with no time limit — so a scan could stop partway
  through with no error and no way to tell it from a slow one. Those calls now go
  through the same bounded machinery as everything else.

- **Every tool now reports real status, not just a spinning clock.** Dolphin
  conversions showed `Converting... (1134s)` against a progress bar pinned at
  0%, because dolphin-tool draws its progress bar only to a terminal and goes
  silent the moment we capture its output to a pipe. With no signal at all,
  there was no way to tell a conversion crawling against a busy array from one
  that had stopped dead — the two looked identical, indefinitely. Progress
  reporting is now shared infrastructure rather than something each tool wires
  up for itself: a tool's own percentages are used whenever it prints any, and
  when it doesn't, status falls back automatically to the output file growing on
  disk, reporting **MB written and a MB/min rate**. A slow job now visibly reads
  as slow — bytes climbing, rate collapsing — instead of as a hang. This applies
  to all nine tools, including the six that previously had no fallback at all.
- **A slow conversion is no longer mistaken for a stalled one.** The stall
  watchdog scales with input size (about 19 minutes without output growth for a
  4.4 GB image). Tools with no parseable progress fed it nothing but the output
  file, so a genuinely slow conversion could be killed as if it had wedged. The
  same size-growth signal now feeds the watchdog for every tool.
- **One wedged conversion can no longer freeze the whole queue.** A process
  blocked on unresponsive storage survives both `SIGTERM` and `SIGKILL`, and the
  code waited for it to exit with no time limit. Since jobs run serially by
  default, that single job never failed, never cancelled — Cancel sat on
  *Cancelling...* forever — and every job behind it waited with it. All waits on
  a subprocess are now bounded and escalate to giving up: the stuck job fails
  with an explanation and the queue carries on.
- **Cleaning up after a failed conversion can no longer freeze the queue
  either.** Bounding every wait on the conversion itself left one unbounded step
  right behind it: once a job failed, each tool deleted the half-written file it
  left behind — on the same storage the conversion had just died on. A delete on
  an unresponsive mount blocks in the kernel and cannot be interrupted, so the
  worst case still ended the same way. The stuck process was abandoned in
  seconds, and then the job hung forever tidying up, with every queued job
  behind it. Partial-output cleanup is now bounded like everything else and
  gives up out loud: the job finishes as failed, the log names the file that
  could not be removed and says the storage is likely unresponsive, and the
  queue moves on. A leftover partial file is a much smaller problem than a
  frozen queue, and deleting it is a one-line fix an operator can actually
  perform. Affects every tool that writes its output in place — maxcso, z3ds,
  nkit2iso, nsz, romz, makeps3iso, JWUDTool — and the temporary directories
  chained conversions work in.
- **A wedged job now says so in the log.** Stuck-queue detection only fired when
  jobs were queued and *none* were processing, so a job frozen mid-conversion —
  exactly the case above — was the one state it could not see, and the only
  record of it was logged at debug level where nobody would find it. It is now a
  warning at the default log level.
- **Verification can no longer run forever, and Cancel now stops it.** The
  previous round bounded every wait in the *conversion* path; verification had
  the same two gaps. There was no verify timeout at all out of the box, so a
  verify that never returned never ended — and since jobs run serially by
  default, it froze every job behind it, reachable through the commonly used
  **Delete sources after verification**. Cancel didn't help: the verify stage
  never received the cancellation, so the UI sat on *Cancelling...* while the
  verifier kept reading. Verification is now bounded by default and genuinely
  cancellable, for every tool:
  - The bound scales with the file, because verify reads all of it: 30 minutes
    plus 10 minutes per GiB, capped at 24 hours
    (`COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` / `…_PER_GIB` / `…_CAP`). That is
    deliberately loose — the per-GiB allowance alone works out to about
    1.7 MB/s, on top of the half-hour baseline and under the 24-hour cap — so a
    slow verify on tired storage still finishes; the point is that a wedged one
    ends. Tools that
    stream progress (chdman, Dolphin) also get a 10-minute no-output stall bound
    (`COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT`), which catches a hang much
    sooner. The two are independent knobs: setting the first to `0` switches off
    the overall bound but leaves the stall bound running, so zero both for the
    old fully unbounded behaviour.
  - Cancel now reaches the verify stage, and the job stops waiting on it at
    once instead of sitting on *Cancelling...*. A verifier that runs as a
    subprocess is terminated and reaped; a pure-Python one (the Wii U container
    walk, the PS3 title readback) stops at its next checkpoint, and a blocking
    read already in flight is abandoned rather than killed — the operating
    system offers no way to interrupt one. That holds at every point the verify
    can be waiting: while the bound is still being sized from the file, and
    while a verifier that has stopped printing is being given a moment to exit
    on its own. Either way the job is reported as a
    **cancelled job, not a failed verification**: it reached no verdict, so the
    source is never deleted on the strength of it.
  - A **Verify all** run now stops if one file's verifier cannot be stopped —
    for any tool, whether the stop was a cancel or a timeout, and whether the
    verifier was a subprocess or a plain read that is still blocked. Every
    remaining file is on the same storage, so continuing used to leave one
    unkillable verifier (or one blocked thread) behind per file. The stop
    carries across tool groups too: a mixed selection no longer starts the next
    tool against storage that just proved it can wedge one.
  - Selecting a folder full of files on a mount that has gone away now fails
    fast, with one message, instead of spending ten seconds per file finding out
    the same thing — which used to delay the whole run by minutes and could
    leave enough stuck threads behind to make verification fail on healthy
    volumes too. (This one needs no wedged verifier — an unreachable mount is
    enough.)
  - Closing the browser tab mid-verify, or leaving it on a stalled connection,
    no longer costs the verification slot. It used to be handed back only when
    the page came back for the next event, so a client that went quiet at the
    wrong moment left every later verification refused as at-capacity until a
    restart. The slot now follows the verifier: it is returned when the check
    stops, and a **Verify all** run holds it for the file being checked rather
    than for the whole list.
  - The bound applies to verification started from the Verify buttons too, not
    just to delete-on-verify jobs. Those run through the same verifiers and hold
    the same verification lane, so a wedged one used to keep that lane occupied
    for good.
- **A job wedged in verify is now visible in the log.** The stalled-job warning
  used to skip the verify phase outright, to avoid calling a legitimately long
  checksum stalled — which meant a job genuinely stuck *in* verify logged
  nothing. It now gets its own line, timed from when verification started.

The counting half of the fix 4.4.1 started. 4.4.1 stopped a deep queue from
*deleting* your job history; this one stops the retention cap from *lying* about
how much work finished, which is what you notice on a run of thousands.

### Fixed

- **The Completed count no longer stops at `MAX_JOB_HISTORY`.** Finish more than
  500 conversions in a run and the Jobs panel kept reading *Completed 500* while
  the queue drained thousands — the same for Failed, and for the dashboard's
  Done / Failed tiles. It looks like a pagination bug and isn't: 500 is
  `MAX_JOB_HISTORY`, not a page size. Every one of those counts was derived from
  the list of jobs the backend still holds, and that list is capped — past 500
  finished jobs the oldest are deleted — so no count taken from it could ever
  exceed the cap, however much work finished. The backend now keeps a tally of
  what the cap evicts, and the UI reports retained **plus** evicted: the real
  total. Queue counts were never affected; pruning only ever touches finished
  jobs, so the live list was already complete.
- **The Completed and Failed tabs say when the list is shorter than the count.**
  Those older rows are genuinely gone, not hidden on another page, so the panel
  now explains the gap instead of letting the badge and the pager disagree in
  silence: *"Showing the 500 most recent of 1,153. Older finished jobs are
  dropped once history passes 500."* A tab whose jobs have **all** aged out —
  every retained slot taken by newer finished jobs — says that too, rather than
  the old *"No completed jobs yet"* sitting under a non-zero badge. Raise
  `MAX_JOB_HISTORY` if you want more of them listed rather than just counted;
  the cost is memory, roughly linear in jobs retained.
- **Clear reports what it actually cleared.** Past the cap it counted only the
  rows it could still see, so clearing a 1,153-job run confirmed *"Remove
  1,153?"* and then reported *"Removed 500."* It now reports the full number,
  and still zeroes everything — evicted tally included, so a badge can never
  outlive the list it came from.
- **`Show metadata jobs` is honoured in the new totals.** The tally is keyed by
  mode as well as status, so pruned `metadata_scan` and `dat_match` history
  can't inflate a badge while its rows stay hidden. Jobs you dismiss by hand are
  likewise never counted as evicted: that is history you dropped, not history
  the cap took.

### Added

- **`GET /api/jobs/history-overflow`, and a `history` event on
  `/api/jobs/events`.** Both report the same thing — how many finished jobs the
  cap has already evicted, broken down by status and mode, alongside
  `max_job_history` itself. The stream pushes it on connect and whenever the
  totals change; the endpoint is the hydration read and the fallback for a
  client that is polling rather than streaming. Anything counting finished jobs
  through the API needs this: `/api/jobs` can only ever return what is retained.
  Additive on both sides — a client that doesn't listen for `history` drops it
  silently, exactly as it does `snapshot`.
- **A cursor for reconciling the two halves.** The endpoint takes `since` (a
  sequence from a previous reply) and `generation` (the backend process that
  minted it), and answers with the ids evicted since that point, so a client can
  drop rows the cap deleted in the gap between reading `/api/jobs` and reading
  the totals — otherwise those jobs get counted twice, once as a row and once in
  the tally. `cursor_expired` says the reply can't be complete and the client
  should re-read the job list instead of trusting what it holds. The contract,
  and the invariants a client has to preserve, are written up in
  `docs/DESIGN_tool_plugin_architecture.md` §3.3.7.

## 4.4.1 (2026-08-15)

A follow-up fix for the batch sizes 4.4.0 made easy to queue.

### Fixed

- **A deep queue no longer wipes your job history mid-run.** `MAX_JOB_HISTORY`
  (default 500) caps how many *finished* jobs are kept, but it was gating on the
  **total** job count — pending work included. Queue more than 500 files at once,
  which 4.4.0's cross-page Select all makes a single click, and the total sat
  permanently over the cap: every sweep then deleted every completed, failed and
  cancelled job it could find and still couldn't get under it, because the rest
  were queued and not removable. The Completed and Failed tabs emptied
  continuously while the batch ran, so a finished 2,000-file conversion left no
  record of which files had failed. The cap now counts terminal jobs only.
  Queued and processing jobs were never at risk of deletion — they simply aren't
  removable — but they were being *counted*, and that is what evicted the
  history. Nothing else changes: history past the cap is still trimmed oldest
  first, and the just-finished job is still preserved. `MAX_QUEUE_DEPTH`, the
  opt-in backpressure ceiling on queued+processing jobs, is untouched and still
  defaults to `0` (disabled).

## 4.4.0 (2026-08-15)

A file-list release for large libraries. Selecting a whole platform folder no
longer means ticking the header checkbox once per page — 2,000+ discs spread over
80-odd pages is a single click — and how many rows a page shows is finally yours
to set. Both work under every tool and mode, inside archives, and across **Search
all** results. Sections are newest-first.

### Added

- **Select all files across every page, not just the visible one.** The header
  checkbox has always covered only the current page, so a folder of 2,000+ discs
  meant ticking it once on each of 80-odd pages before a batch could be queued.
  Now, as soon as anything is selected in a view that spans more than one page, a
  banner appears under the selection bar offering the whole thing —
  *"Select all 2,140 items in this folder"* — in a single click. Shift-clicking
  the header checkbox does the same without going through the banner, mirroring
  the shift-click range select on the rows. Once everything is selected the
  banner confirms the scope (*"All 2,140 in this folder are selected, across all
  43 pages"*) and offers **Deselect all**; the existing **Clear** still drops the
  whole selection.

  It is scoped to what you are actually looking at, and it works everywhere the
  file list does: any tool, any mode, any file type. It selects exactly the rows
  the current mode accepts as input — files under a file mode, convertible
  folders under makeps3iso `folder_to_iso`, archives under `romz_extract` — and
  it respects the active extension filter, so with `.iso` picked in a mixed
  folder the offer reads *"Select all 2,100 `.iso` items"* and leaves the rest
  alone. It works the same inside an archive view and in the recursive
  **Search all** results (*"…in these search results"*), and it only appears when
  there is genuinely something off-page to gain — a filter that narrows the list
  down to one page shows no banner at all.

  No extra loading is involved: pagination was already client-side slicing of the
  listing the browser holds, so "all pages" is the set already in hand.

- **Rows per page is now yours to set.** A **Rows** picker in the file list
  footer offers 25 / 50 / 100 / 250 / 500 (was a fixed 50), so a big folder can
  be 5 pages instead of 43. The choice is remembered across reloads, and it sits
  outside the pager — which hides itself at a single page — so you can always get
  back to a smaller size after picking one big enough to collapse the pagination.

  Changing it keeps your place rather than snapping to page 1: the rows you were
  looking at stay on screen (50 → 100 on page 8 lands on page 4, still showing
  the same files). Selection is keyed by path, not by page, so re-paginating
  mid-selection — including after a cross-page Select all — keeps every ticked
  row.

  The list is deliberately capped at 500 rather than offering an unlimited "All":
  every row on the visible page gets hydrated (archive summaries, CHD metadata,
  and a background DAT hash per file when DATs are loaded), so page size directly
  bounds that work — turning a 2,000-file folder into a single page would kick
  off a hash of the whole folder just by looking at it. Selecting everything no
  longer needs a giant page anyway, now that Select all spans pages.

### Fixed

- **Shift-click no longer breaks after the page shrinks.** The shift-click range
  anchor was an index into the page you were on, but nothing kept it inside the
  page actually on screen. Walk to a shorter last page (or narrow the extension
  filter, or re-sort) and the next shift-click ran off the end of the row list and
  threw, swallowing the click. The anchor is now clamped to the current page, so
  the range simply stops at its end. Changing rows-per-page additionally starts a
  fresh range, since repagination re-cuts the page and extending from a row picked
  under a different layout is surprising even when it happens to be in bounds.

## 4.3.0 (2026-08-09)

Two new tools take the roster to nine, and the tech-debt epic lands. **NKit → ISO**
(`nkit2iso`) restores an NKit-shrunk GameCube or Wii disc to a playable `.iso`,
with a one-step `nkit_to_rvz` pipeline and a synthesized NKit info endpoint; and
**Wii U** (`jwud`) converts `.wud` ↔ `.wux`, exploiting the fact that every Wii U
image is the same 25 GB however much of the disc a game actually uses. Alongside
them: opt-in token authentication for the networked UI and `/api` routes, README
screenshots that regenerate themselves in CI, and the bulk of the #177 tech-debt
epic — idempotency, determinism, and modularity hardening (issues #184, #183, #186
and #179) that turned several per-tool branches into plugin hooks, made extension
matching compound-aware, and put every tool on one shared subprocess loop.
Sections are newest-first.

### Added

- **Wii U support: a new ninth tool, `jwud` (JWUDTool), converting `.wud` ↔
  `.wux`.** Every Wii U disc image is exactly 25,025,314,816 bytes regardless of
  how much of the disc a game uses, so the padding compresses dramatically: WUX
  deduplicates repeated 32 KiB sectors and stores each distinct one once, and
  Cemu reads the result directly. Two modes, `jwud_compress` and
  `jwud_decompress`, both lossless, reversible, and with nothing to configure.

  **No keys.** This only repacks the image, it never decrypts it — a `.wux`
  carries exactly the same encrypted content as the `.wud` it came from, so no
  common key or title key is involved and none ships with the app. (JWUDTool's
  decrypt/extract features do need your own keys; they are out of scope here.)

  Every conversion is verified against its source: JWUDTool's own pass re-reads
  both images and compares them byte for byte before the job reports success, so
  a compress job takes about twice as long as writing the output alone and a
  mismatch fails the job instead of leaving a bad image in place. The progress
  bar covers both phases (conversion 1–50 %, verification 51–99 %).

  The `/api/jwud-verify` endpoints check a `.wux` structurally — header magic and
  fields, then every sector-index entry against the file's real length — which
  catches a truncated copy or a corrupt index table, and is as deep as the format
  allows since WUX stores no content checksums. Both header size fields are
  pinned to the format's only real values (a 32 KiB sector and a whole disc
  image) rather than merely sanity-checked, since the index table's size scales
  inversely with the declared sector size — so no file, however crafted, can
  dictate how much work a verification does. Only `.wux` is verifiable, so
  delete-on-verify is offered on compress but not on decompress. `/api/jwud-info`
  reads the same header, reporting the original image size and the ratio.

  **Split dumps work too.** wudump writes `game_part1.wud` … `game_part12.wud`
  and JWUDTool joins them when you pick part 1. Only part 1 is offered as a
  source; the rest stay listed (so you can see and delete them) but aren't
  convertible alone, since a part is not a disc image. A *complete* set's
  product is named after the disc (`game.wux`, not `game_part1.wux`), and
  delete-on-verify takes the whole set instead of orphaning eleven parts. A
  half-copied set, one with a gap, an orphan part and an archived part all keep
  their own name — none of them can produce the disc, so none of them may claim
  its output and risk overwriting an unrelated finished image. Output detection
  gained a matching `from_archive` flag (`ToolPlugin.detect_output`), so an
  archive member is never mistaken for a real file with real neighbours; the
  pre-existing tools ignore it. "Complete" is checked part by part, not just as
  a total: a short part balanced by a long one sums to a whole image but is
  still unjoinable, because the reader computes each part's offset from its
  index. A set whose sibling parts are symlinks, or resolve outside the
  configured volumes, is refused — the converter opens those files itself, so
  the only place to stop it is before it runs. Checked twice, like the PS3
  folder walk it mirrors: once when the job is queued (`plan_job`) and again in
  the worker after the job takes its locks, since a queued set's part can be
  swapped in between. The re-check runs before any existing output is cleared,
  so a rejection on an overwrite job leaves your previous file intact. And
  because `.wud` is a produced output, the DAT-match scan would otherwise SHA1
  all twelve 2 GiB parts of every set; a new `ToolPlugin.scannable_path` hook
  (default `True`) drops them from the walk.

  **Delete-on-verify is guarded per job (`delete_on_verify_is_safe`).** A third
  input-side hook, because the verification toggle below would otherwise create
  a footgun: jwud's `verify()` walks the WUX container's structure (the format
  has no content checksums), so what justifies deleting a 25 GB source is
  JWUDTool's byte-for-byte comparison during the conversion — and `-noVerify`
  turns that off. `ModeSpec.supports_delete_on_verify` only knows about the
  mode, so the plugin now answers the per-job question too (default `True`, so
  no other tool changes) and the shared request validator rejects the
  combination for single and batch alike.

  The set is protected end to end: `source_companions` also feeds
  `JobManager._track_candidate_paths`, so renaming or deleting part 5 while the
  conversion is running is refused like any other in-use path (that helper's
  hard-coded `.cue`/`.gdi` gate is now registry-driven for the non-track case).

  **The verification pass is now a choice.** It stays on by default, but the
  tool picker's dropdown — where other tools put their codec, since WUX has none
  — offers **Skip verification**, which passes `-noVerify` and roughly halves the
  runtime.

  Both directions also accept a member straight out of a ZIP/7z/RAR. New env var:
  `JWUDTOOL_PATH` (default `/usr/local/bin/jwudtool`). JWUDTool is a Java program,
  so the image now installs a headless JRE plus the pinned, SHA256-checked
  upstream release jar behind a small launcher; the jar is architecture-
  independent, so both `linux/amd64` and `linux/arm64` are covered. The tool
  hides itself in the UI (via the `is_ready()` hook) when no launcher is present,
  which is what a local checkout without Java sees.

- **Two more shared paths that assumed a source is one file became plugin hooks
  (`converts_path`, `source_companions`).** A split Wii U dump is one disc image
  over twelve files, which broke both: the listing offered all twelve as sources
  (eleven of which can only fail), and delete-on-verify removed the part the job
  named while orphaning the rest — about 23 GB of silent leftovers.

  `ToolPlugin.converts_path(path)` is the input-side mirror of `verifies_path`
  (default: the plain `input_extensions` match, so the seven existing tools are
  unchanged), read by `routes/files.py` into the same tool-neutral
  `FileEntry.convertible_by`. `ToolPlugin.source_companions(path)` is the
  input-side mirror of `companion_outputs` (default `[]`), read by
  `utils/delete_plan.build_delete_plan`. The registry gained
  `tools_converting_path()` and `source_companions()` alongside the existing
  verify-side pair. `.cue`/`.gdi` track files still go through delete_plan's own
  content parser rather than the new hook — folding those in would move their
  unsafe-reference handling, so it is left for its own change.
- **New tool: NKit → ISO (`nkit2iso`).** Restores an NKit-shrunk GameCube or Wii
  disc image (`.nkit.iso`, `.nkit.gcz`) back to a plain, full-size `.iso`, using
  [nkit2iso](https://github.com/DonMikone/nkit2iso) (MIT, a static Go binary
  built into the image for `linux/amd64` and `linux/arm64`). One mode,
  `nkit_restore`, with no settings: NKit drops everything a program can recreate
  (junk padding, gaps, all-junk files, and for Wii the AES encryption and H0–H3
  hash tree), and the restore rebuilds all of it, then CRC32-checks the result
  against the value stored in the NKit header — so a completed job is bit-exact.
  Archive members work too: an NKit image inside a `.zip`/`.7z`/`.rar` converts
  in place.

  This is a one-way tool — Compressatorium never *writes* NKit — so there is no
  compress direction. It also registers **no info or verify routes**: nkit2iso
  has no such subcommand, integrity is the header CRC32 checked inline during
  the restore, and for the same reason delete-on-verify is not offered.

  A Wii image whose update partition was removed at shrink time cannot come back
  byte-exact — that data simply is not in the file. By default
  (`NKIT2ISO_RECOVERY=none`) the region is zero-filled, exactly as official NKit
  does without its recovery files, and the job's final message says plainly that
  the ISO is playable but **not** redump-verifiable. Set
  `NKIT2ISO_RECOVERY=download` to have nkit2iso fetch the publicly archived
  recovery partition and splice it in for a bit-exact restore; that is the only
  path on which the tool touches the network, which is why it is opt-in. The
  tool's own interactive `ask` mode is never used — a job worker has no terminal
  to answer it on.

  New env vars: `NKIT2ISO_PATH`, `NKIT2ISO_RECOVERY`, plus the usual
  `COMPRESSATORIUM_NKIT2ISO_NICE` / `_IOPRIO_CLASS` / `_IOPRIO_LEVEL` overrides.

- **NKit info (`GET /api/nkit-info`).** nkit2iso has no `info` subcommand, so
  this reads the NKit/disc header directly — the same one the binary reads —
  and reports the console (GameCube or Wii), game ID and title, disc number and
  version, the size the restored ISO will occupy, the CRC32 the restore is
  checked against, and the shrunk-to-original ratio. Worth a look before
  spending a restore on a multi-gigabyte image. A `.nkit.gcz` is handled by
  inflating a single zlib block to reach the header — with every header-supplied
  length bounded against the file's real size first, so a crafted container
  can't drive a multi-gigabyte read. A file named `.nkit.iso` whose bytes carry
  no NKit marker reports 422, not 500.

- **New one-step pipeline: `nkit_to_rvz` (NKit → ISO → RVZ).** The second
  `ChainSpec`, alongside `cso_to_chd`. NKit is a shrink format no emulator
  reads, so the restored ISO is nearly always a stepping stone to RVZ — which
  Dolphin reads natively and which compresses better than NKit anyway. Chaining
  keeps the full-size ISO in a scratch directory instead of the library. Neither
  NKit mode offers delete-on-verify: dolphin's RVZ verify is *structural* (it
  confirms the container, not that the disc inside matches the original), and a
  Wii image restored without its update partition yields a cleanly verifying RVZ
  that is not bit-exact — deleting the source on that evidence would destroy the
  only file a later recovery-enabled restore could use. The final RVZ uses
  dolphin's defaults; run the
  two steps as separate jobs to pick a codec and level (the RVZ codec guards in
  `_validate_request_compression` key on `spec.tool_id == "dolphin"`, and making
  them chain-aware would mean reading `ChainSpec.steps` from the route, which
  the design keeps exclusive to `ChainTool`).

- **Two per-tool branches on shared paths became plugin hooks
  (`overwrite_targets`, `is_ready`).** Both were single-tool special cases
  sitting on code every tool runs through, so a *second* tool of the same shape
  would have silently inherited the first one's behavior.

  `JobManager._clear_existing_output` branched on `input_kind == DIRECTORY` and
  called `makeps3iso_service.remove_outputs` directly, so any future
  folder-input tool would have had its outputs swept by makeps3iso's split-part
  logic — the wrong files deleted and its own companions left behind, with no
  error. The sweep now asks the owning plugin for `ToolPlugin.overwrite_targets()`
  and runs one validate-then-unlink pass for every mode, file or directory.
  `BaseTool` defaults it to the primary plus `companion_outputs`;
  `MakePs3IsoTool` overrides it with the base plus every numbered part, sharing
  one `MakePs3IsoService.output_artifacts` enumerator with its failure cleanup
  so the two can't drift. The blocking stat/unlink pass also moved off the event
  loop for file jobs, matching what directory jobs already did.

  `GET /api/tools` computed `nsz_service.keys_available()` and branched on
  `tool.id == "nsz"`, so only Switch could ever be hidden; a second tool needing
  operator-supplied keys would have been advertised in the UI while unable to
  run. Availability now awaits each plugin's `ToolPlugin.is_ready()` (default
  `True`; `NszTool` overrides it with the threadpooled keys probe).

  A third instance of the same pattern, in `ChainTool`: `detect_output`
  hard-coded a `.chd` candidate and `_final_tool()` read `self.modes[0]`, so a
  second `ChainSpec` would have badged the first chain's product and delegated
  verify/info to the first chain's final tool. Both now resolve per spec —
  `detect_output` takes the candidate extension from whichever mode accepts the
  input, and verify/info resolve the owning spec by output extension (they get a
  path and no mode).

  No API change, and no change to how the makeps3iso, nsz and `cso_to_chd`
  conversions themselves run. Existing behavior is unchanged **except** for two
  deliberate refinements to the overwrite path, made while the sweep was being
  unified:

  - **A dangling symlink on an output path is now removed rather than ignored.**
    `os.path.exists`/`isfile` both follow symlinks and report `False` for a
    dangling one, so the old file-job check skipped it silently and the
    converter could write *through* the link — landing the output wherever it
    pointed, outside the validated volume. The sweep now tests with
    `lexists`/`islink` (unlinking a symlink removes the link, never its target)
    and still rejects any other non-file occupant before removing anything.
  - **An authorized overwrite always clears the output's verification record**,
    even when the sweep removed nothing. If something else deleted the prior
    output while the job sat queued, the record would otherwise survive and
    report the freshly built, unverified artifact as verified.

  Tests: new `tests/test_tool_seams_generalized.py` pins each hook against a
  *synthetic* second tool (a fake directory tool, a gated non-nsz tool, a
  `.foo`→`.bar` `ChainSpec`), which is what catches a regression back to an
  id/kind branch — asserting on the single existing instance passes either way.

- **Web UI/API authentication (opt-in).** Optional token authentication for the networked UI and `/api` routes, gated behind `COMPRESSATORIUM_ENABLE_AUTH` (default off). When enabled, operators can set `COMPRESSATORIUM_AUTH_TOKEN` (or legacy `CHD_AUTH_TOKEN`), or let the app generate a persistent token in `/config/auth_token`; `/health` remains unauthenticated for container health checks. The token is accepted only in request headers (HTTP Basic, `Authorization: Bearer`, or `X-Compressatorium-Token`) — never the URL query string — and state-changing requests are restricted to same-origin callers to prevent CSRF via cached browser credentials.

- **Automated Web UI screenshots.** The screenshots embedded in the README are
  now generated with [shot-scraper](https://shot-scraper.datasette.io/) instead
  of captured by hand. A new **Take screenshots** GitHub Actions workflow builds
  the UI, boots it against a throwaway fixture library, captures every surface in
  light and dark (definitions in `shots.yml`), optimises the PNGs with Oxipng,
  and commits them back to `docs/screenshots/`. It can also be run locally. See
  [`docs/SCREENSHOTS.md`](SCREENSHOTS.md) for the full setup. The stale,
  unreferenced `ui.png` / `mobile-*` / `tablet-*` captures were removed in favour
  of the regenerated `docs-desktop-view` / `docs-tablet-view` / `docs-mobile-view`
  set.

### Removed

- **Legacy per-tool `FileEntry` flags dropped (issue #186, site 6 — Phase 9
  cleanup).** The ~23 dual-maintained per-tool booleans/paths on `FileEntry`
  (`has_chd` / `has_rvz` / `*_ready` / `*_convertible` / `*_path` and the bare
  `convertible`) are removed. They were pure derivations of the registry-driven
  `convertible_by` / `outputs` / `verifiable_by` fields the frontend already
  reads, so the backend now emits only those: the `_legacy_output_fields` helper
  and the per-archive `has_chd` special-case are gone from `routes/files.py`
  (the per-archive "converted" badge continues to derive from the
  registry-driven `archive_has_output` count), and the last two frontend
  fallbacks (`FileRow` convertibility, `fileBrowser` archive-summary merge) now
  read the registry-driven fields exclusively. No wire-API change the UI relies
  on; the file/search JSON simply no longer carries the dead flags.

### Changed

- **Extension matching is now a suffix match, so a tool can declare a compound
  extension.** Every "does this tool handle this file?" check used to be
  `Path(name).suffix.lower() in declared`, re-typed at each site. That can only
  ever see one trailing component, which made a format like `.nkit.iso`
  undeclarable: its trailing component is the generic `.iso` that CHDMAN,
  Dolphin and maxcso already own, so the choice would have been over-claiming
  every ISO or hard-coding a content sniff.

  The check moved behind one shared helper,
  `utils.path_utils.match_extension(name, extensions)`, which returns the
  longest declared extension the name ends with. It is used by
  `registry.tools_for_input` / `tool_for_verify` /
  `tools_accepting_archive_member`, `BaseTool.verifies_path`, the archive-member
  gate, `routes/files.py`'s output detection, and the plan-time input gate in
  `routes/convert.py`. The frontend already matched with `endsWithAny`, so it
  needed no change.

  **No behavior change for any existing tool**: every other declaration is a
  single component, and `game.iso` ends with `.iso` and nothing else. Matching is
  per-tool, so an NKit image is claimed by `nkit` *and* by the generic-tail
  owners — the same way a raw `.iso` is already claimed by chdman, dolphin and
  cso — rather than displacing them. `tests/test_compound_extension_matching.py`
  pins both halves.

  Two consumers needed a matching tweak. On the frontend,
  `registry.toolsForSourcePath` / `infoToolsForPath` now rank claiming tools by
  **matched-extension length** instead of declaration order: the Info modal keeps
  the first `getInfo()` that returns, and Dolphin's header reader *succeeds* on an
  NKit image (it carries a genuine GC/Wii disc header), so without the ranking a
  `.nkit.iso` would have been described as an ordinary disc. And `ChainTool` now
  names its product from the **first** step's stem plus the chain's own
  `output_ext` rather than delegating to the last step's tool, which would have
  produced `Game.nkit.rvz`; the result is identical for `cso_to_chd`.

- **Chain steps can flag a caveat that survives to the terminal message.** A
  later step's messages replace earlier ones, so anything an earlier step needs
  the operator to *know* (nkit2iso restoring a Wii image without its update
  partition: playable, but not bit-exact) was lost when the chain reported a
  bare "Conversion complete". A step now marks such an update with
  `warning: True` and `ChainTool` carries those into its own final message;
  `job_manager` reads only `progress`/`message`, so the key is inert elsewhere.

- **`test_delete_on_verify_iff_output_is_verifiable` became
  `test_delete_on_verify_requires_a_verifiable_output`.** The old test asserted
  equivalence in both directions. The safety-critical direction still holds for
  every mode with no exceptions (delete ⇒ the output must be verifiable), but
  the converse is wrong: verification proves an output is *well-formed*, not
  that the conversion preserved the source. A mode whose pipeline can be lossy
  now declines the flag via an explicit, documented allow-list.

- **`ToolPlugin.expected_output_size(input_path, mode)`.** `ChainTool`'s
  disk-headroom preflight imported `services.maxcso.uncompressed_iso_size`
  directly — a single-tool special case on code every chain runs through, the
  same shape as the `overwrite_targets` / `is_ready` branches removed earlier, so
  a second chain starting with a different tool would silently fall back to a
  ratio. The preflight now asks the first step's plugin. `BaseTool` returns
  `None` (fall back to `ChainStep.output_ratio`); `MaxcsoTool` answers for
  `cso_decompress`, `Nkit2IsoTool` from the NKit header. Preflight-only, never a
  correctness input, so an unreadable file is `None` rather than an error.

- **"Adding a tool" guide brought back in sync with the code.**
  `docs/ADDING_PLATFORMS_AND_TOOLS.md` had drifted behind the Phase 7/9
  registry-driven cleanups and still told contributors to write code that no
  longer exists. Corrected throughout, and consistently across all four views of
  the same list (the §3 inventory, the §5 walkthrough, the §6 checklist and the
  §14 file map): listings are tool-neutral, so there are **no** `FileEntry`
  per-tool flags, no `routes/files.py` edit and no `FileRow.svelte` fallback to
  add; `plan_job` input validation is three data lines
  (`SkipReason` + `_SKIP_HTTP` + `_BAD_EXTENSION_REASON`), not an `if`-block;
  `fileIcon.js` takes one `TOOL_MEDIA` entry rather than extension lists; Help
  needs a `helpModes.js` blurb rather than a hand-written table row; and
  compression defaults are declared on the frontend descriptor
  (`defaultCompression` / `compressionLevelRange`) rather than branched on in
  `conversion.svelte.js`. Also documented the parts of the contract that had
  never been written up — `ModeSpec.companion_exts` / `input_kinds`,
  `ChainSpec`, `accepts_directory()`, `companion_outputs()`, the `split` convert
  kwarg, `BaseTool._basic_info_fields`, the full registry query surface, and the
  fact that info/verify routes are optional — plus the eighth (synthetic
  `ChainTool`) registration and its ordering constraint, the five-stage
  Dockerfile, `PYTHONPATH=app` on every test command, and the correct
  tool-id-vs-binary-name pairings (`cso`←maxcso, `romz`←7z). Fixed the internal
  contradiction about `tests/conftest.py` stubbing tool binaries (it does not;
  it is DB-only), filled the missing §7 (a "which existing tool should I copy?"
  decision table, so the section numbering no longer skips), and added the
  screenshot-refresh and doc-maintenance steps. The guide is now linked from
  `README.md`, `AGENTS.md` and `CLAUDE.md`, which previously pointed only at the
  design doc.

- **Frontend icon and Help lists now derive from the tool registry (issue #186,
  site 3).** `src/lib/util/fileIcon.js` builds its disc/game extension buckets
  from `registry.all()` plus a small tool→media map instead of a hand-typed
  extension list (so Switch dumps and loose `.7z`/`.zip` archives now get
  meaningful icons), and the Help view's mode-reference table is generated from
  the registry — one section per tool and mode-group — with only the human
  blurbs and multi-output display strings curated in the new
  `src/lib/tools/helpModes.js`. A Node-evaluated guard
  (`tests/test_frontend_registry_derives_186.py`) fails if either list drifts
  from the registry's tools or modes.

- **PS3 folder-to-ISO inputs are now recursively confined to configured volumes.**
  Directory-input planning rejects symlinks and non-regular filesystem entries,
  and the queued worker repeats the same safety walk after acquiring the source
  directory lock and before clearing any existing output, immediately before
  invoking `makeps3iso`. A symlinked source root is rejected even when hidden
  behind a trailing separator, a `.`/`./` component, or a `..`-cancelled
  symlinked ancestor. This prevents a crafted or mutated PS3 folder from
  causing the native packer to read and embed files outside the configured
  volume boundary, and keeps a safety rejection on an overwrite job from
  deleting the user's prior output.

- **Deterministic ordering across the registry, search, and runner (issue
  #183).** The `ToolRegistry` extension-union helpers (`convertible_extensions`,
  `archive_input_extensions`, `verify_extensions`, `output_extensions`,
  `scannable_extensions`) now return a **sorted `tuple`** instead of a
  `frozenset`, so any serialized extension list is byte-stable run-to-run.
  Recursive `/api/files/search` results are sorted to match the directory
  listing, the file browser keeps a stable order for rows with equal sort keys,
  and subprocess progress-line segmentation no longer depends on read-chunk
  boundaries. The cross-process FIFO job ticket is hardened against counter
  fallback/recovery collisions and now preserves legacy ticket files across a
  rolling restart. No wire-API change.
- **CHD embedded-hash matching is hardened against a metadata-cache TOCTOU.** When a
  CHD is matched against a DAT, its cached header/data SHA1 are now read and
  freshness-checked as a single observation (read the row, then re-stat), so a CHD
  rewritten mid-match can no longer yield hashes from the old file. On any staleness
  the match falls back to a file-level SHA1 as before.
- **Duplicate same-file/same-mode submissions keep an accurate in-progress count.**
  An optimistic placeholder is now cleared only when its real job first arrives, not
  on every later progress update, so submitting the same file in the same mode twice
  no longer briefly under-counts the queue.
- **Re-queuing a job whose verified output already exists is now a no-op (issue
  #184, site 1).** When a job starts and its target artifact is already on disk and
  provably the exact result of the current request, the pipeline recognizes the prior
  success and completes the job without re-spawning the converter — the existing
  verified file is neither re-extracted-from nor cleared. To make this safe rather
  than a source of surprising output, a delete-on-verify conversion now records a
  `produced_meta` snapshot alongside the verification (new nullable
  `verifications.produced_meta` JSON column, migration `0003`): the producing
  mode/compression/split plus stat fingerprints of the **complete** source set (a
  `.cue`/`.gdi` and its track files, not just the descriptor) and of the output. The
  fast path fires only when a record carries that snapshot **and** the current
  request's shaping settings, source-set fingerprint, and output fingerprint all
  match it — so a different compression, a changed `.bin` track, or a replaced/
  corrupted output all force a full re-convert. A plain `/info` verify records no
  snapshot and never qualifies; directory (folder→ISO) and delete-on-verify jobs
  always take the normal path (the latter so its guarded source deletion still runs).
- **All conversion tools now share one subprocess loop.** `z3ds`, `maxcso` and `nsz`
  previously hand-rolled their own ~150-line spawn / stall-timeout / cancel / progress
  loop; they now delegate to the shared `SubprocessRunner` like `chdman`/`dolphin`/
  `romz`/`makeps3iso`, so a cancel, stall, or exit-code fix lands in one place instead
  of drifting between three copies. Progress for these no-parseable-percent tools is
  still estimated from output-file growth, now via a shared `size_progress` seam, so
  the conversion bar is unchanged. Per-tool error parity is preserved too — when nsz
  exits cleanly but produces no file, the failure still reports the reason nsz printed
  (via a shared `require_output` seam) instead of a bare "no output" message. (issue
  #179)

### Fixed

- **Documented the Synology DSM `cpus:`/NanoCPUs startup failure (issue #248).**
  DSM's Docker implementation doesn't support CPU CFS bandwidth control, so the
  default compose files' `cpus:` limit makes the container fail to start with
  `NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler
  or the cgroup is not mounted`. The compose files, README, and
  `docs/DOCKER-COMPOSE.md`/`docs/DEPLOYMENT.md` now flag this next to the
  limit and document the fix: drop the `cpus:` lines (keep `memory:`), or pin
  cores with `cpuset: "0-1"` instead (not equivalent — it pins cores rather
  than capping CPU time). There's no compose-level way to auto-detect kernel
  CFS support, so this remains a documented manual step on affected hosts.

- **The delete-on-verify confirmation no longer crashes the view for archive
  sources.** Selecting two or more archives with **Delete sources after
  successful verification** enabled took down the whole workspace with "This
  view crashed / `each_key_duplicate`" before a single job was queued. The
  confirmation modal renders the plan's warnings and blocking reasons in keyed
  `{#each}` blocks keyed by the message text, and the backend emits a *fixed*
  string per item — one "Archive input detected; delete-on-verify will remove
  the entire archive" per archive source, one "multiple selections from the same
  archive" per offending member — so any multi-archive selection handed the list
  duplicate keys, which Svelte treats as a hard runtime error. Both lists now go
  through a shared `summarizeMessages` helper that collapses repeats to one line
  tagged with its count (`… (×12)`), which also reads better than the old wall
  of identical lines. Identity stays separate from presentation: each row is
  keyed by the raw message — unique because that is what the helper
  deduplicates on — while the count suffix appears only in the rendered text,
  so a message that itself ends in a `(×N)` suffix can't collide with a
  different message that repeated that many times. Present since the 4.0.0
  Svelte 5 rebuild and unchanged through 4.1.0 / 4.2.0.

- **Unsupported archive members return a 400, not a 500.** When an archive
  member with an extension the target tool doesn't accept (e.g. `switch.zip::bad.txt`
  for `nsz_compress`) was submitted, `plan_job` derived the output path from the
  member suffix *before* the input-extension gate ran, so the tool's output-path
  mapper raised an uncaught `ValueError` and the API answered HTTP 500. The
  per-tool extension check now also runs inside the archive branch, before any
  output-path derivation, so these inputs fail early with the intended per-tool
  skip reason (`NSZ_BAD_EXTENSION` and friends) and a controlled 400 response.

- **Verify streams no longer leak their child process on cancellation.** When an
  SSE verification client disconnects mid-stream (or the consuming task is
  otherwise cancelled), `chdman` and `dolphin-tool` `verify_stream` now wrap their
  read loop in a `try/finally` that terminates the still-running child and untracks
  its PID on every exit path. Previously a cancelled consumer left the native
  `chdman verify` / `dolphin-tool verify` subprocess running, slowly exhausting the
  process table and file descriptors. This brings both tools in line with the
  `nsz`/`maxcso`/`z3ds` verifiers, which already cleaned up in a `finally`.

## 4.2.0 (2026-06-13)

This release adds the first folder-input tool and the first cross-tool chain. PS3
ISO packs a decrypted PS3 folder into a `.iso` that RPCS3 mounts directly, with an
optional 4 GB split for FAT32 targets, and a new CSO mode converts a
`.cso`/`.zso`/`.dax` straight to `.chd` in one step. Sections are newest-first.

### PS3 decrypted folder → ISO (issue #98, Phase 2)

#### New

- **Build a PS3 `.iso` from a decrypted disc/JB folder.** A new **PS3 ISO**
  tool packages a folder you've already decrypted (a `PS3_GAME/` root, plus
  `PS3_DISC.SFB` for disc rips) into a single `.iso` that RPCS3 mounts directly,
  using **makeps3iso**. This is the first conversion whose input is a *folder*,
  not a file: browse to (or search for) a PS3 folder, switch to the PS3 ISO
  tool, and the folder becomes selectable like any source row — clicking its
  name still navigates in. The output is `<folder>.iso` next to the source (or
  in your chosen output directory). After a successful build the tool does a
  light PARAM.SFO `TITLE_ID` readback from the produced ISO and flags a mismatch
  in the job message. Decryption and keys are **out of scope** — this only
  repackages folders you already decrypted, and it never deletes the source
  folder (no delete-on-verify).
- **Optional 4 GB split for FAT32 targets.** A per-job **Split into 4 GB parts
  (FAT32)** toggle on the PS3 ISO tool runs makeps3iso with `-s`. A FAT32
  filesystem can't hold a single file larger than 4 GB, so most PS3 discs won't
  fit as one `.iso`; with the toggle on, an image over ~4 GB is written as
  `Game.iso.0`, `Game.iso.1`, … (RPCS3 mounts the `.0`). A title under 4 GB
  still produces a single `Game.iso`. The file browser **folds a split set into
  one entry** — it shows `Game.iso` with the combined size, not a row per chunk.
  Default off; ext4/NTFS/exFAT targets don't need it.
- **Concurrent jobs that touch a folder being packed don't corrupt it.** While a
  PS3 folder is being built into an ISO its whole subtree is locked. A per-file
  job that was **already queued** when the folder build starts and whose
  input/output lands inside that folder (only reachable when
  `MAX_CONCURRENT_JOBS > 1`) is **deferred — re-queued and retried** once the
  folder job finishes, instead of failing. A *new* request submitted while the
  folder is mid-build is treated like any other output collision: it's rejected
  up front (HTTP 409 for a single job, skipped in a batch). The default output
  (`<folder>.iso`) must also land inside a configured volume; if the folder is
  itself a volume root (so the sibling output would escape all volumes), the plan
  is rejected with a clear message asking for an in-volume output directory.

#### Fixed

- **File rename and delete now require an explicit confirmation header.**
  `POST /api/files/rename`, `DELETE /api/files/delete`, and
  `POST /api/files/delete-batch` are now guarded by the same
  `X-CHD-Action-Confirm` header used elsewhere for destructive actions — a
  shared `_require_action_confirmation` helper enforces `rename-file` for rename
  and `delete-file` for delete (`recursive-delete` still gates non-empty
  directory removal). The batch endpoint carries the same `delete-file` guard so
  the single-delete gate can't be bypassed by wrapping a path in a one-item
  batch. The Web UI sends these headers automatically; existing volume/path and
  job-lock checks are unchanged.

- **Rename no longer spin-loops when its output is inside a folder being packed.**
  A new `duplicate_action="rename"` job whose output lands in a locked PS3 subtree
  used to probe numbered names (`name_1`, `name_2`, …) in a tight loop. Every
  candidate sits in the same locked subtree and reads as locked, so the loop spun
  with no sleep until the folder build released the lock, burning a thread. It now
  detects the held subtree and rejects the request up front with the same
  locked-output error skip and overwrite already returned, so the job pipeline
  defers and retries it like any other blocked job (only reachable with
  `MAX_CONCURRENT_JOBS > 1`).

#### Internal

- The directory-input seam scaffolded in Phase 1 (`InputKind` +
  `ModeSpec.input_kinds`, `ToolPlugin.accepts_directory`,
  `registry.tools_for_directory`, the `services.ps3` detector) is now wired
  end-to-end: a `MakePs3IsoTool` + `makeps3iso` service, `ConversionJob.input_kind`
  threaded through `plan_job` / the job pipeline (a directory source skips the
  archive-extract path), convertible-directory annotation in the file listing
  **and** recursive search, frontend selection for convertible folders, and
  lock-manager **subtree protection**. The lock manager grows directory-lock
  APIs (`acquire_dir_lock` / `release_dir_lock` / `is_within_locked_dir` /
  `dir_lock_would_conflict`): a directory job takes an `fcntl` lock on the
  directory path itself and records the subtree in an in-memory `_dir_locks`
  set; subtree containment (whether a per-file output lands inside) is enforced
  **in-process** (the `MAX_CONCURRENT_JOBS` concurrency all lives in one
  process), so a child path contends just like an output collision. A job
  already queued when the lock is taken and found to fall inside the subtree is
  **deferred** — its slot is released and it is re-queued after a fixed short
  delay to run once the folder finishes, instead of failing (a `Set[asyncio.Task]`
  holds strong refs so the retry task can't be GC'd mid-sleep); a *new*
  submission whose output is already inside the locked subtree is rejected at
  plan time via `check_output_conflicts` like any collision. A rename / delete
  inside an in-flight folder is rejected by the files route's
  `_assert_path_not_in_use` (→ `find_active_job_for_path`), and delete-on-verify
  cleanup honors the same descendant check (`_is_path_in_use_by_other_job`).
  All subtree containment comparisons resolve symlinks (`os.path.realpath`) so a
  symlinked path into a locked folder can't slip past them, and
  `_plan_directory_job` validates the resolved output is not inside the source
  **and** is within a configured volume. `InputKind` moved
  from `services.tools.spec` to `models` (re-exported) so the job model can type
  the field without an import cycle. The makeps3iso binary (GPL-3.0,
  `bucanero/ps3iso-utils`, pinned commit) is built unmodified in the multi-stage
  `Dockerfile`, mirroring maxcso, and confirmed to build on linux/amd64 and
  linux/arm64 (plain portable C, no x86 asm). Tests: `tests/test_makeps3iso.py`,
  plus directory coverage in `tests/test_ps3_detector.py`.
- **Split (`-s`) plumbing.** A `split` bool joins the shared
  `ToolPlugin.convert` contract next to `compression` (file tools accept and
  ignore it) and threads request → `create_job`/`create_jobs_atomic` →
  `ConversionJob` → `convert()`. makeps3iso's split output is **size-dependent**
  (only >4 GB splits) and the part names aren't known until the build finishes,
  so the service discovers them by probing `<output>` then `<output>.0`/`.1`/…;
  the `TITLE_ID` readback targets the first part and failure cleanup removes the
  base **and** every numbered part. `_plan_directory_job` treats an existing
  split set as an output collision (and RENAME steps past it); the file listing
  folds a set into one `FileEntry` (`split_parts` count, `.0` as the path).
  Frontend: a `supportsSplit` mode flag gates the toggle, masked at submit like
  `deleteOnVerify`. Tests in `tests/test_makeps3iso.py` (argv flag placement,
  part detection, part-aware readback/cleanup, plan collision, listing fold).
  Split-set handling is hardened across the surfaces: the listing fold requires a
  **contiguous set of ≥2 parts** (a lone `Game.iso.0` is an interrupted split, so
  it stays a raw row with its single-row/bulk delete actions intact — bulk delete
  also hides when only folded split rows are selected); `MakePs3IsoTool.detect_output`
  probes the split set so a `>4 GB` build still badges its source folder (and the
  unverified-output delete warning still fires) when no bare `<folder>.iso` exists;
  the overwrite cleanup rejects a destination that already exists as a *directory*
  (its `os.remove` would no-op and makeps3iso would write *inside* it); and the
  worker rejects an existing split set as an output collision when overwrite wasn't
  authorized (the bare-path lock alone can't see `.0`/`.1`).

### CSO → CHD in one step (issue #98, Phase 1)

#### New

- **Convert a `.cso`/`.zso`/`.dax` straight to `.chd`.** A new
  **Convert to CHD** mode on the CSO tool runs the full PSP/PS2 pipeline —
  maxcso decompresses the compressed image to a temporary `.iso`, then chdman
  packages that to a `.chd` (PPSSPP / PCSX2 / RetroArch read the result). The
  intermediate `.iso` lives in a private temp dir and is cleaned up
  automatically; only the source and the final `.chd` remain. Delete-on-verify
  works end-to-end: the original `.cso` is removed only after the final `.chd`
  passes chdman verification. Disc-ID `GAME`/`NAME` tags are embedded into the
  CHD just like a direct Create DVD. The final CHD uses chdman's default
  compression (no codec picker yet for the chain).

#### Internal

- A general **cross-tool chaining seam** sits above the existing plugin
  registry: a `ChainSpec` (ordered `(tool_id, mode)` steps + declared
  intermediates + which step owns the final verify) and a synthetic `ChainTool`
  that orchestrates the component tools through the registry. Every existing
  registry consumer (convert route, job_manager, files route) works unchanged —
  `registry.for_mode("cso_to_chd")` resolves to the chain, verify/delete-on-verify
  delegate to the final step's tool. Progress is aggregated into one bar with
  normalized per-step weights (chd compression dominates cso decompression). A
  new shared `services.disk.ensure_headroom` preflights free space on the
  work-dir and output-dir volumes independently (a chain holds source +
  intermediate + partial final at once). Tests: `tests/test_chain_service.py`,
  `tests/test_disk_headroom.py`, plus chain coverage in `tests/test_tool_registry.py`.
- **Directory-input scaffolding** for the upcoming PS3 folder→iso work (issue
  #98, Phase 2): an `InputKind` enum + `ModeSpec.input_kinds`, a
  `ToolPlugin.accepts_directory` seam with `registry.tools_for_directory`, and a
  PS3 disc/JB-folder detector (`services.ps3`) that requires the `PS3_GAME/`
  layout and rejects bare installed-game folders. No makeps3iso binary, UI, or
  job plumbing yet. Tests: `tests/test_ps3_detector.py`.

## 4.1.0 (2026-06-04)

This is the stable 4.1.0 release. It supersedes the `4.1.0-beta-1` through
`4.1.0-beta-4` pre-releases; everything in those betas is rolled up here and no
further changes were made between `4.1.0-beta-4` and stable. The release rolls
up everything since 4.0 into one version: PSP / PS2 CSO·ZSO·DAX support
(maxcso), Handheld ROM archiving (GB / GBC / GBA / NDS ↔ `.7z`/`.zip`), full
Nintendo 3DS reversibility on a new upstream fork (decompression +
`.cxi`/`.3dsx`), faster archive-heavy directory listings, archive browsing made
global (so a packed ROM and an archived `.chd` are now visible inside their
container), CHD decompression straight out of an archive, registry-driven
library-scan / DAT-match across every format, tool-neutral process-priority and
timeout settings, dependency refreshes, and the related fixes below. Sections
are newest-first; a per-beta breakdown is in [Beta history](#beta-history-410-beta-1-to-beta-4)
at the end of this release.

### CHD: decompress a .chd straight out of an archive

#### New

- **Extract a CHD without unzipping it first.** A `.chd` packed inside a `.zip`/`.7z`/`.rar` now shows when you browse into the archive and can be extracted in place — chdman's Extract CD/DVD/Raw/HD/LD modes accept a `.chd` member, decompress it back to its disc image (`.cue`+`.bin`, `.iso`, `.raw`, …), and drop the result next to the archive. Previously a `.chd` inside an archive was hidden and unconvertible; you had to unzip by hand first. Recompression is deliberately **not** offered from an archive: chdman's Copy/Recompress mode would just re-CHD an already-finished `.chd`, a pointless round trip, so it stays disabled for archive members.

#### Internal

- chdman's five extract modes set `allows_archive_input=True` (in both the Python and JS registries); only `copy` stays opted out. `.chd` therefore joins `registry.archive_input_extensions()` (the convert gate) and, because chdman intentionally drops `.chd` from its loose-source `input_extensions`, `ArchiveService._listable_extensions` now unions `archive_input_extensions()` into the browse set so an archived `.chd` is *visible* there too (a loose `.chd` on disk stays non-convertible — it's still an output, not a source). The job pipeline needed no changes: it already extracts a member to a temp file before the tool runs, so extract-from-archive rides the same path as create-from-archive. Tests: `tests/test_archive_conversion_e2e.py` (extractcd/dvd/raw/ld from a real `.zip`, Copy still rejected), `tests/test_archive_preference.py` (`.chd` lists and is convertible from an archive).

### Handheld ROM: the ROM is now visible inside its archive

#### Fixed

- **Browsing into a compressed ROM archive no longer shows "Empty folder".** A ROM packed into `.7z`/`.zip` carried the **ARC**/**OK** badge but reported "0 items" and, when opened, "Empty folder — No convertible files here" — the contained ROM was invisible, contradicting the "(and back)" promise. The archive browser now lists the ROM, so a single-ROM archive shows "1 items" and reveals its member. The ROM is shown for visibility/verification only; it is **not** offered for in-place re-conversion (recompressing an already-archived ROM would be recursive), and the whole-archive Extract/Verify actions are unchanged.

#### Internal

- The bug was a conflation in the archive-listing filter: it sourced its members from `registry.archive_input_extensions()` (the union of modes with `allows_archive_input=True`), which doubles as the `plan_job` conversion gate — and romz keeps that `False` to stay non-recursive, so its ROM extensions got filtered out of the listing too. Fixed by splitting "what shows when you browse" from "what can be converted in place." Browsing is now **global, scoped to known extensions**: `ArchiveService._listable_extensions` lists `registry.convertible_extensions()` (every tool's source extensions) minus the archive containers, so any known source — handheld ROMs included — shows up when you look inside an archive. No per-tool flag is involved; a finished output a tool disowns as a source (chdman drops `.chd`) stays hidden for free. The convert gate stays narrow: `plan_job` and the recursive **Search** path use `registry.archive_input_extensions()`, and the archive route derives each member's `convertible_by` from `registry.tools_accepting_archive_member(ext)`, so a visible-only ROM is badged non-convertible and the UI offers no conversion the route would reject (`allows_archive_input` is unchanged — still `false` for romz in both the Python and JS registries). Search also now filters to the convert-gate set *before* the per-archive entry cap, so a pile of list-only members in a huge archive can't exhaust the 5000-entry limit ahead of a genuine convertible member. Documented in `docs/DESIGN_tool_plugin_architecture.md` and `docs/ADDING_PLATFORMS_AND_TOOLS.md` §17.5. Tests: `tests/test_archive_preference.py` (global-known browse superset vs convert-gate subset; entry-cap regression), `tests/test_romz_files_integration.py` (browse-in surfaces the ROM, Search excludes it), `tests/test_archive_conversion_e2e.py` (member listed but recompression rejected), updated `tests/test_archive_summary_lazy.py` counts.

### Faster directory listings in archive-heavy folders

#### Changed

- **Browsing a folder with thousands of archives is now fast.** The file listing used to open and parse *every* archive in the directory on the request thread — twice each (once for the member summary, once for the romz single-ROM Verify/Info gate) — so a large `.zip`/`.7z` folder could take ~40 s to list and, because the browser caps connections per host, that one slow request stalled job/metadata polling behind it (the UI looked frozen). The listing now returns immediately and the per-archive badges (member counts, "N converted", romz Verify/Info eligibility) are hydrated in the background via a new `POST /api/archive-summary` batch — the same pattern already used for CHD media-type badges. The badges therefore appear a moment after the rows.

#### Internal

- New shared, mtime-keyed archive-member cache. `services/archive_members.read_archive_members()` is now the single seam every member-listing consumer reads through (`ArchiveService.list_archive_contents`, `RomzService.is_single_rom_archive`, archive size lookups), backed by one global `utils.mtime_cache.MtimeCache` keyed by `(path, mtime, size)`. An archive is opened at most once per change across all consumers and all formats (`.zip`/`.7z`/`.rar`), invalidated automatically when a conversion rewrites it; a new format gets the cache for free. The change also collapsed `ArchiveService`'s three per-format listing methods into one format-agnostic `_filter_members`. `GET /api/files` gained `summarize_archives` (default `true` for API back-compat; the Web UI passes `false` and hydrates via `/api/archive-summary`). Hydration is bounded to the **visible page** (re-running on pagination/sort/filter) with a server-side `MAX_ARCHIVE_SUMMARY_PATHS` ceiling, and the raw-member cache skips archives over `MAX_CACHED_MEMBERS` so memory stays flat. The browser store also de-dupes a duplicate in-flight listing of the same directory, drops stale responses after navigation (a listing-generation guard prevents a late hydration from clobbering a newer listing), still honours a forced post-mutation refresh while a load is in flight, and keeps selected archive rows in sync with hydrated badges. Documented in `docs/DESIGN_tool_plugin_architecture.md` §3.3.2. Tests: `tests/test_archive_summary_lazy.py` (lazy deferral, batch↔inline parity, per-path error handling, batch cap, cache open-once / invalidate-on-change / oversized-not-cached).

### Nintendo 3DS: decompression + new upstream (.cxi/.3dsx)

#### New

- **3DS is now fully reversible.** A new `z3ds_decompress` mode restores the original ROM from a compressed Z3DS container (`.zcci → .cci`, `.zcia → .cia`, `.z3ds → .3ds`, `.zcxi → .cxi`, `.z3dsx → .3dsx`), in the Web UI ("Decompress 3DS") and the REST API. The compress/decompress round trip is byte-identical. The old "compress-only, keep your sources" caveat is gone — though delete-on-verify is offered for compress only (the restored ROM is not itself a verify-class file, so the compressed source can't be confirmed before deletion).
- **Two new 3DS input formats.** `.cxi` (CTR Executable Image) and `.3dsx` (homebrew) join `.cci`/`.cia`/`.3ds` as compressible inputs, mapping to `.zcxi`/`.z3dsx` (and back). Treated as experimental, like `.cia`.

#### Changed

- **New upstream: [`pacnpal/z3ds_compress`](https://github.com/pacnpal/z3ds_compress).** The `z3ds_compressor` binary is now built from this fork (which adds the decompression mode and `.cxi`/`.3dsx` support) instead of `energeticokay/z3ds_compress`. The fork builds with **CMake** (static libzstd via pkg-config), so the Docker builder stage gained `cmake`/`pkg-config` and the bundled `.local-bin/z3ds_compressor` was rebuilt. Direction is always forced with the tool's explicit `-c`/`-d` flag, so it never depends on magic-header auto-detection.

#### Internal

- `z3ds` slots the second mode in with no job-pipeline edits — the registry dispatches it like every other tool. `app/services/z3ds_compress.py` gained `Z3DS_DECOMPRESS_EXTENSIONS` / `Z3DS_DECOMPRESS_FORMATS` and direction-aware `_build_command` (`-c`/`-d`), `convert` (progress verb + ratio), `get_output_path_for_mode`, `info`, and `verify_stream`; the plugin adds a `ModeKind.EXTRACT` `ModeSpec`, splits `output_extensions` (compressed + restored ROMs) from `verify_extensions` (compressed containers only), and the `ConversionMode.Z3DS_DECOMPRESS` enum value, route messages, and `info.py` extension sets follow. Tests: extended registry / dispatch-routing / output-path suites plus new `z3ds_decompress` command-flag, extension-map, and info coverage.

### Handheld ROM: Verify/Info only on single-ROM archives

#### Changed

- **The romz Verify/Info row-actions now appear only on single-ROM `.7z`/`.zip` archives**, not on every archive in a directory. The Handheld ROM tool claims `.7z`/`.zip` so it can offer Verify/Info on the archives it produces, but it previously surfaced those actions on *any* archive purely by extension — including an unrelated multi-file `.zip` you happened to have. The file listing now inspects each archive's members and only offers romz Verify/Info when it holds exactly one Game Boy / GBC / GBA / DS ROM (the same single-ROM invariant verify/extract already enforce; OS/NAS clutter like `__MACOSX/…` / `.DS_Store` is ignored). The same gate covers the verify-*from-output* flow (a loose ROM row only offers Verify against a sibling `Game.gba.7z` when that archive is a genuine romz output) and the bulk **Verify selected** path (a non-single-ROM archive selected in extract mode no longer surfaces the button or gets submitted to the romz batch verify). Non-romz archives are unchanged — they stay browseable archives (`type="archive"`) with their extract behavior intact (issue #146).

#### Internal

- New per-file `ToolPlugin.verifies_path(path)` seam: the refinement of `verify_extensions` from a coarse extension claim to a per-file decision. `BaseTool` defaults it to the extension match (every other tool is unaffected); `RomzTool` overrides it via the new `RomzService.is_single_rom_archive` member probe. The registry exposes `tools_verifying_path(path)`, and `routes/files.py` materializes it into a tool-neutral `FileEntry.verifiable_by` list (on-disk files + archive containers, in both the directory listing and recursive search) that the frontend gates Verify/Info on. The narrowing rule is centralized in `registry.verifyToolForPath` and shared by the row menu (`RowActionsMenu`), the "Verify selected" gate (`FileList`), and the bulk target picker (`BulkVerifyModal`). The registry's extension match stays the source of truth so deliberate frontend exclusions hold (e.g. `.iso` is still not routed to Dolphin verify); `verifiable_by` only narrows it. `RomzTool.detect_output` likewise validates the candidate so a non-single-ROM `.7z`/`.zip` is never badged as a romz output (keeping the verify-from-output flow consistent). Documented in `docs/DESIGN_tool_plugin_architecture.md` §3.6 and `docs/ADDING_PLATFORMS_AND_TOOLS.md`.
- Tests: `tests/test_romz_files_integration.py` gains listing- and search-level regressions (a multi-file / no-ROM / corrupt archive does not surface romz actions; a single-ROM archive does; a coincidentally-named multi-file sibling is not badged as a romz output); `tests/test_romz_service.py` covers `is_single_rom_archive`; `tests/test_tool_registry.py` covers `tools_verifying_path`; and the `FileEntry` JSON-key parity suite is extended for `verifiable_by`.

### Handheld ROM support: GB / GBC / GBA / NDS → .7z / .zip

#### New

- **A sixth tool: Handheld ROM.** Compress Game Boy, Game Boy Color, Game Boy Advance, and Nintendo DS ROM dumps (`.gb` / `.gbc` / `.gba` / `.nds`) to a standard `.7z` or `.zip` archive, and extract them back. Three modes: `romz_7z` (ROM → `.7z`, LZMA2, smallest output), `romz_zip` (ROM → `.zip`, deflate, broadest compatibility), and `romz_extract` (`.7z`/`.zip` → the original ROM). Available in the Web UI and REST API, with live progress, single + batch verify, and file-info. No keys required.
- **Archive-quality lossless compression.** The compress/extract round trip reproduces the original ROM byte-for-byte (verified by SHA-1 on real homebrew dumps). On the sample set GBA dumps shrank by roughly half and GB/GBC by around two thirds. Output names preserve the ROM extension in front of the archive suffix (`Game.gba` → `Game.gba.7z`), so the extract direction is deterministic and same-stem ROMs of different platforms never collide.
- **Compression-effort presets.** The compress modes expose a Fast / Default / Max preset, reusing the same compression picker (and the shared "Reset to default" button) as the other tools and remembered per tool between sessions. Max is the `-mx9 -md=256m -mfb=273 -m0=lzma2` profile; the tool ships with Max as the default. Verify runs `7z t` over the archive, and delete-on-verify is offered for the compress modes.

#### Internal

- The tool reuses the `7z` binary already in the image (`p7zip-full`, exposed via the new `SEVENZIP_PATH` env var, default `7z`) — no new build stage. The write/extract/test paths shell out through the shared `SubprocessRunner`; the read side (member listing, info, single-member validation) reuses `zipfile` / `py7zr` directly.
- Slots into the registry as `romz` with **zero job-pipeline edits** — `job_manager` / `convert` / `files` / `info` all dispatch through the registry. New `app/services/romz.py` service + `app/services/tools/romz.py` plugin, a `RomzInfo` model (with the contained-ROM name, original size, and ratio) and `romz_*` `FileEntry` fields, the three `ConversionMode` entries (`ROMZ_7Z` / `ROMZ_ZIP` / `ROMZ_EXTRACT`), and an `info.py` `_VERIFY_CONFIG` entry behind the shared verify-route factory. ROM outputs are picked up automatically by the registry-driven library scan / DAT-match (`scannable_extensions()`) and the tool-neutral priority/timeout policy (`COMPRESSATORIUM_ROMZ_*`).
- `.7z`/`.zip` stay classified as browseable archives (`type="archive"`): the compress/extract modes set `allows_archive_input=false`, so ROM extensions never enter the archive-member surface and the existing archive guard keeps archive browsing undisturbed. When `romz_extract` is the active mode the file browser makes archive rows selectable (the row's checkbox; name-click still browses in), gated on `conversion.allowsInput`, so the extract workflow is reachable from the UI — every other tool still treats archives as browse-only.
- The extract mode reverts this tool's own single-ROM archives: it validates exactly one handheld-ROM payload, ignoring OS/NAS clutter (`__MACOSX/._Game.gba`, `.DS_Store`, `Thumbs.db`, …) so a ROM zipped on macOS/Windows still counts as one member. This junk filtering now goes through a shared, platform-agnostic `utils.junk.is_junk_path` (a path-level companion to `is_junk_entry`), which also replaces the macOS-only `ArchiveService._is_macos_metadata_member` so archive listings, `info()`, and romz validation hide the same clutter.
- Recursive **Search All** now emits `.7z`/`.zip` containers as top-level results (not just their members), so the `romz_extract` batch workflow is reachable from search too; the container row mirrors the on-disk file schema and is browse-only unless an archive-direct mode is active.
- Extract and verify are hardened: both honor the shared `CHD_ARCHIVE_MAX_ENTRIES` / `CHD_ARCHIVE_MAX_MEMBER_SIZE` / `CHD_ARCHIVE_MAX_TOTAL_SIZE` limits via the new `ArchiveService.enforce_archive_limits` (zip-bomb / oversized-archive guard, previously bypassed because romz shells out to `7z`); verify now requires a single-ROM payload before running `7z t`, so an unrelated multi-file/source archive can't be marked "Verified"; and the `7z t` test, like the extract command, passes member/archive names after `--` so a name beginning with `-` is treated as a filename.
- Tests: new `tests/test_romz_service.py` (compress / extract round-trip / verify / single-member validation / cancel cleanup, plus a real `zipfile`/`py7zr` listing exercise), `tests/test_romz_routes.py` (info + single/batch SSE verify), and `tests/test_romz_files_integration.py` (the archive-classification regression), plus extended registry, dispatch-routing, mode-parity, and outputs-parity suites.

### PSP / PS2 support: CSO / ZSO / DAX via maxcso (#127)

#### New

- **A fifth tool: CSO.** Compress PSP/PS2 `.iso` disc images to every format [maxcso](https://github.com/unknownbrackets/maxcso) writes, and back. Five modes: `cso_compress` (ISO → CSO v1, the universally-supported default), `cso2_compress` (ISO → CSO v2, better block alignment for recent emulators), `zso_compress` (ISO → ZSO, lz4, faster to decode), `dax_compress` (ISO → DAX, legacy PSP format), and `cso_decompress` (CSO/ZSO/DAX → ISO). PPSSPP and PCSX2 read these formats directly, so no separate decompress step is needed to play. Available in the Web UI and REST API, with live progress, single + batch verify, and file-info. No keys required.
- **Compression-effort presets.** The compress modes expose a Fast / Default / Max effort preset (mapped to maxcso's `--fast`, default trials, and extra `--use-zopfli`/`--use-libdeflate` / `--use-lz4brute` trials), reusing the same compression picker the other tools use and remembered per tool between sessions. CSO ships a strong default (the Max preset, smallest output).
- **Reset to default.** Every compression-capable tool (CHD, Dolphin, Switch, CSO) gains a "Reset to default" button under its compression picker that restores that tool's codec/layout/level/effort to its default and confirms with a toast (disabled when already at the default).
- **Lossless and verifiable.** The compress/decompress round trip reproduces the original `.iso` byte-for-byte. Verify runs maxcso's `--crc` over the compressed container, and delete-on-verify is offered for the compress modes. CSO/ZSO/DAX sources can also be converted from inside ZIP/7z/RAR archives.

#### Internal

- maxcso is built from source in a dedicated Dockerfile builder stage, pinned to a tagged release for reproducible images (build deps `liblz4`/`libuv`/`libdeflate`/`zlib`; the runtime stage ships their shared libs, including Debian trixie's time64 `libuv1t64`). The binary lands at `/usr/local/bin/maxcso`, exposed via the new `MAXCSO_PATH` env var.
- The tool slots into the registry as `cso` with **zero job-pipeline edits** — `job_manager` / `convert` / `files` / `info` all dispatch through the registry. New `app/services/maxcso.py` service + `app/services/tools/maxcso.py` plugin, a `CsoInfo` model and `cso_*` `FileEntry` fields, the five `ConversionMode` entries (`CSO_COMPRESS` / `CSO2_COMPRESS` / `ZSO_COMPRESS` / `DAX_COMPRESS` / `CSO_DECOMPRESS`), and an `info.py` `_VERIFY_CONFIG` entry behind the shared verify-route factory. CSO outputs are picked up automatically by the registry-driven library scan / DAT-match (`scannable_extensions()`) and the tool-neutral priority/timeout policy (`COMPRESSATORIUM_MAXCSO_*`).
- Tests: new `tests/test_cso_service.py` (convert / verify / cancel / output-path mapping) and `tests/test_cso_routes.py` (info + single/batch SSE verify), plus extended registry, dispatch-routing, mode-parity, outputs-parity, and archive-conversion suites. `docs/ADDING_PLATFORMS_AND_TOOLS.md` was corrected for the globals it had omitted — the `_VERIFY_CONFIG` step, the `fileIcon.js` / `FileRow.svelte` / `conversion.svelte.js` / `HelpView.svelte` frontend touch-points, and the inaccurate conftest "binary stub" note.

### Library scan + DAT matching now cover every format (not just CHD)

#### New

- **The background library scan discovers all tool outputs, not just `.chd`.** Discovery is registry-driven (every registered tool's output / verify extensions), so Dolphin (`.rvz`/`.wia`/`.gcz`), 3DS (`.z3ds`/`.zcci`/`.zcia`), Switch (`.nsz`/`.xcz`), and plain `.iso`/`.bin` libraries are now visited. A new scan phase (Phase 3) primes the DAT-match cache for every discovered file, so non-CHD libraries get cached match results just like CHDs. Discovery resolves symlinks (so a file is matched/cached under the same path the on-demand match endpoints use), drops symlinks whose target escapes the configured volumes, and matches on the real file suffix (a `.ciso` is not admitted just because it ends with `.iso`). When no DATs are imported, Phase 3 is skipped and the job progress advances cleanly instead of appearing stuck.
- **Compressed Dolphin images match Redump DATs.** Matching now uses each tool's embedded/derivable content hashes via a per-tool hook: chdman reports the CHD header/data SHA1, and Dolphin reports the reconstructed disc SHA1 from `dolphin-tool verify` — the hash MAME Redump records for GameCube/Wii discs — so `.rvz`/`.wia`/`.gcz` outputs match without the container bytes being identical. Formats with no embedded hash (3DS/Switch/plain `.iso`/`.bin`) fall back to file-level SHA1 as before.
- **extractcd `.bin` data tracks are now scannable.** CHDMAN's `extractcd` writes a `.cue` plus a `.bin` data track; Redump DATs index the `.bin` bytes, so the scan now discovers and matches the `.bin` (previously only the tiny `.cue` was visited).
- **Heavy Dolphin hashing is bounded and interruptible.** The `dolphin-tool verify` disc-hash pass runs under the `match` workload lane (so `MAX_MATCH_CONCURRENCY` caps concurrent full-disc reconstructions), honors the `MATCH_MAX_FILE_SIZE` cap *before* doing the work, and is cancelled promptly when you cancel the scan or DAT-match job instead of running to completion.

#### Fixed

- **Stale / failed hashes no longer poison the match cache.** A transient hash failure (e.g. `dolphin-tool verify` crashing, a stat error, or a stale CHD whose metadata refresh failed) is recorded as a non-cacheable error rather than a false "unmatched", and a forced rescan clears any existing match row whose recompute is non-cacheable so `/dat/matches/lookup` doesn't keep showing an outdated result. A stale CHD still suppresses its outdated embedded disc hash but falls back to a file-level SHA1 of the current file.

#### Internal

- `ToolPlugin` gains `embedded_hashes(path, *, cancel_event=None)` (default empty) and an `embedded_hash_is_exhaustive` flag. `EmbeddedHashUnavailable` signals a non-cacheable miss; for *exhaustive* tools (Dolphin, where the recompressed container can never be DAT-indexed) a content-hash miss is definitive and skips the file-level fallback, while non-exhaustive tools (CHD) still fall back. `ToolRegistry` gains `output_extensions()` / `scannable_extensions()`; `DATStore` gains `delete_match()`. The DAT lookup + result-dict building is centralized in one `_lookup_sha1_match()` helper shared by the embedded-hash and file-level paths.
- The shared `SubprocessRunner` gains a one-shot, cancel/timeout-aware `run_capture()` (PID-tracked, TERM→KILL on abort, honoring the tool nice/ioprio policy) that backs the Dolphin disc-hash extraction. Cancellation threads from the scan / DAT-match jobs through `embedded_hashes` → `disc_hashes` → `run_capture`. `media_type`, disc-ID embedding, and the `chd_metadata_store` cache remain CHD-only. The plugin contract additions are documented in `docs/DESIGN_tool_plugin_architecture.md`.

### Tool-neutral process priority and timeout settings (#132)

#### New

- **The process-priority and timeout knobs are no longer chdman-specific.** The nice level, I/O priority, and info/verify timeouts govern *every* conversion tool (chdman, Dolphin, 3DS, Switch), not just chdman, so they're now exposed under tool-neutral names: `COMPRESSATORIUM_TOOL_NICE`, `COMPRESSATORIUM_TOOL_IOPRIO_CLASS`, `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL`, `COMPRESSATORIUM_TOOL_INFO_TIMEOUT`, and `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT`. The old `CHD_CHDMAN_*` / `CHD_INFO_TIMEOUT` / `CHD_VERIFY_TIMEOUT` names keep working as aliases, so existing setups are unaffected.
- **Per-tool overrides.** You can give a single tool a different priority or timeout with `COMPRESSATORIUM_<TOOL>_*` (e.g. `COMPRESSATORIUM_DOLPHIN_TOOL_NICE=15`, `COMPRESSATORIUM_NSZ_VERIFY_TIMEOUT=300`), falling back to the shared default when unset. `<TOOL>` is `CHDMAN`, `DOLPHIN_TOOL`, `NSZ`, `Z3DS`, or `MAXCSO`; the info-timeout override applies only to the tools whose `info` runs a subprocess (chdman, Dolphin).
- **Switch/3DS verification now honors the verify timeout.** Long or hung `nsz`/`z3ds` verify runs are bounded by `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` (or the per-tool override), matching chdman and Dolphin; previously these ran unbounded.

#### Internal

- The priority/timeout policy is resolved once in the shared `SubprocessRunner` (`nice_value` / `ioprio_prefix` / `nice_prefix` / `apply_nice` / `info_timeout` / `verify_timeout`) instead of each service re-reading a chdman-named setting. The Docker image no longer bakes the priority defaults as `ENV` (which would have shadowed the lower-precedence legacy aliases); the 10/2/6 defaults come from app config.

### Delete-on-verify no longer leaks verification records (#130)

#### Fixed

- **Non-CHD verify-class sources are cleared from the verification store on delete-on-verify.** When a conversion deleted its source after verifying, `job_manager` only cleared the persistent verification record if the source ended in `.chd`. But `verification_store` is tool-wide — it tracks verify-class outputs for every registered tool — so deleting a non-CHD verify-class source (`.rvz`/`.wia`/`.gcz`/`.z3ds`/`.zcci`/`.zcia`/`.nsz`/`.xcz`) left a stale record pointing at a file that no longer exists. The clear is now gated on `ext in registry.verify_extensions()` (tool-wide), mirroring the already-generalized file-browser delete/rename paths, while the CHD-specific `chd_metadata_store` clear stays gated on `.chd`.

#### Internal

- `tests/test_mode_parity_fixes.py` gained a regression test deleting a `.rvz` source on verify (asserting the tool-wide store is cleared but `chd_metadata_store` is not), and the existing z3ds test now asserts a non-verify-class `.3ds` source leaves both stores untouched.

### Dependency updates

#### Changed

- **Routine dependency refresh (Dependabot).** Shipped in `4.1.0-beta-3`, no
  behavior change:
  - **npm / frontend:** `vite` 6.4.2 → 8.0.16 (#142), `svelte` 5.55.10 → 5.56.1
    (#143), `svelte-eslint-parser` 1.6.1 → 1.7.1 (#144), `eslint` 10.4.0 →
    10.4.1 (#139), `eslint-plugin-svelte` 3.18.0 → 3.19.0 (#138).
  - **pip / backend:** `pytest-asyncio` `>=1.3.0` → `>=1.4.0` (#141),
    `python-multipart` `>=0.0.29` → `>=0.0.30` (#140).
  - **GitHub Actions:** `docker/setup-qemu-action` 4.0.0 → 4.1.0 (#145).

### Beta history (4.1.0-beta-1 to beta-4)

The stable 4.1.0 was cut from four betas. Each entry below points at the
thematic section above where the change is described in full; this is the
chronological map of what landed in which pre-release.

- **`4.1.0-beta-1` (2026-06-03).** Handheld ROM tool — GB / GBC / GBA / NDS ↔
  `.7z`/`.zip` (#137); romz Verify/Info gated to single-ROM archives (#147);
  Nintendo 3DS decompression + the new `pacnpal/z3ds_compress` upstream adding
  `.cxi`/`.3dsx` (#148). (The maxcso CSO/ZSO/DAX tool, the registry-driven
  library-scan / DAT-match generalization, tool-neutral priority/timeout
  settings, and the delete-on-verify fix were authored against 4.0.1 and are
  rolled into this release per the note at the top.)
- **`4.1.0-beta-2` (2026-06-04).** Faster directory listings in archive-heavy
  folders via the shared, mtime-keyed archive-member cache and the
  `POST /api/archive-summary` batch (#149).
- **`4.1.0-beta-3` (2026-06-04).** Dependency updates (#138–#145) — see
  [Dependency updates](#dependency-updates) above.
- **`4.1.0-beta-4` (2026-06-04).** Archive browsing made global, scoped to
  known extensions, so a packed handheld ROM is now visible when you browse
  into its archive instead of showing "Empty folder"; list-only members kept
  out of Search and the convertible flag; and chdman extract enabled from
  inside an archive so a `.chd` decompresses in place (#150, #151).

## 4.0.0-beta-10 (2026-06-01)

### Switch key discovery + logging cleanup

#### New

- **Switch keys are found recursively.** When `SWITCH_KEYS` isn't set, the app checks the standard locations and then recursively walks your mounted game volumes (and the data dir) for `prod.keys`/`keys.txt`, skipping junk dirs and bounded so it can't stall startup. Set `SWITCH_KEYS` to point straight at the keys dir if they live very deep.

#### Internal

- **Log prefix renamed `chd` → `compressatorium`** (a leftover from the chdman-only days), centralized in `logging_setup` so the root name lives in one place.
- The OS/NAS junk-file filter is now shared (`utils/junk`) between the file browser and the key search, a single source of truth.

## 4.0.0-beta-9 (2026-06-01)

### File browser polish + Switch key visibility

#### New

- **The file browser hides OS/NAS junk.** Clutter like `.DS_Store`, AppleDouble (`._*`), `Thumbs.db`, `desktop.ini`, `$RECYCLE.BIN`, `System Volume Information`, `@eaDir`, `#recycle`/`@Recycle`, `lost+found`, `.Trash-*`, and `.nfs*`/`.fuse_hidden*` no longer show up in listings or search (matched case-insensitively).
- **Delete non-empty folders, with a guardrail.** You can now delete a directory and everything inside it. It takes two explicit confirmations; the second spells out that it's recursive and permanent. Files and empty folders are unchanged (one confirm), and deletion is still blocked while a conversion is using anything under the folder.

#### Internal

- **Switch key discovery is now logged at startup.** The log says whether the Switch (nsz) tool is enabled and, when it isn't, which locations were searched for `prod.keys` and how to enable it (`SWITCH_KEYS`). Previously the decision was silent.
- Documentation moved under `docs/` with internal links updated.

## 4.0.0-beta-8 (2026-06-01)

### Nintendo Switch support (nsz)

#### New

- **A fourth tool: Switch.** Compress `.nsp`/`.xci` dumps to `.nsz`/`.xcz` and back, using [nsz](https://github.com/nicoboss/nsz) (the Tinfoil/DBI-compatible standard). Two modes: `nsz_compress` and `nsz_decompress`. Available in the Web UI and the REST API, with live progress, single + batch verify, and file-info.
- **Bring your own keys.** Switch content is encrypted, so nsz decrypts it (losslessly, reversibly) before compressing. The app ships no keys: mount your own `prod.keys` and set `SWITCH_KEYS` to the directory holding it (or let the app best-effort find it in `~/.switch` or your volumes). Missing keys fail the job with a clear message instead of crashing, and hide the Switch tool from the UI entirely. Key file names are git-ignored and never baked into the image or logged.
- **Per-job compression, remembered.** Switch Compress exposes a layout choice (Solid = smaller, Block = random access) and a zstandard level (1-22) per job, threaded through to nsz. Your choice is saved server-side per tool (new `GET`/`PUT /api/preferences/conversion`) so it persists across sessions and browsers, and the same mechanism now remembers chdman and Dolphin compression settings too.

#### Internal

- New `app/services/nsz.py` service and `app/services/tools/nsz.py` plugin, registered in the tool registry. nsz loads keys at import (no `--keys` flag), so the service runs it with a temp `$HOME` symlinked to the resolved key file; progress is estimated from output-file growth (nsz's enlighten bar is silent on a pipe); verify uses nsz's own `-V`. `nsz` is installed via `requirements.txt`. New settings: `NSZ_PATH`, `SWITCH_KEYS`, `NSZ_COMPRESSION_LEVEL`. New `GET /api/tools` reports tool availability so the UI can hide Switch when keys are absent.
- Tests: `tests/test_nsz_service.py`, `tests/test_nsz_routes.py`, plus registry/parity coverage extended for the new modes.

## 4.0.0-beta-7 (2026-06-01)

### Resizable panels and table columns (#120)

#### New

- **The workspace layout is now adjustable.** Drag the divider between the file list and the right Convert/Jobs panel to give the table more room, and drag individual table column borders (Name, Size, Ext, Status) to size them how you like. Double-click a divider or column border to reset it. The handles are keyboard-accessible too (focus, then arrow keys; Enter or Home resets).
- **Widths are remembered on the server.** Layout preferences persist through a browser or cache wipe and follow you across browsers. This is the first server-stored preference; everything else still lives in localStorage. The panel split is shared, while column widths are kept per tool (chdman / dolphin / z3ds) since each lists different file types.

#### Internal

- New generic `preferences` key/value table (Alembic migration `0002`, idempotent) with a `PreferencesStore` service and `GET`/`PUT /api/preferences`. Single-user, no auth. Reusable for future preferences.
- A `layout` store loads localStorage synchronously on boot (no flash), reconciles with the server, and writes both back debounced. A dirty flag plus a mutation-version guard keep an in-flight server sync from clobbering a width the user just changed, and keep an unsaved edit from being lost on tab close.
- A reusable `Splitter` component (WAI-ARIA window-splitter pattern) drives both the panel dividers and the column handles. The file table moved to `table-layout: fixed` with a `<colgroup>`; the Name column absorbs slack and a min-width keeps the horizontal scroll when columns outgrow the panel.

## 4.0.0-beta-6 (2026-06-01)

### Visible Actions header (#119)

#### Changed

- **The file table's Actions column has a visible header.** It was a screen-reader-only label before; now it reads "Actions" like the other column headers.

## 4.0.0-beta-5 (2026-05-31)

### Search all: instant feedback + clear label (#118)

#### Fixed

- **"Search all" now reacts the instant you click it.** The recursive scan (which walks into archives and can take a few seconds) ran with no loading state, so the button sat silent until results came back. It now flips to a spinner with "Searching…" the moment you click and disables itself while the scan runs, matching the Refresh button. This restores the immediate feedback the old Preact UI had.
- **The button says what it does.** Replaced the bare folder-search icon with a labelled "Search all" button, so it's no longer a guess.
- **Stale error banner no longer survives a search.** `_enterSearch` now clears `entriesError` on entry, the same as the directory load paths. A failed search used to leave the error banner up through a retry and even past a later successful search.

#### Internal

- "Search all" uses the shared `Button` component instead of a one-off `<button>` with hand-rolled hover/focus/disabled CSS. The spinner is driven through the `icon` snippet because `Button`'s `loading` prop only dims and changes the cursor.
- Overlapping `_enterSearch` calls are dropped with an early return, so a finishing request can't clear the busy flag while a later one is still running.
- Removed a dead `.warning strong` CSS selector in `ConvertPanel`; `svelte-check` is clean (0 errors, 0 warnings).

## 4.0.0-beta-4 (2026-05-31)

### In-app Help content

#### New

- **The Help view is no longer a placeholder.** It now covers the whole workflow end to end: picking a tool, browsing and Search All, a full mode reference per tool, the compression codecs and the zlib / AetherSX2 gotcha, where output lands and the skip / rename / overwrite choice, the job queue, the verify-then-delete safety flow, the CHD inspector, DAT matching, archive conversion, a troubleshooting list, an FAQ for the common edge cases, and how to file bugs and feature requests. All of it written to match the app, not the README.

#### Internal

- **Repo links live in one place.** Added `repository`, `bugs`, and `homepage` to `package.json`. The Help view's GitHub and issues links are derived from those fields with a tree-shaken named import, so moving the repo is a one-line change instead of a hunt through hardcoded URLs.

## 4.0.0-beta-3 (2026-05-31)

### Sidebar: full title + relocated collapse control

#### Fixed

- **Brand title no longer truncates.** The "Compressatorium" wordmark was being clipped to an ellipsis in the sidebar; `.brand-text` now keeps its full width (no `flex-shrink`, no `overflow`/ellipsis clamp) and fits within the existing 240px rail.

#### Changed

- **Collapse toggle promoted to its own section.** The sidebar collapse / expand control moved out of the brand row into a dedicated, full-width labelled row (`Collapse sidebar` / `Expand sidebar`) below the Tool list, set off by a divider, far more visible than the small icon previously tucked beside the logo. Relocating it freed the horizontal space the title needed, so the sidebar width is unchanged at 240px.

### Archive conversion test coverage (#117)

#### Internal

- New `tests/test_archive_conversion_e2e.py` exercises end-to-end archive conversion across every supported source extension, locking in the issue #113 fix. `.gcm` dropped from the README's supported-format list.

## 4.0.0-beta-2 (2026-05-31)

### Live job toasts + one-click Search all

#### New

- **Live toast for every running job.** A new `jobToasts` tracker surfaces a svelte-sonner loading toast for each job in the `processing` state, keyed by job id, and resolves it to success / error / cancelled when the job reaches a terminal state. A single reconcile `$effect` in `App.svelte` drives it off `jobs.jobs`, so jobs restored from a snapshot/poll and jobs started by *other* clients get a toast too, not just ones queued in the current tab. External-scan modes (`metadata_scan` / `dat_match`) are excluded unless the user has opted to show them, matching the queue's default visibility.
- **One-click "Search all" restored.** The Preact UI had a one-click action that recursively listed every convertible file under the current folder (including inside archives); the Svelte rebuild had gated the recursive scan behind a text query, so an empty query just exited search. A `FolderSearch` toolbar button now runs the all-files scan (empty filter = show everything), and a forced refresh re-runs it while in that view.

#### Fixed

- **Correct empty-string toast description.** `runningDescription` used `pct ?? lead ?? 'Processing…'`, but `''` is not nullish, so a job with no message and an unrecognised mode produced a blank description. Truthiness fallbacks now land on the percentage or `Processing…` instead.

#### Internal

- `jobToasts` uses null-prototype object maps (non-reactive): the reconcile effect both reads and writes the collection, so a `SvelteMap` would loop; the plain object map is also `svelte/prefer-svelte-reactivity` clean.
- Optional chaining on job props in the `App.svelte` reactivity loop, matching reconcile's defensive `job?.id`.

### Archive conversion for every tool (issue #113)

#### Fixed

- **Convert any supported file from inside an archive.** A `.zip` (or `.7z`/`.rar`) containing 3DS ROMs previously showed nothing to convert, the archive listing only recognised CHDMAN source extensions, and only CHDMAN create modes accepted archive members. Now every convertible *source* is surfaced and convertible from inside an archive: CHDMAN (`.gdi`/`.iso`/`.cue`/`.bin`), Dolphin (`.iso`/`.gcz`/`.wia`/`.rvz`/`.wbfs`), and 3DS (`.cci`/`.cia`/`.3ds`). CHDMAN extract/copy modes stay archive-disabled because they act on a finished `.chd` (an output, not a source).
- **Correct output extension for 3DS archive members.** z3ds derives its output extension from the input (`.3ds`→`.z3ds`, `.cci`→`.zcci`, `.cia`→`.zcia`); archive members now preserve their original extension through path routing so the mapping is right (previously they would have defaulted to `.zcci`).

#### Internal

- Archive listing is driven by the tool registry (`registry.archive_input_extensions()`) instead of a hardcoded set, so it stays in lockstep with the on-disk directory view and with the `allows_archive_input` mode flags.
- `allows_archive_input` is now set on every convertible-source mode (Dolphin + 3DS, alongside CHDMAN create); the frontend tool registry mirrors the same flags.
- New `ArchiveService._output_name_for_member()` keeps the member's extension on the flattened output name; CHDMAN/Dolphin strip it via `Path.stem`, leaving their output paths unchanged.

## 4.0.0-beta-1: Svelte 5 frontend rebuild (2026-05-29)

The first beta of the rebuilt frontend. Backend / REST / SSE contract
unchanged; the whole `static/js/app.js` Preact-via-htm monolith is
replaced by a Svelte 5 + Vite SPA under `src/`, with per-domain
class-singleton stores, a declarative tool registry, a design-token
system with light / dark / system themes, a sidebar + dashboard
layout, and ~18 rounds of post-merge review fixes folded in.

Topic sections below capture the development phases (P4 file browser,
P5 conversion + job queue, P6 verify, P7 modals + DAT view, P8
dashboard cards) and the supporting library adoptions (Lucide,
bits-ui, svelte-sonner, mode-watcher).

### Duplicate-output preflight (P7 part 2)

#### New

- **DuplicateModal preflight.** ConvertPanel now runs `conversion.checkDuplicates(paths)` before each submit. If the backend reports any output collision (`d.exists === true`), the dialog opens with the conflict list and three choices: Cancel, Skip duplicates, Overwrite all. Resolution flows back into `conversion.submit(paths, { duplicateAction })` via a per-submit `Promise` resolver, no global pending-state to leak. No collisions → submit fires straight through, no modal.

#### Internal

- Duplicate-modal state lives entirely inside ConvertPanel as a `$state` resolver-or-null. Opening sets the resolver; the modal's `onResolve` callback nulls it and resolves the awaited promise. Avoids stale-prompt bugs when the user submits, cancels, and re-submits.

### DAT view + dashboard cards (P7/P8)

#### New

- **DAT view shipped.** `src/lib/components/views/DATView.svelte` exposes Import DAT (file picker → `api.importDAT`), Sync MAMERedump (with a 2 s poll loop that auto-stops when `syncStatus.syncing` flips false), per-DAT delete, and a stats strip (total DATs / entries / cached matches). The view drives the existing `datMatching` store; the FileList badges that the store already powers light up once imports complete.
- **Dashboard shipped.** Five cards composed via a generic `StatCard` wrapper: `QueueSummaryCard` (queued / active / done / failed counts + stuck-state badge), `VolumeOverviewCard` (top 4 volumes + file counts), `RecentConversionsCard` (last 5 terminal jobs across any tool), `VerificationStatusCard` (cached verified count + in-flight batch), `QuickToolsCard` (one tile per registered tool, deep-links to `#/workspace/<tool>`, adding a 4th tool surfaces a 4th tile automatically).

#### Internal

- `datMatching` store gains `dats`, `datsLoading`, `datsError`, `stats` $state fields + `loadDATs()`. The duplicate `importDAT` definition is removed; the kept version refreshes the DATs list (not just hasDats) after import.
- `StatCard` exposes `title`, `subtitle`, `icon` / `body` / `footer` snippets, and a `--card-accent` CSS variable so each tile can take its own accent color without per-tile style overrides.
- DAT sync polling lives in a `$effect` keyed off `syncing`: start interval when sync flips on, clear on sync-end (or component unmount).

### Confirmation + file modals (P7 part 1)

#### New

- **Modal foundation.** `BaseModal.svelte` wraps bits-ui Dialog with shared chrome (header, title, close, description, footer); `ConfirmModal.svelte` provides the standard confirm/cancel pattern. All future modals slot into these via `{#snippet body()}` / `{#snippet footer()}`.
- **DeleteModal + BulkDeleteModal.** The Delete action in `RowActionsMenu` now opens a destructive-styled confirm dialog; the "Delete" bulk-action in FileList's selection bar opens the bulk version (preview list capped at 20 rows + tail counter, calls `api.deleteBatch`). Both refresh the file listing on success.
- **RenameModal.** RowActionsMenu Rename opens a file-name input pre-populated with the current name (Enter submits, Esc cancels, bits-ui Dialog handles focus-trap).
- **CHDInfoModal.** RowActionsMenu Info opens a dialog that calls `tool.getInfo(path)` via `registry.toolForVerifyPath`, no `if (tool === 'chdman')` chains, works for `.chd` / `.rvz` / `.z3ds` identically. Renders the backend payload as a labelled key/value list with JSON pretty-print for nested objects.
- **CancelAllJobsModal + ClearDoneModal.** Replace the inline `window.confirm()` calls in JobsPanel. The Cancel-all dialog reports the queued+processing count; the Clear dialog reports the completed+failed+cancelled count. Both bind their busy state to the store flags so the buttons disable mid-action.

#### Internal

- New `src/lib/components/modals/` (BaseModal, ConfirmModal, BulkVerifyModal, BulkDeleteModal, DeleteModal, RenameModal, CHDInfoModal, CancelAllJobsModal, ClearDoneModal). Each modal mounts via App.svelte and self-renders against a `ui` store target (`ui.deleteTarget`, `ui.bulkDeleteEntries`, `ui.renameTarget`, `ui.chdInfoTarget`, `ui.showCancelAll`, `ui.showClearDone`, `ui.bulkVerifyItems`).
- `JobsPanel.handleCancelAll` / `handleClearCompleted` now just toggle `ui.show*` flags instead of running their own try/catch, the modal owns the confirmation + success/error toast.

### Verify flows (P6)

#### New

- **Single + batch verify shipped.** `RowActionsMenu` already routed Verify to `verification.verifyOne(toolId, path)` via `registry.toolForVerifyPath`; P6 adds the batch flow. The new `BulkVerifyModal` (bits-ui Dialog) opens from a "Verify selected" button in the FileList selection bar. It groups the selection by tool (`.chd` → chdman, `.rvz/.wia/.gcz/.wbfs` → dolphin, `.z3ds/.zcci/.zcia` → z3ds), runs each group as a back-to-back batch, shows live per-file progress + an overall counter, and supports mid-run cancel via `verification.cancelBatch()` (AbortController).
- **Verified set rehydrates on reload.** `App.svelte` calls `verification.loadVerified()` on mount so `OK` badges and the delete-on-verify gate survive a page refresh.
- **Selection bulk-action bar.** FileList's selection bar gains `Verify` (hidden when none of the selected files have a verify-extension match, registry-driven) and `Delete` actions; the Delete action sets `ui.bulkDeleteEntries` for the P7 modal to consume.

#### Internal

- New `src/lib/components/modals/` directory; mounted via App.svelte so any panel can launch a modal by writing to a `ui` target (`ui.bulkVerifyItems`, `ui.bulkDeleteEntries`, etc.). bits-ui Dialog handles focus-trap / Escape / overlay-click for free.
- The bulk-verify dialog tracks its own `running` / `groupIndex` / `runResults` state and uses `verification.batchRun` for the in-flight per-file progress payload (`done`, `currentPath`, `currentFilename`, `currentPercent`, `message`).
- Auto-cancel on dismiss: closing the dialog while a batch is running aborts the in-flight `fetch` via `verification.cancelBatch()` rather than letting it complete silently.

### Conversion config + job queue (P5)

#### New

- **P5 conversion config + job queue shipped.** `ModeSelect`, `CompressionPicker`, `ConvertPanel`, `JobRow`, `JobsPanel`, `RowActionsMenu` components under `src/lib/components/panels/`. Pick mode (grouped by tool/group label from registry), pick compression (chdman: chip-multi; Dolphin: single codec + numeric level via codec metadata in registry; z3ds: nothing rendered), set output directory, toggle delete-on-verify, submit. Live progress, queue/completed/failed tabs with badge counts, per-row Cancel / Dismiss, global Cancel all + Clear, stuck-state banner with Recover action, "Show metadata jobs" toggle persists.
- **Per-row file actions menu.** `bits-ui` DropdownMenu drives the FileRow `⋯` button: Info / Verify / Rename / Delete with keyboard-accessible focus management. Verify wires through `verification.verifyOne(toolId, path)` via registry lookup (`registry.toolForVerifyPath(path)`), no tool-specific branches. Rename / Delete set `ui.renameTarget` / `ui.deleteTarget` for the modals to be wired in P7.
- **Codec metadata in the registry.** Each tool descriptor now declares its `compressionCodecs`, `compressionStyle` (`'multi'` | `'single-with-level'` | `'none'`), and optional `compressionLevelRange`. `CompressionPicker` reads this and renders the right control with zero `if (tool === ...)` checks. chdman: 5 codecs as chips (zlib / lzma / lzma2 / flac / cdfl), comma-joined. Dolphin: 4 codecs + "no compression" via dropdown plus 1–22 level slider (default 19, MAME Redump). z3ds: control hidden.

#### Internal

- `conversion.svelte.js` gains `toggleCodec(codec)` (chdman chip toggle, with `'none'` mutex) and `setSingleCodec(codec)` (Dolphin), clean store API for the picker.
- `JobsPanel` consumes `jobs.pageJobs`, `jobs.queuedCount + jobs.processingCount`, etc. directly from the store so the rune-graph tracks updates without intermediate `$state` mirrors.
- `bind:checked={() => jobs.showExternalScanJobs, (v) => jobs.setShowExternalScanJobs(v)}` uses Svelte 5.9+ function bindings to drive the metadata-jobs toggle through the store setter (which persists to localStorage).
- `RowActionsMenu` styles its bits-ui primitives via `:global(...)` selectors keyed off the `[data-highlighted]` / `[data-disabled]` attributes the headless components emit.
- `FileRow` drops its inline `.actions-trigger` styles; `RowActionsMenu` owns them now.

#### Documentation

- `README.md`, Frontend Development section reflects the conversion config + job queue.
- `AGENTS.md`, adds "Per-row actions live in `RowActionsMenu.svelte`" pointer.

### File browser + library adoptions (Lucide, Bits UI, svelte-sonner, mode-watcher) (P4)

#### New

- **P4 file browser shipped.** `VolumeList` / `Breadcrumb` / `FileList` / `FileRow` / `ConvertPanel` / `JobsPanel` components under `src/lib/components/panels/`, all driven by the existing `fileBrowser` store. Sort, filter (extension list derived from `registry.allFilterableExts()`), paginate, shift-click range select, click-to-navigate folders, click-to-enter archives, works end-to-end. ConvertPanel + JobsPanel are placeholders for P5.
- **Lucide Svelte icons** replace every Unicode glyph placeholder. ThemeToggle finally has unambiguous Sun / Moon / Monitor icons; FileRow uses Disc / Disc3 / Gamepad2 / Archive / Folder; etc.
- **Bits UI** added as a dependency (no components in use yet, reserved for P5 DropdownMenu/ContextMenu row actions and P7 modal Dialog).
- **svelte-sonner** replaces the hand-rolled `Notification.svelte`. Call `toast.success/error/info/warning(...)` from any store; the `<Toaster />` lives in `App.svelte`. Brings rich color, swipe-to-dismiss, keyboard support, multi-toast queueing for free.
- **mode-watcher** replaces the hand-rolled theme state in `ui.svelte.js`. Cross-tab sync, system-preference tracking, `color-scheme` style management, and the dark/light class on `<html>` all delegated. The inline FOUC-prevention script in `index.html` uses the same storage key (`theme-preference`) for parity with the legacy frontend.

#### Internal

- `src/styles/tokens.css` selectors migrated from `[data-theme="light"|"dark"]` to `:root` (light defaults) + `:root.dark` (mode-watcher's class convention).
- `ui.svelte.js` deletes ~70 lines: `theme` / `systemIsDark` / `resolvedTheme` / `setTheme` / `cycleTheme` / `applyTheme` / `notification` / `notify` / `dismissNotification`. `reportConnection` rewritten to use `toast.warning` (sticky) + `toast.dismiss(id)` + `toast.success` for the SSE reconnect flow.
- `Notification.svelte` deleted.
- `registry.allSourceExts()`, `registry.allVerifyExts()`, `registry.allFilterableExts()` added so the file-list filter dropdown is now extensible by registry edit alone.
- `ConvertPanel.svelte` + `JobsPanel.svelte` extracted from `WorkArea.svelte` so P5 can fill them in without touching the layout.

#### Documentation

- `README.md`, Frontend Development section lists the adopted runtime libraries.
- `AGENTS.md`, explicit "don't reinvent these" list with import patterns for the four libraries.

### Svelte 5 frontend rebuild (P0–P3)

#### New

- **Frontend rebuilt on Svelte 5 + Vite.** The 6,096-line Preact-via-htm monolith (`static/js/app.js`) is replaced by a Svelte 5 SPA under `src/`. Per-domain class-singleton stores with `$state` class fields (jobs, fileBrowser, conversion, verification, datMatching, chdMetadata, ui), one declarative `TOOLS` array in `src/lib/tools/registry.js` that drives every tool-specific decision, design tokens (`src/styles/tokens.css`) with `light` / `dark` / `system` themes, sidebar + dashboard layout with deep-linkable hash routes, mobile drawer + `inert` focus trap, skip-to-content link and `<main>` focus management on route change, error boundary around routed views.
- **Backend SSE snapshot-on-connect.** `/api/jobs/events` now emits a one-time `snapshot` event per known job at connection time (and re-emits on every reconnect). Eliminates the hydration race that existed when clients had to do a separate `/api/jobs` fetch before subscribing. Additive change, legacy clients that don't listen for `snapshot` silently ignore it.
- **Docker `frontend-builder` stage.** New `node:lts-slim` stage runs `npm ci && npm run build`; the runtime image stays Python-only. The existing multi-arch buildx pipeline (`linux/amd64` + `linux/arm64`) works unchanged because `node:lts-slim` ships both arches and the SPA has no native deps.

#### Internal

- Legacy frontend removed: `static/js/app.js` (6,096 lines), `static/js/api.js` (874 lines), `static/css/style.css` (2,424 lines), `static/vendor/*.mjs` (Preact + htm bundles). Git history preserves them.
- New `npm run dev` / `npm run build` / `npm run preview` / `npm run lint` scripts. Vite dev server proxies `/api` and `/health` to the FastAPI sidecar.
- ESLint flat config extended with `eslint-plugin-svelte` + `svelte-eslint-parser` for `.svelte` and `.svelte.js` files.
- Per the project contract: every `.svelte` file is verified via the Svelte MCP `svelte-autofixer`.

#### Documentation

- `README.md`, new "Frontend Development" section documenting Svelte as the default.
- `ADDING_PLATFORMS_AND_TOOLS.md`, the "frontend" steps for adding a tool collapse to "one entry in `TOOLS`"; the per-file list at §3.3 is updated; §5.10 rewritten end-to-end with the new registry pattern.
- `DESIGN_tool_plugin_architecture.md`, §3.7 marked implemented; descriptor updated to match what shipped (per-tool `groups`, `defaultMode`, `glyph`, `accent`; `verifyPrefix` is the URL segment).
- `AGENTS.md`, §1 covers both prod-style and HMR dev loops.

## v3.7.8 - Optional PUID/PGID ownership remap for Unraid/home servers

### New Features

- **Added optional `PUID`/`PGID` support for container file ownership mapping.** On startup, the entrypoint now accepts `PUID`/`PGID` (default `999:999`) and remaps the internal `converter` account before launching the app. This aligns output ownership with host conventions used by Unraid and other home-server setups.
- **Safe GID fallback for common Unraid defaults.** When `PGID` points to a GID that already exists in the base image (for example `100`), the startup remap falls back from `groupmod` to `usermod -g` so `converter` is reassigned to the existing group instead of failing.
- **Privilege drop now handled by `gosu` in entrypoint.** The image installs `gosu`, keeps startup as root only long enough to perform optional remapping, then immediately re-execs as `converter`.

### Internal

- `Dockerfile` now installs `gosu` and no longer sets `USER converter` at build time.
- `entrypoint.sh` now:
  - validates `PUID`/`PGID` are numeric,
  - remaps converter UID/GID when needed,
  - preserves old behavior when env vars are unset (`999:999`),
  - drops privileges with `exec gosu converter "$0" "$@"`.

### Documentation

- Updated `README.md`, `DOCKER-COMPOSE.md`, `DEPLOYMENT.md`, and compose examples to document `PUID`/`PGID` as an optional runtime ownership setting.

---

## v3.7.7 - Cancellable metadata scan and DAT match jobs

### New Features

- **Metadata scan and DAT match jobs are now cancellable from the UI.** Both job types are "external jobs" (they bypass the conversion queue and drive their own execution), and until this release were deliberately excluded from cancellation, once started, a full-volume metadata scan or DAT match had to run to completion, even if the user realised they kicked it off on the wrong volume or wanted to free the workload lane for something else. The existing per-row **Cancel** button and the **Cancel All** button now both accept these jobs. No new endpoints, the existing `DELETE /api/jobs/{id}` and `POST /api/jobs/cancel-all` routes were widened.
- **Partial results are preserved on cancel.** Any match entries already written to `dat_matches`, or metadata/disc-id tags already extracted and committed to `chd_metadata`, stay durable. This matches the pre-existing "durable mid-run" comment in `_run_match_job`, cancelling mid-loop gives you a partial cache, not a rollback.
- **Cancel-All now cancels every kind of job.** `cancel_all_jobs()` no longer filters out external modes, and the matching three frontend guards (`handleRequestCancelAll` activeCount, `handleCancelAllJobs` per-job state map, `queuedJobsCount` / `processingJobsCount`) were removed so the button's visibility, the summary counts, and the optimistic "Cancelling..." state update all apply uniformly.

### Bug Fixes & Hardening

- **`_run_match_job` releases the match-job lock *before* its finalize awaits.** Previously the `async with _get_match_job_lock(): _active_match_job_id = None` cleanup was the last statement in the `finally` block, after both `finish_external_job` and `finish_external_job_cancelled`. If either finalize call raised (e.g. a subscriber queue throwing), `_active_match_job_id` stayed pinned to the dead job id and every subsequent match request returned HTTP 409 until process restart. The reset now runs at the top of the `finally`, so the lock is released even if finalize blows up.
- **Startup migrated from deprecated `@app.on_event("startup")` to FastAPI lifespan context manager.** Silences the `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead` the test suite was emitting, and puts us on the supported path for future FastAPI upgrades. No behaviour change, same startup body, same ordering (`configure_logging` → DB init → Alembic → JSON→SQLite import → background tasks → auto-sync).
- **"Cancelling..." string unified across backend and frontend.** The new external-job cancel path briefly used a Unicode ellipsis (`"Cancelling…"`) while the pre-existing conversion-job path and the frontend's optimistic update used ASCII (`"Cancelling..."`). Because SSE delivery races the local optimistic write, the displayed string would flip between the two forms. Backend now emits ASCII everywhere.

### Internal

- **New cancellation primitives on `JobManager`.**
    - `ExternalJobCancelled`, purpose-built exception raised inside external-job loops when `cancel_job()` has been requested. Only caught by the dedicated `except` branch in each loop; will never mask a real `Exception`.
    - `is_cancelled(job_id) -> bool`, cheap polling check used at the top of each loop iteration in `_run_match_job` and both phases of `scan_metadata_task`.
    - `get_cancel_event(job_id) -> asyncio.Event | None`, awaitable form for future use.
    - `finish_external_job_cancelled(job_id, *, message)`, terminal finalize that sets `JobStatus.CANCELLED`, clears cancel state, emits the `cancelled` SSE event, and archives the job. Symmetric with `finish_external_job(success=...)`.
- **Tri-state `job_success`** in both loops (`True` = success, `False` = failure, `None` = cancelled). Cancelled path takes `finish_external_job_cancelled`; non-cancelled path keeps the pre-existing `finish_external_job(success=...)` call so DAT match's "all files errored → mark failed" signal is unchanged.
- **Cancel events auto-register on external-job creation.** `create_external_job` now populates `_cancel_events[job_id]` when a running loop is available (falls back silently under sync test setups).

### Tests (+16, total 324)

- `test_cancel_job_signals_metadata_scan` / `..._signals_dat_match` / `..._emits_status_event_for_external_job`, cancel_job on a PROCESSING external job sets the event, flips the message, returns True, and emits a `status` SSE event.
- `test_cancel_all_includes_external_jobs`, Cancel-All requests both a metadata scan and a DAT match.
- `test_cancel_job_returns_false_for_terminal_external_job`, terminal external jobs are not re-cancellable.
- `test_finish_external_job_cancelled_sets_status_and_message` / `..._emits_cancelled_event` / `..._clears_cancel_state` / `..._noop_for_unknown_id`, terminal-finalize semantics.
- `test_run_match_job_cancellation_ends_in_cancelled_status`, mid-loop cancel flips the job to CANCELLED and clears `_active_match_job_id`.
- `test_run_match_job_cancellation_keeps_partial_cache`, per-file matches written before cancel stay in `dat_store`.
- `test_scan_metadata_cancellation_flips_to_cancelled`, Phase 1 cancel path.
- `test_scan_metadata_cancellation_during_phase2`, Phase 2 (disc-id) cancel path covered independently.
- Two pre-existing tests (`test_cancel_job_returns_false_for_metadata_scan`, `test_cancel_all_skips_metadata_scan_jobs`) were rewritten to reflect the new behaviour.

324 tests pass, 0 warnings (was 323 + 2 FastAPI deprecation warnings before this release).

### Files Changed

- `app/services/job_manager.py`, `ExternalJobCancelled`, `is_cancelled` / `get_cancel_event`, `finish_external_job_cancelled`, cancel-event registration in `create_external_job`, `cancel_job` external-job branch, `cancel_all_jobs` no longer filters external modes.
- `app/routes/dat.py`, `_run_match_job` gains a per-iteration cancel check, tri-state `job_success`, dedicated `except ExternalJobCancelled` branch, and early match-lock release in the `finally`.
- `app/routes/info.py`, `scan_metadata_task` gains cancel checks at the top of both Phase 1 and Phase 2 loops, and a cancelled branch in the `finally` that calls `finish_external_job_cancelled`.
- `app/main.py`, `@app.on_event("startup")` → `@asynccontextmanager async def lifespan(app)` passed to `FastAPI(lifespan=...)`. Startup body unchanged.
- `static/js/app.js`, Cancel button on `JobList` row is no longer suppressed for external scan modes; Cancel-All count filter, per-job state map, and `queuedJobsCount` / `processingJobsCount` no longer exclude them.

### Upgrade Notes

- **No action required.** The `DELETE /api/jobs/{id}` endpoint keeps its existing contract (returns 200 on success, 404 on unknown id); external jobs just no longer fall through to a silent `False` return.
- **Cancelling a metadata scan mid-run leaves partial metadata cached.** If you cancel and re-trigger, the re-triggered scan skips already-refreshed entries via the normal `is_stale` check, no duplicate work, no lost data.
- **Cancelling a DAT match mid-run leaves per-file matches cached.** Re-matching the same paths skips the ones already in `dat_matches`; size-cap and missing-file skips are re-evaluated.
- **"Cancel All" now cancels scans too.** If you were relying on Cancel-All stopping only conversion jobs while a metadata scan kept running, use the per-row Cancel button on the conversion jobs instead.

---

## v3.7.6 - ACL enforcement on post-sync rematch + UI signals

### Bug Fixes

- **Post-sync rematch now re-validates every path against `CONFIGURED_VOLUMES`.** v3.7.5's rematch hook snapshotted paths from `DATMatch` and handed them to `schedule_match_job` unchanged. Those paths were ACL-approved at write time, but a later tightening of `CONFIGURED_VOLUMES` (a mount unmounted, a library removed) or a symlink retargeted since the original scan would slip through undetected. `schedule_match_job` now runs every incoming path through `os.path.realpath` + `is_within_configured_volumes` and drops denied entries with a `WARNING` log (`"schedule_match_job: dropped N path(s) outside configured volumes"`). The check is idempotent, the HTTP `/dat/match-batch/job` handler pre-filters, so duplicate filtering is a no-op for the HTTP path.
- **`_background_match_tasks` set-membership invariant is now self-checking.** `schedule_match_job` logs a warning if the strong-ref set is non-empty when a new task is about to be scheduled, the concurrency guard should make this impossible, so a warning here surfaces a guard-bypass regression that would otherwise be silent.

### UX Improvements

- **Sync result / status payload carries `rematch_status` + `rematch_job_id`.** `GET /dat/sync/status` now reports whether the post-sync rematch was `scheduled` (with a `rematch_job_id` the UI can poll), `deferred` (another match job held the concurrency lock, user can retry after it completes), `failed` (scheduling raised; DAT import still succeeded), or `None` (sync had errors; no rematch attempted). Previously the UI had to infer rematch progress from log-tailing.

### Tests (+5, total 313)

- `test_schedule_match_job_returns_none_when_all_paths_denied`, empty allow-list short-circuits before job creation.
- `test_schedule_match_job_filters_denied_paths_but_schedules_remainder`, mixed allow/deny list drops the deny with a warning log, schedules the rest.
- `test_do_sync_surfaces_rematch_scheduled_status` / `..._deferred_status` / `..._failed_status`, each rematch outcome propagates into the progress payload and result dict.
- Existing `test_schedule_match_job_uses_create_task_without_background_tasks` now additionally asserts `_background_match_tasks` set membership before the task completes and cleanup afterward.

### Files Changed

- `app/routes/dat.py`, new `_filter_paths_within_volumes` helper; `schedule_match_job` runs it before claiming the concurrency slot; `_background_match_tasks` size-guard warning; inline comment explaining why scheduling lives outside the lock.
- `app/services/dat_sync.py`, `_do_sync()` captures `rematch_status` + `rematch_job_id` and surfaces them through `_update_progress` and the return dict.

### Upgrade Notes

- **No action required.** The ACL re-check is idempotent on already-admitted paths, so existing workflows are unaffected.
- **Frontend integration:** existing UI consumers of `/dat/sync/status` see two new optional keys inside `progress` (`rematch_status`, `rematch_job_id`). Old clients that ignore unknown keys keep working unchanged.

---

## v3.7.5 - Post-sync auto-rematch + hardened match-job error handling

### New Features

- **DAT re-syncs now auto-rematch previously-scanned files.** When a DAT re-sync wipes the match cache (via `_persist_sync` in `app/services/dat_store.py`), the sync now snapshots the pre-wipe path list and enqueues a background `/dat/match-batch/job` against those paths. Before this, `GET /dat/stats` would read `matched=0, scanned=0` immediately after a sync until the user manually browsed every affected directory to re-trigger the frontend's match-batch effect. On a typical install with 1500+ scanned files across a dozen volumes, that was easy to miss. Now the stats counter refreshes on its own within a few minutes of sync completion.
- **`schedule_match_job()` helper.** Extracted from the `/dat/match-batch/job` HTTP handler so non-HTTP callers (like the new post-sync hook) can reuse the same concurrency-guarded scheduling path. Works with or without a FastAPI `BackgroundTasks`, `asyncio.create_task` with a module-level strong-reference set (`_background_match_tasks`) is used for non-HTTP callers so the task can't be garbage-collected mid-run.

### Bug Fixes & Hardening

- **Failure counting in `_run_match_job`.** Previously a dropped volume produced a misleading `"complete, 0 matched"` signal, every per-file OSError was WARN-logged and the job finished green. Two new counters (`errors` + `skips`) now track real failures vs. policy skips. When `errors == total` the job flips to `failed` with a `"check volume accessibility"` error message so operators see a red signal instead of a DAT-coverage-gap illusion.
- **Mid-loop exception preserves counter context.** When the outer `except Exception` in `_run_match_job` fires (e.g., `job_manager.update_external_job` starts throwing), the error message now includes `processed N/M, K error(s)` so operators know how far the job got before failure.
- **Programmer-error tracebacks surface.** Two broad `except Exception` blocks in `_hash_one_for_job` and `_run_match_job`'s cache-write path now use `logger.exception` (ERROR level with full traceback) rather than `logger.warning`. A `KeyError`/`AttributeError` from a refactor bug is visible in logs instead of buried as a one-line warning.
- **Background match-task exceptions surface too.** If `_run_match_job` raises past its own `try/finally` (rare, but possible if `finish_external_job` throws), the exception is now routed through the project logger instead of only appearing as asyncio's "Task exception was never retrieved" warning on GC.
- **`list_match_paths` failure no longer leaks staged DATs.** If the snapshot SELECT raised (DB locked, disk error), the sync would abort before `persist()` committed, leaving DATs staged in `_pending_imports`, the next sync would double-stage and corrupt the store. Snapshot failure is now best-effort: empty list + `logger.exception`, and `persist()` still runs so the DAT import is durable.
- **Rematch `ImportError` no longer silently swallowed.** The rematch scheduling's `try/except Exception` would hide a missing-module deployment bug as a silent "complete" sync. `from routes.dat import schedule_match_job` is now outside the try/except, a real ImportError propagates and fails the sync loudly so operators notice.
- **`schedule_match_job` scheduling failure rolls back `_active_match_job_id`.** If `asyncio.create_task` or `background_tasks.add_task` raises after the lock was claimed, future match jobs would return HTTP 409 forever. The rollback clears the id and finalizes the phantom external job before re-raising.
- **Canonical internal imports.** Removed three `try: from services.x / except ImportError: from app.services.x` fallback pairs in `app/services/dat_sync.py`. Under the project's `PYTHONPATH=app` convention the fallback path was unreachable, and the `except ImportError` would silently absorb a real circular-import bug if one ever arose. Production-only feature-gate imports in `archive.py` / `disc_id.py` are untouched.

### Tests

- `test_do_sync_schedules_rematch_after_success` / `..._when_no_previous_matches` / `..._on_partial_failure` / `..._logs_when_skipped_due_to_active_job`, post-sync rematch hook.
- `test_do_sync_list_match_paths_failure_is_best_effort`, snapshot SELECT failure is handled.
- `test_do_sync_rematch_schedule_exception_does_not_poison_sync`, rematch failure doesn't mark sync as errored.
- `test_run_match_job_reports_errors_in_final_message` / `..._marks_failure_when_all_files_error` / `..._skip_count_does_not_trip_failure` / `..._cache_write_failure_counts_as_error` / `..._outer_exception_includes_counter_context`, failure-counter coverage.
- `test_hash_one_for_job_logs_match_error_with_traceback`, programmer-error tracebacks land at ERROR level.
- `test_schedule_match_job_returns_none_on_empty_paths` / `..._returns_none_when_active` / `..._rolls_back_active_id_on_scheduling_failure` / `..._uses_create_task_without_background_tasks`, helper behavior.
- `test_list_match_paths_returns_empty_on_fresh_store` / `..._returns_all_paths`, snapshot query.

308 tests pass (+14 new since v3.7.3).

### Files Changed

- `app/routes/dat.py`, `schedule_match_job` helper + `_log_background_match_task_error` callback; `_run_match_job` refactored for error/skip counting, `logger.exception` for broad catches, counter context in outer-except message.
- `app/services/dat_store.py`, `has_stale_dats()` and `list_match_paths()` helpers.
- `app/services/dat_sync.py`, `_do_sync()` now snapshots match paths (best-effort) and schedules rematch after successful commit; three `except ImportError` fallbacks removed.
- `app/main.py`, startup lifespan now auto-triggers self-heal sync when any DAT has `file_count=0` (shipped in v3.7.3).

### Upgrade Notes

- **No action required.** After restart, if your DAT store has stale rows they self-heal (v3.7.3 behavior), and if that sync runs it now auto-rematches your scanned files. If a match job is already active when the post-sync hook fires, the rematch is skipped with a log line and you can re-trigger manually via `POST /dat/sync {"force": true}`.
- **Jobs panel:** after a sync you'll see a `"DAT Match"` external job tick through your previously-scanned files. If a volume is offline while it runs, the job now finishes with `failed` + `"all N file(s) failed, check volume accessibility"` instead of a misleading `"0 matched"` success.

---

## v3.7.3 - DAT self-heal fires automatically on startup

### Bug Fixes

- **Stale DATs now repair themselves on server restart.** v3.7.2 taught `DATSyncService` to skip its `already_synced` fast-path whenever any DAT had `file_count=0`, but the self-heal only ran when `sync()` was explicitly called, via the UI sync button or `POST /api/dat/sync`. Users who had never clicked sync stayed stuck with 67 empty DATs (the classic pre-#49 state: every CD/softlist `<disk>` DAT showing 0 entries while Nintendo GameCube + Wii, the two `<rom>`-based ones, were healthy). Startup auto-sync previously gated on `MAMEREDUMP_AUTO_SYNC=true` AND an empty DAT store, so it never fired for someone who already had 69 DATs sitting in broken state.

  Startup now has a second trigger: whenever any DAT has `file_count=0`, a background sync kicks off unconditionally (no env-var gate, no empty-store gate). The service's existing self-heal logic then rebuilds the DAT set in place using the current parser. Fresh-install behaviour (`MAMEREDUMP_AUTO_SYNC=true` + empty store) is unchanged.

### Tests

- `test_has_stale_dats_empty_store_returns_false` / `_all_healthy_returns_false` / `_detects_zero_count`, unit tests for the new `DATStore.has_stale_dats()` existence check.

### Files Changed

- `app/services/dat_store.py`, new `has_stale_dats()` method (single-row SQL existence check, cheap enough for startup).
- `app/main.py`, startup auto-sync split into two independent triggers; shared task-spawn helper extracted.
- `tests/test_db_store_operations.py`, three new tests for `has_stale_dats()`.

### Upgrade Notes

- **No action required.** Restart the server once and stale DATs heal within ~60 s (given network access to the MAMERedump repo). If GitHub is unreachable the sync errors into the log and state is preserved, identical to the existing manual-sync behaviour.

---

## v3.7.2 - Self-healing DAT re-sync after the softlist &lt;disk&gt; parser fix

### Bug Fixes

- **DATs with only `<disk>` hash entries no longer stay stuck at 0 entries after an upgrade.** The v3.7.0 parser (PR #49) added `<disk>` support so MAME-Redump softlist DATs for disc-based systems (Amiga CD / CD32 / CDTV, Bandai Pippin, Konami FireBeat / System 573, Sega Naomi / Chihiro / Lindbergh, Atari Jaguar CD, IBM PC, etc.) import correctly. However, anyone who ran the DAT sync *before* that parser change ended up with DAT rows whose `file_count=0` because the old parser silently ignored `<disk>` elements, and the `DATSyncService` treated the (cached, correct) tag as "already synced", trapping users in the stale state.

  The sync service now self-heals: if `last_sync_tag` matches but any existing DAT has `file_count=0`, the fast-path is skipped and the DAT set is rebuilt in place. The old DATs are only deleted after the new set is fully imported, so there's never a window with zero DATs.

### New Features

- **`POST /api/dat/sync` accepts `"force": true`**, bypasses the `already_synced` fast-path unconditionally, useful when operators want to rebuild the DAT set for any reason (repo-side fixes, local DB corruption, etc.). Default remains `false`.

### Tests

- `test_sync_auto_forces_when_any_dat_has_zero_entries`, regression test for the self-heal path.
- `test_sync_force_bypasses_already_synced_guard`, covers the explicit `force=True` opt-in.
- `test_sync_already_synced_requires_nonzero_file_counts`, the fast-path still fires when every DAT is healthy.

### Files Changed

- `app/routes/dat.py`, `SyncRequest` gains `force: bool = False`; route forwards it to the service.
- `app/services/dat_sync.py`, `sync()` / `_do_sync()` take `force`; the fast-path skips when any DAT has `file_count=0`.
- `tests/test_dat_sync.py`, `tests/test_dat_routes.py`, new regressions + existing assertions updated for the new kwarg.

### Upgrade Notes

- **No action required.** On next sync click the service notices the stale state and rebuilds automatically. If you prefer to trigger it explicitly, POST `/api/dat/sync` with `{"force": true}`.

---

## v3.7.1 - Color-coded stdout logs

### New Features

- **ANSI-colored log levels on stdout**, Log lines now highlight `%(levelname)s` with an SGR color so severity is easy to skim in `docker logs` output: `DEBUG` dim, `INFO` green, `WARNING` yellow, `ERROR` red, `CRITICAL` bold red. Message bodies are left uncolored so copy-paste stays clean.
- **`LOG_COLOR` env var**, Controls the stream-handler color policy. Values:
  - `always` (**default**), colored stdout out of the box, so Docker users see colors without any configuration. `docker logs` does not allocate a TTY, so this was the right default rather than `auto`.
  - `auto`, color iff stdout is a TTY and the `NO_COLOR` env var (<https://no-color.org>) is unset.
  - `never`, disable entirely.
  Invalid values log a warning and fall back to `auto`.
- **File logs are never colored**, `LOG_PATH` output always uses the plain formatter regardless of `LOG_COLOR`, keeping `grep` / log-aggregator pipelines intact.

### Files Changed

- `app/main.py`, added `ColorFormatter`, `_resolve_color()`, wired into `configure_logging()`.
- `app/config.py`, added `log_color: str` setting.
- `README.md`, `LOG_COLOR` row in the env-var table.
- `tests/test_logging_config.py`, +14 tests: default value, resolver TTY/`NO_COLOR` logic, formatter round-trip, end-to-end colored stream, and a guarantee that the file handler never emits ANSI escapes even with `LOG_COLOR=always`.

---

## v3.7.0 - Unified SQLite Store, Alembic Migrations, and Wider DAT Matching

### New Features

- **Unified SQLite persistence layer**, The four legacy JSON stores (`dat_store.json`, `chd_metadata.json`, `verified_chds.json`, `dat_sync.json`) are consolidated into a single `compressatorium.db` SQLite file backed by SQLAlchemy 2.0. Tuned for this workload: WAL journal mode, `synchronous=NORMAL`, `foreign_keys=ON`, 30s `busy_timeout`. Composite primary keys let sha1/md5 coexist; foreign keys give cascade-on-DAT-delete for hashes and set-null semantics for cached matches so UI history survives DAT removal.
- **One-shot JSON → SQLite migration on startup**, On first boot against the new release, legacy JSON stores are imported into the DB and the source files are renamed to `*.migrated.bak` (never deleted). Migration is isolated per store: a failure in one does not block the others, leaves that store's JSON untouched, rolls the transaction back, and retries on the next startup. Corrupt JSON is renamed to `*.corrupt` and logged loudly.
- **Alembic schema versioning**, Schema is driven by Alembic migrations (`migrations/versions/0001_baseline_schema.py`) rather than `create_all`. Pre-Alembic databases (anyone who ran the SQLite-migration build before Alembic landed) are detected by baseline-table presence and auto-stamped to `0001` before upgrade, existing rows are preserved untouched.
- **DAT match badge widened to raw disc images with bounded hashing**, The match badge now evaluates raw `.iso`/`.bin`/`.cue`/`.gdi` sources in addition to CHDs, with a bounded hashing window so large images can't stall the scan. Softlist `<disk>` elements are parsed as part of DAT matching, and matches run as a background job so the UI stays responsive.
- **`:beta` Docker tag for pre-releases**, Pre-release GitHub releases now publish a moving `:beta` tag (in addition to the semver tag) on both Docker Hub and GHCR; stable releases continue to publish `:latest`. Consumers opting in to betas pull `pacnpal/compressatorium:beta`. See the README "Opting in to beta updates" section for the data-loss warning, running a beta against a production data volume can irreversibly migrate the DB, and downgrading to `:latest` afterwards is not supported.

### Bug Fixes

- **DAT match no longer leaks `OSError` internals to clients** (#56), `_match_single_file` previously surfaced filesystem error detail (paths, errno strings) in the API response. It now returns a generic failure code and logs the detail server-side.

### Tests

A full test suite has been added around the new SQLite feature, 26 new tests across 5 files covering every documented invariant end-to-end:

- **`tests/test_db_engine_pragmas.py`**, verifies WAL, `synchronous=NORMAL`, `busy_timeout ≥ 30s`, and `foreign_keys=ON` are actually applied on every checked-out connection (SQLite PRAGMAs are per-connection), and that FK enforcement raises `IntegrityError` on orphaned inserts.
- **`tests/test_db_startup_integration.py`**, replays the exact `startup_event` ordering: fresh disk, full legacy migration, pre-Alembic + JSON coexistence, and cross-restart idempotency.
- **`tests/test_db_store_operations.py`**, DAT cascade-delete wipes hashes; DAT delete sets cached match `dat_id` to `NULL` (not delete); `_set_matches_batch_sync` round-trips at the 899/900/901/1800 boundary against SQLite's 999-bind-parameter limit; `VerificationStore._mark_sync` is idempotent; CHDMetadata JSON column survives unicode, nested structures, and emoji; `DATSyncState` singleton never duplicates.
- **`tests/test_db_migration_edge_cases.py`**, backup-filename collision preserves the source JSON, mid-migration rollback preserves JSON and doesn't block other stores, unicode + ~1800-char paths round-trip, empty collections migrate cleanly, orphaned match `dat_id` becomes `NULL` rather than failing, and truncated JSON bodies land as `.corrupt` (not `.migrated.bak`).
- **`tests/test_verification_store.py`**, added a concurrent-writer test (two asyncio tasks × 200 writes each) proving WAL + busy_timeout end-to-end through `mark_verified`.
- **`tests/test_db_migration.py` and `tests/test_alembic_migrations.py`** (introduced during the beta cycle), cover the five no-data-loss migration invariants and the five Alembic invariants (fresh DB, pre-Alembic stamp-then-upgrade, idempotency, ORM drift guard, called-before-init guard).

Total suite: 270 tests, all green.

### CI / Infrastructure

- **Docker workflow**, `docker-image.yml` now emits `:beta` for pre-releases and `:latest` only for stable releases. Semver tags (`X.Y.Z`, `X.Y.Z-beta-N`, `X.Y`, `X` for non-v0.x) and `sha-<short>` tags continue to be emitted for all releases.
- **Code scanning noise reduced**, Project-wide linter configs expanded, Codacy bandit engine disabled, and remaining flagged false positives silenced with inline rationale.
- **Dependabot**, bumped `actions/stale` 5→10, `docker/build-push-action` 7.0.0→7.1.0, `docker/login-action` 3.7.0→4.1.0, `actions/labeler` 4.3.0→6.0.1, `actions/attest-build-provenance`, and `globals` 17.4.0→17.5.0.

### Files Added / Changed (high-level)

- `app/services/db.py` (new), engine init, PRAGMA hook, Alembic bootstrap (`apply_migrations`), `init_and_migrate`, per-store migrators.
- `app/services/dat_store.py`, `verification_store.py`, `chd_metadata_store.py`, `dat_sync.py`, re-pointed at the SQLAlchemy session factory; chunked upserts where bulk operations cross SQLite's 999-bind-parameter limit.
- `migrations/` (new), `alembic.ini`, `env.py`, `versions/0001_baseline_schema.py`.
- `app/main.py`, `startup_event` initialises the DB, runs Alembic, then `init_and_migrate` before any store is touched.
- `.github/workflows/docker-image.yml`, `:beta` tag for pre-releases.
- `README.md`, "Available Tags" table expanded with `:beta` and pre-release semver entries; new "Opting in to beta updates" section with data-loss warning.
- `tests/`, 5 new DB test modules plus shared `conftest.py` with sample JSON payloads and the `reset_db_engine` fixture.

### Upgrade Notes

- **Back up your `data_dir`** before upgrading, especially if you have a large `dat_store.json`, this is a one-way migration. The legacy JSON is preserved as `*.migrated.bak` on successful import, but the new DB is the source of truth after restart.
- **Downgrading to v3.6.x** after `compressatorium.db` has accepted writes is not supported. If you need to roll back, restore the `.migrated.bak` files (remove the suffix) and delete `compressatorium.db` before starting the older image.
- **Read-only `/config` volumes**, the DB falls back to `$TMPDIR/compressatorium/compressatorium.db` if `data_dir` is unwritable, matching the legacy JSON-store fallback. Set `CHD_DB_PATH` explicitly if you want a different location.

---

## v3.3.2 - CD CHD Sector Extraction Fix

### Bug Fixes

- **PS1 serial extraction from CD CHDs**, `_extract_from_chd_sectors` previously returned `None` for all CD-based CHDs (unit_bytes=2352/2448), meaning PS1 game serials could only be extracted from companion `.bin`/`.cue` files or if those CHDs were already tagged at conversion time. The function now probes both **Mode 2 Form 1** (24-byte header) and **Mode 1** (16-byte header) sector framing when reading 2352- or 2448-byte CD sectors, allowing it to walk the embedded ISO 9660 filesystem directly, exactly as libchdr-based emulators (PCSX2, AetherSX2, NetherSX2) do. For CHDs created from Mode 2 Form 1 BIN/CUE images (the most common PS1 format), extraction now succeeds without requiring a companion source file.

### Tests

- Added `test_extract_from_chd_sectors_ps1_cd_mode1`, verifies PS1 serial extraction from a CD CHD with 2352-byte Mode 1 sectors.
- Added `test_extract_from_chd_sectors_ps1_cd_mode2`, verifies PS1 serial extraction from a CD CHD with 2352-byte Mode 2 Form 1 sectors.
- Added `test_extract_from_chd_sectors_cd_no_recognizable_content`, confirms that a CD CHD with no ISO 9660 filesystem still returns `None` gracefully.
- Replaced the now-obsolete `test_extract_from_chd_sectors_non_dvd_returns_none` test (which verified the old early-return shortcut) with the three new tests above.

### Files Changed

- `app/services/disc_id.py`, `_extract_from_chd_sectors` extended to handle CD CHDs; docstrings updated throughout
- `tests/test_disc_id.py`, Three new CD CHD extraction tests; module docstring updated

---

## v3.3.1 - Structured Logging with LOGLEVEL

### New Features

- **`LOGLEVEL` environment variable**, Replaces the removed `CHD_DEBUG` flag. Set to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` to control log verbosity. Defaults to `INFO` so useful operational logs are visible out of the box without any configuration. Legacy `CHD_DEBUG=true` is still honoured and maps to `LOGLEVEL=DEBUG` for backwards compatibility.
- **`LOG_PATH`**, Replaces the removed `CHD_DEBUG_LOG_PATH`. Optionally write logs to a file in addition to stdout, at any log level (not just debug). Legacy `CHD_DEBUG_LOG_PATH` is still read for backwards compatibility.

### Enhanced Metadata Scan Logging

Metadata scans now emit detailed, structured `INFO`-level logs covering every stage, so users can fully understand what the application is doing by inspecting logs:

- **Scan start**, Logs `force` mode, number of volumes, and their paths.
- **Discovery**, Logs how many CHD files were found across how many volumes; warns on any missing/inaccessible volume.
- **Phase 1 (metadata extraction)**, Logs each file with a `[n/total]` counter as its `chdman info` metadata is extracted and cached. Logs success per file; warns on failure.
- **Phase 1 summary**, Reports how many files were refreshed vs already up-to-date.
- **Phase 2 (disc ID tagging)**, Logs when each previously-unchecked CHD is scanned for a GAME/NAME tag. When a disc ID is successfully embedded into the CHD file, logs the filename and extracted `game_id`. When no disc ID can be found, logs that the file was marked as checked.
- **Phase 2 summary**, Reports already-checked, newly-checked, and embedded tag counts.
- **Flush**, Logs when the metadata store is being persisted and when it completes.
- **Final summary**, Reports total metadata refreshed, disc IDs embedded, and total elapsed time.

Disc ID embedding operations inside `disc_id.py` (strategies 2–4) are also promoted from `DEBUG` to `INFO` so the source of each embedded tag (CHD sectors, GDRO, or companion file) is visible in normal operation.

### Internal Changes

- The background maintenance loop (stuck-job detection, stale lock cleanup) now always runs, not only when `LOGLEVEL=DEBUG`. It was previously gated behind `CHD_DEBUG=true`, meaning stuck-job recovery silently didn't operate in production.

### Files Changed

- `app/config.py`, Removed `debug`/`CHD_DEBUG`; added `log_level`/`LOGLEVEL` (default `INFO`) and `log_path`/`LOG_PATH`
- `app/main.py`, `configure_logging()` parses `LOGLEVEL` instead of the old boolean flag
- `app/services/job_manager.py`, Maintenance loop always starts; removed `settings.debug` gate
- `app/routes/info.py`, Detailed structured INFO logging throughout `scan_metadata_task`
- `app/services/disc_id.py`, Embedding log calls promoted from `DEBUG` to `INFO`
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md`, Updated env var tables

---

## v3.3.0 - Game ID & Title Extraction for CD and DVD CHDs

### New Features

- **Game ID & Title in CHD Inspector**, PS1, PS2, PSP, and Dreamcast game serials are extracted from CHD sector data (via `SYSTEM.CNF`, `PARAM.SFO`, and `IP.BIN`) and displayed in the CHD info modal. Human-readable titles (e.g. "Patapon", "DEAD OR ALIVE 2") are shown when available; the serial is used as a fallback title.
- **CHD metadata tagging at conversion time**, When a CD or DVD CHD is created, the game serial is embedded as a `GAME` tag and the title as a `NAME` tag inside the CHD file itself, making it readable by emulator frontends and database scrapers.
- **Retroactive game ID tagging**, The metadata scan (Phase 2) now loops over all CHDs and embeds `GAME`/`NAME` tags into any file that doesn't have them yet. Already-tagged and previously-scanned files are skipped efficiently.
- **Persistent disc-ID cache**, Extracted game IDs and titles are stored in the metadata cache (`chd_metadata.json`). The `/api/info` endpoint reads from the cache first; `chdman dumpmeta` subprocesses are only spawned on a cache miss, and "nothing found" results are also cached to prevent repeated subprocess calls for unsupported discs.

### Implementation Details

- `app/services/disc_id.py`, New service implementing:
  - `_CHDReader`, minimal CHD v5 sector reader (ZLIB, LZMA, ZSTD, NONE, MINI compression)
  - `extract_from_source()`, PS1/PS2 `.iso`/`.bin`/`.cue`/`.gdi` serial extractor
  - `extract_from_chd()`, four-strategy extractor (embedded GAME tag → sector read → Dreamcast GDRO → companion source files)
  - `ensure_disc_id_embedded()`, retroactive CHD tagger called during metadata scan
- `app/services/chd_metadata_store.py`, Added `get_disc_id_info()`, `update_disc_id_info()`, `is_disc_id_checked()`, and `mark_disc_id_checked()`; `set_metadata()` now preserves `game_id`, `title`, `disc_id_checked`, and `disc_id_checked_mtime` during Phase 1 metadata refreshes
- `app/routes/info.py`, `/api/info` uses the metadata cache; CHDs already scanned (Phase 2 or prior `/api/info` call) with no game ID skip re-extraction
- `app/models.py`, `CHDInfo` model extended with optional `game_id` and `title` fields
- `static/js/app.js`, CHD info modal shows "Game ID" and "Title" rows when values are present

### Tests

- 12 new tests covering `_CHDReader` MINI hunk decoding, `_extract_cue` (including missing-file fallback and multi-file fallback), source-file extraction, CHD sector extraction, metadata store disc-ID methods, and retroactive embedding

### Files Changed

- `app/services/disc_id.py`, New service (source extractor + CHD reader + retroactive tagger)
- `app/services/job_manager.py`, Embeds `GAME`/`NAME` tags at conversion time
- `app/services/chd_metadata_store.py`, Disc-ID cache methods; `set_metadata()` field preservation
- `app/routes/info.py`, `/api/info` disc-ID cache lookup + Phase 2 retroactive tagging
- `app/models.py`, `CHDInfo.game_id` / `CHDInfo.title` fields
- `static/js/app.js`, Game ID and Title rows in CHD info modal
- `tests/test_disc_id.py`, Full disc-ID extraction tests
- `tests/test_metadata.py`, Metadata store disc-ID caching tests

---

## v3.2.3 - Batched Notifications, Deferred UI Updates & Job Index

### UI / UX

- **Batched terminal notifications** - Completed, failed, and cancelled job notifications are now aggregated per flush cycle. Instead of one toast per job, a single summary toast is shown (e.g. "Completed 5 jobs", "2 jobs failed"). Individual filenames are still shown when only one job completes.
- **Batched verified-CHD set updates** - `setVerifiedCHDs` is called once per flush with all added/removed paths collected during the batch, eliminating per-job Set cloning.
- **Deferred UI updates during dropdown interaction** - A `deferJobUiUpdatesRef` flag pauses job-driven React re-renders while a `<select>` dropdown (mode, filter, page-size) is focused or has its menu open. This prevents the dropdown from closing mid-selection when an SSE update or poll cycle triggers a state change. Dropdowns set the flag on `focus`/`mousedown` and clear it on `blur`/`change`.
- **Capped placeholder rows** - Optimistic "creating" placeholders are capped at `MAX_VISIBLE_CREATING_PLACEHOLDERS` (100). For larger batches, remaining jobs are counted but not rendered, with an info toast showing the total queued count.

### Reliability & Performance

- **Job index map** - `applyQueuedJobUpdates` now builds a lazy `Map<jobId, index>` via `ensureJobIndex()` on first lookup, replacing O(n) `findIndex` per update with O(1) map lookups. The index is maintained as jobs are inserted or replaced.
- **Extracted `applyPolledJobs` helper** - The poll-interval and initial-fetch merge logic is deduplicated into a single `applyPolledJobs(serverJobs)` function. It also checks `deferJobUiUpdatesRef` to skip state updates while a dropdown is open.
- **Stuck-state polling guard** - `checkStuckStatus` responses are silently discarded when `deferJobUiUpdatesRef` is active, preventing spurious stuck-state banner flickers during dropdown interaction.
- **New-job insertion order** - Hydrated jobs arriving via SSE for unknown IDs are now appended (`push`) instead of prepended (`unshift`), maintaining chronological order and avoiding unnecessary array shifts.

### Files Changed

- `static/js/app.js` - All changes above: batched notifications, deferred UI flag, `ensureJobIndex`, `applyPolledJobs`, placeholder cap, dropdown `onFocus`/`onBlur`/`onMouseDown` handlers

---

## v3.2.2 - Search View Snapshot & Auto-Return

### New Features

- **Search view snapshot / restore** - Before "Search All" runs, the current file-list state (entries, archive path, selection, page) is captured. The "← File List" button restores this snapshot exactly, preserving scroll position context instead of re-fetching the directory.
- **Auto-return to file list** - After a successful conversion from search results, the UI automatically restores the pre-search file-list view when `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` is `true` (default). The setting is served from `/api/version` and respected by the frontend at runtime.
- **New config setting** - `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` (default `true`) with legacy alias `CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST` via Pydantic `AliasChoices`.

### Bug Fixes

- **Conversion return values** - `executeConversion` and `maybeConfirmDeletePlan` now return `boolean` success indicators, enabling callers to decide post-conversion behavior (e.g. auto-return).
- **Snapshot invalidation** - Pre-search snapshot is cleared on volume switch and directory navigation so stale state is never restored.

### Documentation

- **README** - Added `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` and legacy alias to env var table.
- **DEPLOYMENT.md** - Added new env var to deployment reference.
- **DOCKER-COMPOSE.md** - Added new env var to compose reference.

### Tests

- **Search auto-return config** - New `test_search_auto_return_default_and_legacy_alias` verifies the setting defaults to `true` and respects the legacy `CHD_` alias.

### Files Changed

- `app/config.py` - `search_auto_return_to_file_list` field with `AliasChoices`
- `app/routes/info.py` - `/api/version` response includes `search_auto_return_to_file_list`
- `static/js/app.js` - `capturePreSearchView` / `restorePreSearchView`, auto-return logic, boolean conversion returns, "← File List" button
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` - Env var docs
- `tests/test_volume_discovery.py` - Config default + legacy alias test

---

## v3.2.1 - CI Release Notes from Commit Log

### CI / CD

- **Auto-generated release body** - The `create-release` job now checks out the repo with full history and tags, derives the previous semver tag, and builds a changelog from `git log --no-merges` between the two tags. The result is written to `RELEASE_BODY.md` and passed via `body_path` instead of relying on GitHub's `generate_release_notes`.
- **Previous-tag derivation** - New `previous_tag` output in the `release_meta` step finds the most recent `v*.*.*` tag excluding the current one, enabling accurate commit-range changelogs.
- **Full changelog link** - The generated release body includes a GitHub compare URL (`previous_tag...current_tag`) for easy diff browsing.
- **Fallback handling** - If no previous tag exists (first release) or no non-merge commits are found, sensible defaults are emitted instead of an empty body.

### Files Changed

- `.github/workflows/docker-image.yml` - Added checkout step with `fetch-depth: 0` and `fetch-tags: true`, commit-log changelog generation, `body_path` release body

---

## v3.2.0 - Job Tabs & Queue Pagination

### New Features

- **Job tabs** - The jobs panel is now split into three tabs: **Queue** (queued + processing + creating), **Completed**, and **Failed/Cancelled**. Each tab shows its own count. The "Failed/Cancelled" tab auto-hides when empty and auto-switches back to Queue if emptied while active.
- **Job pagination** - Jobs within each tab are paginated with configurable page-size (10 / 25 / 50 / 100 / All), page navigation buttons, and a "Showing X–Y of Z" summary. Current page is clamped when the list shrinks.

### UI / UX

- **Tab bar styling** - Pill-shaped tab buttons with uppercase labels, accent-color active state, hover highlights, and full-width equal-sizing on mobile (≤768 px).
- **Job header count** - The "Jobs" heading now displays the combined total across all tabs rather than the raw `jobs.length`.
- **Empty-state messaging** - Each tab shows contextual empty titles and help text (e.g. "Select files and click Convert" for Queue, "Successfully completed jobs will appear here" for Completed).

### Files Changed

- `static/js/app.js` - Job tab state, `displayedJobs` / `queueJobs` / `completedJobs` / `issueJobs` memos, `jobsPagination` / `paginatedJobs` computed values, tab bar and pagination controls, contextual empty-state props
- `static/css/style.css` - `.job-tabs`, `.job-tab`, `.job-tab.active`, `.job-tab:hover` styles + mobile responsive rules

---

## v3.1.1 - SSE Batching, Verify Concurrency & Test Coverage

### Bug Fixes

- **Verify concurrency default** - `MAX_VERIFY_CONCURRENCY` default changed from `2` to `1` to match serial-first processing policy, preventing unexpected parallel verify workloads on low-resource hosts.

### UI / UX

- **SSE job-update batching** - Rapid Server-Sent Events are now coalesced in a micro-batch window (`JOB_UPDATE_BATCH_WINDOW_MS`) before applying to React state. Only the latest update per job is kept, drastically reducing re-renders during high-throughput conversions.
- **Lazy state cloning** - `setJobs` updater uses an `ensureMutable()` helper that clones the jobs array at most once per flush, avoiding unnecessary allocations when no fields actually changed.
- **Flush-on-terminal** - Terminal events (`complete`, `error`, `cancelled`) force an immediate flush of the pending batch so the UI reflects final state without waiting for the batch timer.

### Documentation

- **README** - Updated `MAX_VERIFY_CONCURRENCY` default from `2` to `1` in the environment variable table.

### Tests

- **Serial concurrency defaults** - New `test_concurrency_defaults_are_serial` in `tests/test_volume_discovery.py` asserts both `max_concurrent_jobs` and `max_verify_concurrency` default to `1`.

### Files Changed

- `app/config.py` - `max_verify_concurrency` default `2` → `1`
- `static/js/app.js` - SSE micro-batching, lazy array cloning, flush-on-terminal
- `README.md` - Env var table correction
- `tests/test_volume_discovery.py` - Serial-default assertion test

---

## v3.1.0 - Volume Discovery, Queue Controls & UI Refresh

### New Features

- **Automatic volume discovery** - When `COMPRESSATORIUM_VOLUMES` is not set, the app discovers mounted libraries from `COMPRESSATORIUM_MOUNT_ROOT/*` at startup. Mount-point children are preferred over plain directories; results are cached for stable runtime behavior.
- **Queue-wide cancellation** - New `POST /api/jobs/cancel-all` endpoint with `X-CHD-Action-Confirm: cancel-all-jobs` header guard. A confirmation modal in the UI prevents accidental bulk cancellation.
- **Clear completed jobs** - New `DELETE /api/jobs/completed` endpoint with `X-CHD-Action-Confirm: clear-completed-jobs` header guard. Adds a "Clear Done" button with a confirmation modal showing the count of jobs to remove.
- **Entrypoint volume discovery** - `entrypoint.sh` now runs the same discover-volumes logic at container startup and logs whether volumes are explicit or auto-discovered, with proper comma-delimited iteration and whitespace trimming.

### Bug Fixes

- **Serial queue enforcement** - When `MAX_CONCURRENT_JOBS=1`, the dispatcher now awaits `_run_job()` inline instead of spawning a detached task, preventing race-condition parallel processing.
- **Serial verify default** - `MAX_VERIFY_CONCURRENCY` now defaults to `1` so verification workloads are single-lane unless explicitly increased.
- **Concurrency invariant logging** - `_run_job()` now logs an error if the count of processing jobs exceeds `max_concurrent`, providing fast detection of scheduling bugs.
- **Job lookup continuity** - Recently deleted jobs are archived in memory (TTL 15 min, max 2000 entries) so frontend polling does not immediately 404 after a job is cleared. Archive timestamps refresh on access.
- **z3ds cancellation propagation** - `z3ds_compress.convert()` now catches and re-raises `ConversionCancelled` before the generic `Exception` handler, ensuring clean cancellation logging.
- **Progress update deduplication** - SSE event handler skips `setJobs` when all tracked fields are identical, eliminating unnecessary React re-renders.
- **File-list auto-refresh paused during work** - Auto-refresh interval is suppressed while any jobs are creating, queued, or processing, preventing disruptive list flickers mid-batch.

### UI / UX

- **Header and footer logo** - `logo.png` is displayed in the header alongside the title and in the app footer.
- **Favicon** - `index.html` now uses `favicon.ico` from `/static/images/`.
- **Cache-busting** - `app.js` is loaded with a `?v=<version>` query parameter sourced from the backend, and the index response sets `Cache-Control: no-store` to avoid stale JS after deploys.
- **Completion refresh debounce** - File-list refresh after job completion is debounced to reduce churn during rapid batch completions.
- **Progress render throttle** - Per-job progress rendering is throttled so the UI stays responsive during fast SSE bursts.
- **Stale progress ref cleanup** - `progressRenderAtRef` entries are pruned on terminal events and during `mergeJobs`, preventing memory leaks from long sessions.
- **Clear Done confirmation modal** - New `ClearDoneModal` component shows job count and guards accidental clears. Button shows a spinner (`clearingCompletedJobs` state) during the API call.

### Reliability & Maintenance

- **Environment variable standardization** - Preferred env names are `COMPRESSATORIUM_MOUNT_ROOT` and `COMPRESSATORIUM_VOLUMES`. Legacy `CHD_MOUNT_ROOT` / `CHD_VOLUMES` remain supported via Pydantic `AliasChoices`.
- **Startup volume caching** - `Settings.scan_data_mounts_on_startup()` snapshots discovered volumes once at boot so the runtime volume list is stable even if mount points change later.
- **Data mount root config** - New `data_mount_root` setting (default `/data`) controls where the auto-discovery scan looks.

### Documentation

- **README** - Updated batch conversion docs, added cancel-all / clear-done mentions, documented confirmation headers on destructive API actions, added new 3DS verify endpoints, updated environment variable table with `COMPRESSATORIUM_*` names and legacy aliases, updated example docker-compose snippet.
- **DEPLOYMENT.md** - Refreshed for new environment variable names.
- **DOCKER-COMPOSE.md** - Refreshed for new environment variable names.
- **AGENTS.md** - New agent runbook covering dev, test, Docker, queue API, version sync, and CI workflows.
- **Docker Compose files** - All three compose files (`docker-compose.yml`, `docker-compose.multi-volume.yml`, `docker-compose.cli.yml`) updated from `CHD_VOLUMES` to `COMPRESSATORIUM_MOUNT_ROOT=/data`.
- **run_dev.sh** - Updated to use `COMPRESSATORIUM_MOUNT_ROOT` / `COMPRESSATORIUM_VOLUMES` with fallback to legacy names.

### Tests

- **Volume discovery** - New `tests/test_volume_discovery.py` covering explicit volumes, auto-discovery from children, and startup cache stability.
- **Cancel-all confirmation header** - Test that `cancel_all_jobs` rejects requests missing the confirmation header with `400`.
- **Clear-completed confirmation header** - Test that `delete_completed_jobs` rejects requests missing the confirmation header with `400`.
- **Archived job lookup** - Tests for archive retrieval after delete, timestamp refresh on access, and route-level lookup returning archived jobs.
- **Serial dispatcher concurrency** - End-to-end test proving `max_concurrent=1` never exceeds one simultaneous conversion.
- **z3ds / Dolphin test fixtures** - Added `data_mount_root` monkeypatch to `test_z3ds_routes.py`, `test_metadata.py`, `test_dolphin_routes.py`, and `test_mode_parity_fixes.py` to satisfy the new required setting.

### Files Changed

- `app/config.py` - Volume discovery engine, `data_mount_root`, `AliasChoices`, startup scan cache
- `app/main.py` - Index `Cache-Control: no-store`, version query param on `app.js`
- `app/routes/convert.py` - Cancel-all and clear-completed confirmation header guards, client-IP logging
- `app/services/job_manager.py` - Archived jobs subsystem, serial dispatch, concurrency invariant check
- `app/services/z3ds_compress.py` - `ConversionCancelled` catch-and-reraise
- `entrypoint.sh` - `discover_volumes()` function, volume logging, whitespace-safe iteration
- `static/index.html` - Logo, favicon, cache-busted script tag
- `static/css/style.css` - Header/footer logo styles
- `static/js/app.js` - ClearDoneModal, progress throttle, completion debounce, auto-refresh guard
- `static/js/api.js` - `deleteCompletedJobs` with confirmation header
- `docker-compose.yml`, `docker-compose.multi-volume.yml`, `docker-compose.cli.yml` - `COMPRESSATORIUM_MOUNT_ROOT`
- `run_dev.sh` - New env var names with legacy fallback
- `Dockerfile` - Minor build layer update
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` - Doc refresh
- `AGENTS.md` - New agent runbook
- `tests/test_mode_parity_fixes.py` - 6 new test cases + fixture updates
- `tests/test_volume_discovery.py` - New volume discovery tests
- `tests/test_dolphin_routes.py`, `tests/test_z3ds_routes.py`, `tests/test_metadata.py` - Fixture updates

---

## v3.0.1 - Clear Queue Feature

### New Features

- **Clear Queue** - Added a "Clear Queue" button to the job queue header that allows users to cancel all running and queued jobs at once.
    - **API Endpoint** - `POST /api/jobs/cancel-all` endpoint exposed for bulk cancellation.
    - **UI Integration** - Prominently displayed button in the queue header for quick access.

---

## v3.0.0 - Nintendo 3DS Support & Docker Compose Overhaul

### New Features

- **Nintendo 3DS Support** - Native support for compressing `.cci`, `.cia`, and `.3ds` ROMs using `z3ds_compress`.
    - **New Tool Option** - Select **3DS** from the main tool selector to access 3DS compression modes.
    - **Supported Formats** - Compress `.cci`, `.cia`, and `.3ds` files to `.zcci`, `.zcia`, and `.z3ds`.
    - **Smart Detection** - Automatically identifies 3DS ROMs and filters the file list.
- **Docker Compose Overhaul** - Complete restructuring of Docker Compose configurations for better usability and deployment flexibility.
    - `docker-compose.yml` - Standard single-volume setup.
    - `docker-compose.multi-volume.yml` - Template for multiple volume mounts.
    - `docker-compose.cli.yml` - Dedicated CLI batch processing configuration.

### Breaking Changes

- **ISO Handling Policy** - The "ISO Handling" setting no longer defaults to Dolphin.
    - **Explicit Selection Required** - Users must now explicitly choose between "CHDMAN" (for PS2/DVD) or "Dolphin" (for GameCube/Wii) when processing `.iso` files.
    - **UI Validation** - The interface prevents conversion of ISO files until a handler is selected, preventing accidental invalid conversions.

### Bug Fixes

- **Delete-on-verify messaging** - Corrected messaging for z3ds mode delete-on-verify operations in `static/js/app.js`.
- **Lock Manager** - Fixed `ensure_lock_manager` usage in `services/job_manager.py` to prevent race conditions during z3ds detection.
- **Async Info Method** - Fixed `info()` method in `strategies/z3ds.py` to be properly synchronous within the `run_in_threadpool` wrapper, resolving potential event loop blocking issues.
- **Output Path Logic** - Fixed `treat_as_stem` logic in `get_output_path_for_mode` (routes/convert.py) to correctly handle file extensions.
- **Cancellation Handling** - Standardized usage of `ConversionCancelled` exception in `services/job_manager.py` for reliable job cancellation.
- **Archive Size Checks** - Fixed archive size limit checks in `services/archive.py`.
- **Return Type Consistency** - Improved return type consistency across internal API methods in `routes/info.py`.
- **UI Accessibility** - Increased warning text size and improved color contrast for better readability in `static/css/style.css`.
- **ISO Handling Validation** - Added strict check for `iso_handling` parameter in `routes/convert.py`, rejecting requests where it is null.

### Reliability & Maintenance

- **Periodic Lock Cleanup** - Added `cleanup_stale_locks_periodic` to `JobManager` (services/job_manager.py), running every 10 debug heartbeats (approx. 5 minutes) to automatically remove stale lock files.
- **Z3DS metadata flags** - Added `has_z3ds` and `z3ds_convertible` flags to file search responses in `routes/files.py` to speed up frontend filtering.
- **Conversion Queue Backpressure** - Added queue depth limiting for job creation endpoints:
    - New `MAX_QUEUE_DEPTH` guard applies to `/api/jobs` and `/api/jobs/batch`.
    - Requests now return HTTP `429` when queued + processing jobs exceed configured capacity.
- **Workload Lane Concurrency Controls** - Added lane-specific limits to reduce cross-workload contention:
    - `MAX_VERIFY_CONCURRENCY` caps concurrent CHD/Dolphin/3DS verify workflows.
    - `MAX_METADATA_SCAN_CONCURRENCY` caps concurrent metadata scan tasks.
    - Verify endpoints now fail fast with HTTP `429` when verify capacity is saturated.
- **Adaptive Stall Timeouts** - Conversion stall detection is now size-aware:
    - Uses baseline `CHD_PROGRESS_TIMEOUT`.
    - Adds `CHD_PROGRESS_TIMEOUT_PER_GIB` seconds per GiB of input.
    - Enforces upper bound with `CHD_PROGRESS_TIMEOUT_CAP`.

### Technical Details

- **Z3DS Integration** - Implemented `Z3DS_INFO_EXTENSIONS` and `Z3DS_VERIFY_EXTENSIONS` constants for centralized file type management.
- **Path Helper Methods** - Added `_is_z3ds_info_file` and `_is_z3ds_verify_file` helpers in `routes/info.py` for consistent file type checking.
- **Type Hinting** - Updated type hints in `services/chdman.py` and `services/dolphin_tool.py` for better code quality and static analysis.
- **Refactoring** - Extracted `needsIsoSelection` computed variable in `static/js/app.js` for better maintainability.
- **Timeout Policy Helper** - Added `services/timeout_policy.py` to centralize adaptive stall-timeout computation.
- **Workload Limiter Service** - Added `services/workload_limiter.py` to coordinate verify and metadata scan lane capacity.
- **Queue Depth API** - Added `get_queue_depth()` in `services/job_manager.py` for backpressure checks in convert routes.
- **Regression Coverage** - Added tests for queue-capacity `429`, verify-lane `429`, and adaptive timeout math.


### Deployment & Security

- **New Deployment Guide** - `DEPLOYMENT.md` covers security best practices, resource limits, and production hardening.
- **Docker Documentation** - `DOCKER-COMPOSE.md` provides a quick reference for common commands and troubleshooting.
- **Security Audit** - verified path traversal protections, secret scanning, and container security.

### Files Changed

- `static/js/app.js` - Added 3DS tool logic and frontend integration.
- `app/services/z3ds_compress.py` - New service for 3DS compression.
- `app/routes/info.py` - Fixed async info method patterns.
- `app/routes/convert.py` - Fixed output path logic.
- `README.md` - Added 3DS documentation and Docker Compose sections.
- `DEPLOYMENT.md` - New deployment guide.
- `DOCKER-COMPOSE.md` - New Docker Compose reference.
- `docker-compose*.yml` - New compose files.
- `app/config.py` - Added queue/lane controls and adaptive timeout settings.
- `app/services/timeout_policy.py` - New adaptive stall-timeout helper.
- `app/services/workload_limiter.py` - New lane limiter for verify + metadata scan workloads.
- `app/services/job_manager.py` - Added queue depth accessor for backpressure.
- `app/services/chdman.py`, `app/services/dolphin_tool.py`, `app/services/z3ds_compress.py` - Switched conversion stall checks to adaptive timeout policy.
- `tests/test_timeout_policy.py` - Added adaptive-timeout unit tests.
- `tests/test_mode_parity_fixes.py` - Added conversion queue backpressure tests.
- `tests/test_dolphin_routes.py` - Added verify-lane saturation (`429`) test.

---

## v2.0.1 - Mobile-Responsive Design

### UI/UX Improvements

- **Mobile-responsive Web UI** - A full mobile layout with a card-based file list, touch-friendly controls (44-48px minimum touch targets), and a single column for screens under 768px.
- **Responsive breakpoints** - Added media queries at 480px, 768px, 900px, and 1200px for a consistent layout across phones, tablets, and desktops.
- **Touch-friendly controls** - Interactive elements meet WCAG touch-target sizing.
- **Card-based file list** - On mobile, file list converts from table layout to vertical cards with better information hierarchy.
- **Full-width inputs** - Form controls, dropdowns, and buttons span full width on mobile for easier interaction.
- **Vertical stacking** - ISO handling options, toolbar elements, and compression options stack vertically on mobile.
- **Modal improvements** - Modals now use 95% viewport width on mobile with proper scrolling (90vh max-height).
- **Screenshots documentation** - Added responsive design screenshots to README showcasing desktop, tablet, and mobile views.

### Technical Details

- Pure CSS solution with no JavaScript changes required
- Zero breaking changes to desktop functionality
- 627 lines of responsive CSS added
- Desktop layout (3-column) fully preserved for screens ≥1200px

### Files Changed

- `static/css/style.css` - Added mobile-responsive styles with multiple breakpoints
- `README.md` - Added Screenshots section with responsive design examples
- `docs-desktop-view.png`, `docs-tablet-view.png`, `docs-mobile-view.png` - Added documentation screenshots

### New Features

- **Archive delete-on-verify** - Archive inputs can now delete the entire archive after a successful conversion + verification, with an explicit warning in the delete plan.

---

## v1.2.1 - Archive Safety Limits & Timeout Controls

### New Features

- **Archive safety limits** - Configure maximum archive entries, per-member size, and total extraction size with `CHD_ARCHIVE_MAX_ENTRIES`, `CHD_ARCHIVE_MAX_MEMBER_SIZE`, and `CHD_ARCHIVE_MAX_TOTAL_SIZE`.
- **Archive truncation metadata** - File listing/search responses now report when archive listings are truncated by safety limits.
- **Verification timeouts** - New `CHD_VERIFY_TIMEOUT` and `CHD_VERIFY_PROGRESS_TIMEOUT` allow you to stop long-running or stalled `chdman verify` operations.

### Safety Improvements

- **Output directory validation** - Output directories are trimmed and rejected if empty, preventing accidental writes to invalid paths.
- **Safe temp cleanup** - Temporary directories are only removed if they are within expected temp locations.
- **Chdman info timeout** - `CHD_INFO_TIMEOUT` prevents `chdman info` from hanging indefinitely.

### Bug Fixes

- **Archive enumeration errors** - Directory scans skip problematic entries instead of failing entire requests.
- **Output path creation** - Output directories are only created when a directory component exists.

### Files Changed

- `app/config.py` - Added archive and timeout configuration values
- `app/models.py` - Archive truncation metadata
- `app/routes/convert.py` - Output directory validation and reuse
- `app/routes/files.py` - Archive truncation metadata + safe scanning
- `app/services/archive.py` - Archive limits + truncated listings
- `app/services/chdman.py` - Timeout handling for info/verify
- `app/services/concurrency_manager.py` - Ticket lock handling improvements
- `app/services/job_manager.py` - Safe temp cleanup checks
- `README.md` - Archive limit and timeout documentation

---

## v1.2.0 - Delete-on-Verify & Safer File Ops

### New Features

- **Delete-on-verify** - Optional post-conversion verification that deletes the original source only after a successful CHD verify (create/copy modes).
- **Delete plan confirmation** - New `/api/jobs/delete-plan` endpoint + UI modal showing exactly which files will be removed before conversion starts.
- **Track-aware deletes** - `.cue`/`.gdi` companion tracks are included in the delete plan and removed as a set.

### Safety Improvements

- **Snapshot + fingerprint validation** - Delete plans are revalidated at completion and must match original fingerprints before any deletion.
- **In-use protection** - File delete/rename operations are blocked while a path is used by an active job (including cue/gdi track files).
- **Lock hygiene** - Hash-based lock filenames and startup cleanup for stale file locks.
- **Cancel-safe** - If a cancel occurs after verify, deletion is skipped.

### UI/UX

- **Always-visible Select All** checkbox with indeterminate state.
- **Conversion panel refresh** with clearer post-conversion options and copy-mode warnings.

### Files Changed

- `app/utils/delete_plan.py` - Track parsing, delete plan snapshotting, safety checks
- `app/services/job_manager.py` - Delete-on-verify orchestration + safety validation
- `app/routes/convert.py` - Delete plan endpoint and request validation
- `app/routes/files.py` - In-use path blocking for rename/delete
- `app/services/lock_manager.py` - Lock directory management and cleanup
- `static/js/app.js` - Delete-on-verify UI + Select All + layout updates
- `static/js/api.js` - Delete plan API
- `static/css/style.css` - Toolbar/options layout styling
- `README.md` - Feature and API docs

---

## v1.1.5 - Archive Conversion Safety & Stall Watchdog

### Bug Fixes

- **Archive member selection** - When both `.cue`/`.gdi` and `.bin` exist in the same archive folder, `.bin` entries are now suppressed. This prevents conversions from starting with an incomplete input set (missing TOC/track layout), which can stall `chdman` and never reach completion.
- **Batch dedupe by output path** - Batch job creation now keeps only one job per output CHD and prefers `.cue`/`.gdi` > `.iso` > `.bin` when multiple archive members map to the same output. This avoids duplicate work, conflicting locks, and stuck jobs.
- **Stall watchdog** - New `CHD_PROGRESS_TIMEOUT` fails a conversion if both progress and output size stay unchanged for the configured period (default 600s). The job is marked failed with a clear error instead of lingering at 99%.

### Files Changed

- `app/services/archive.py` - Prefer `.cue`/`.gdi` over `.bin` for archive listings
- `app/routes/convert.py` - Deduplicate batch jobs by output path and prioritize safe inputs
- `app/services/chdman.py` - Conversion stall detection with timeout and clear failure message
- `app/config.py` - New `CHD_PROGRESS_TIMEOUT` setting
- `README.md` - Archive behavior and timeout docs
- `DOCKER-COMPOSE.md` / `DEPLOYMENT.md` - Added `CHD_PROGRESS_TIMEOUT`

---

## v1.1.4 - Python 3.8 Compatibility Fix

### Bug Fix

- **Conversion completion regression** - On Python 3.8, the new `list[str]` annotation in `app/services/chdman.py` raises `TypeError: 'type' object is not subscriptable` at runtime. That exception happens inside the conversion generator before the "complete" event is emitted, so jobs never transition to `completed` on the frontend even if `chdman` finishes. The annotation is now `typing.List[str]` to keep Python 3.8 compatibility.
- **Guardrail test** - Added a test that fails if `list[...]` annotations appear in `chdman.py` without `from __future__ import annotations`, preventing this regression.

### Files Changed

- `app/services/chdman.py` - Python 3.8-safe annotation for output buffering
- `tests/test_chdman_annotations.py` - Regression test for annotation compatibility

---

## v1.1.1 - Async I/O & Reliability Improvements

### Internal Improvements

- **Async I/O Refactor** - Filesystem operations on request paths (info, files, stores) now offload to threadpool, preventing event loop blocking
- **Version-Gated Persistence** - Metadata and verification stores implement last-write-wins with version checks to prevent stale overwrites
- **Lock Order Consistency** - Eliminated potential deadlocks between sync and async persistence paths
- **Timezone-Aware Timestamps** - Replaced deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
- **Concurrency Tests** - Added test coverage for concurrent metadata/verification store writes

### Files Changed

- `app/services/chd_metadata_store.py` - Async persistence, version-gated replace
- `app/services/verification_store.py` - Async I/O, version-gated replace
- `app/services/job_manager.py` - Timezone-aware timestamps
- `app/routes/info.py` - Threadpool offloading for filesystem checks
- `app/routes/files.py` - Async filesystem operations
- `tests/test_metadata.py` - Concurrency tests
- `tests/test_verification_store.py` - Concurrency tests
- `walkthrough.md` - Updated async safety documentation

---

# Release Notes - v1.0.0

## Major Release: CHD Metadata Caching & Version System

This release introduces significant new features including intelligent CHD metadata caching, a unified version system, and enhanced UI capabilities.

---

## New Features

### CHD Metadata Caching System
- **Persistent metadata cache** - CHD file metadata is now cached to disk, avoiding repeated `chdman info` calls
- **Automatic cache invalidation** - Uses file modification time (mtime) to detect stale entries
- **Media type detection** - Automatically identifies DVD vs CD media types from CHD metadata
- **Background metadata scanning** - New "Scan Metadata" button triggers async scanning of all volumes
- **Batch metadata API** - Frontend can request metadata for multiple CHDs in a single call

### Version Management
- **Single source of truth** - Version now managed via `.version` file at project root
- **API endpoint** - New `/api/version` endpoint returns current version
- **Footer display** - Version shown in application footer with GitHub link
- **CI integration** - Docker workflow reads version from `.version` for image tags
- **Sync script** - `scripts/sync-version.sh` keeps package.json in sync

### UI Enhancements
- **File type filtering** - Filter file list by CHD files, archives, or disc images
- **Shift-click selection** - Select ranges of files with Shift+Click
- **Media type badges** - CHD files display DVD/CD badge when metadata is cached
- **Selection pruning** - Selected files are pruned when filter hides them

---

## Technical Improvements

### Performance
- **Non-blocking persistence** - Metadata cache writes use thread pool to avoid blocking event loop
- **Batched updates** - Background scan accumulates changes and writes once at end
- **Threaded filesystem traversal** - `os.walk` runs in thread pool during metadata scans

### Concurrency Safety
- **Version-tracked writes** - Prevents stale async writes from overwriting newer data
- **Lock ordering** - Consistent lock acquisition order prevents deadlocks
- **Safe background tasks** - Fire-and-forget tasks wrapped with error logging

### Error Handling
- **Graceful version fallback** - Returns "0.0.0" if version file unreadable
- **Import compatibility** - Version endpoint works with both `uvicorn main:app` and `uvicorn app.main:app`

---

## Files Changed

- `.version` - New version source of truth
- `scripts/sync-version.sh` - New version sync utility
- `app/services/chd_metadata_store.py` - New metadata caching service
- `app/routes/info.py` - Added metadata and version endpoints
- `app/main.py` - Dynamic version reading
- `app/models.py` - Added MetadataBatchRequest model
- `static/js/app.js` - File filtering, shift-select, badges
- `static/js/api.js` - Metadata and version API methods
- `static/css/style.css` - Badge and filter styling
- `static/index.html` - Added useMemo hook
- `.github/workflows/docker-image.yml` - Version-based tagging
- `package.json` - Updated version

---

## Upgrade Notes

This release is backwards compatible. The metadata cache will be built automatically as CHD files are accessed or when "Scan Metadata" is clicked.
