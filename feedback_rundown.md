# Feedback rundown — exclusions, broken games, and what to build next

Collected 2026-08-26 from the Habr article, GitHub issue #21, CompactGUI / Compactor public reports, and Microsoft docs. Written so a later session does not have to re-derive this.

Companion sources:

- Article: https://habr.com/ru/articles/1073950/ (11 upvotes at capture)
- Issue: https://github.com/me-when-the-uh/trash-compactor/issues/21
- `ironing_out.md` was a closed v0.7.0 record and has been removed. Do not reopen those items.

This is a decision log, not a live TODO. The recommended first product change is **user path exclusions** (issue #21). Everything else is context so that change is not implemented blindly.

§3.1 was expanded after checking the current walker / skip list / entropy / one-click targets against the CompactGUI reports: those games **would** be compacted today. Automatic protection without a title denylist is mostly not feasible (Wildlands is one unnamed 5.4 GiB delta file; GW2 is a live archive with a public magic that can wait). §7 and §11 list the exact files and functions for the exclusions PR and for every later item.

---

## 1. What issue #21 actually asks for

Ghokury (2026-03-30):

> Possibility to add exclusions would be quite useful: not all games work after compression (Wildlands and Ghost Recon, for example), and some applications glitch, especially when it concerns `.db`. I have not checked the examples above on this application, but with the original Compactor it was like that.

Owner reply (2026-08-25): will take it; Ubisoft titles in particular are a known awkward case.

Two separate problems are mixed in one request:

1. **Some games break** after WOF / `compact.exe /exe` compression. That is a *compatibility* problem, not a compressibility problem.
2. **Some apps glitch around database files.** That is a *write-heavy / open-file* problem.

CompactGUI skips by *extension*. Freaky’s Compactor skips by *user path globs*. This issue is the Compactor-style request. Trash-Compactor currently has neither a user path list, nor `--exclude`, nor a GUI exclusion editor.

---

## 2. What the program already does (do not rebuild)

### Extension skip (`src/config.py` `SKIP_EXTENSIONS`)

Already skipped, case-insensitive:

- archives, disk images, images, video, audio
- ML weights (`.gguf`, `.safetensors`, …)
- office/PDF
- **databases:** `.mdf`, `.ldf`, `.sqlite`, `.sqlite3`, `.db`, `.db3`, `.mdb`, `.accdb`, `.pst`, `.ost`, `.edb`
- incomplete downloads

So the `.db` complaint is only half-solved. SQLite files often have **no extension** (Chrome `History` / `Cookies`, game saves named `save` / `playerdata`). Extension skip cannot catch those. A **path exclusion** can.

### System directory prune (Rust walker)

Never walked: active `Windows` on every fixed drive, `Windows.old` / `Windows.old.NNN`, `$Recycle.Bin`, `System Volume Information`, `Recovery`, `PerfLogs`. Matching is `normcase` + `normpath` + prefix.

### Entropy / incompressible cache

Directory-level and per-file LZ4 gates already drop high-entropy trees. That answers “don’t waste writes on JPEG/MP3/zip”. It does **not** answer “this game launches, then hangs.” A perfectly compressible file can still be a broken game asset.

### CompactOS

Already opt-in, admin-gated, not the default path. Keep it that way.

### Would the §3.1 games actually be compressed today?

**Yes.** Nothing in the current pipeline would save them. Walk through the gates:

| Gate | Where | Wildlands `.tbf` / `.forge` | GW2 `Gw2.dat` | SWL data + `ClientPatcher.exe` | LOTRO `client_dat*.dat` |
|---|---|---|---|---|---|
| System-dir prune | `file_utils._default_excluded_directories` → Rust `ExclusionIndex` | Not under `Windows` | Not under `Windows` | Not under `Windows` | Not under `Windows` |
| Extension skip | `config.SKIP_EXTENSIONS` / Rust `skip_extension_name` | `.tbf`, `.forge`, `.wmap`, `.pck` are **not** listed | `.dat` is **not** listed | `.dat` is **not** listed | `.dat` is **not** listed |
| Min size | `MIN_COMPRESSIBLE_SIZE` = 8 KiB | 31 MiB / 5.4 GiB | tens of GiB | large | large |
| Already-compressed | `GetCompressedFileSizeW` / attr 0x800 | Only if previously compacted | Only if previously compacted | same | same |
| Entropy / 15% savings | `_filter_high_entropy_directories` | Delta terrain **wants** to compress | CompactGUI wiki: 22 GB → 1.9 GB. Entropy will **enthusiastically** keep it | Compresses well | Compresses well |
| One-click targets | `one_click.resolve_targets` | Steam / Ubisoft Connect live under `ProgramFiles*` | ArenaNet default is `Program Files\Guild Wars 2` or AppData-adjacent | Funcom under `Program Files (x86)\Funcom` | SSG/Turbine under Program Files |

One-click mode is the dangerous path: it walks `ProgramFiles`, `ProgramFiles(x86)`, `AppData`, `Downloads`, `Documents`, `ProgramData`. That is where these installs live. A user who never heard of Wildlands can still compact it.

Entropy is the wrong tool here. It answers “will this shrink?”, not “will the owning process then hang?” A perfectly compressible file can still be a broken game asset. Do not try to “fix” these titles by raising the savings threshold.

---

## 3. Videogames that do not work after `compact.exe` / CompactGUI

These are reports against **WOF compression** (`compact /exe:XPRESS*` / `LZX`), which is the same mechanism Trash-Compactor uses. They are not reports against this repo specifically. Ghokury said as much.

Primary sources:

- CompactGUI issue #101, “Report Issues with Compacted Games and Programs Here!” (opened 2017, still the community dump)
- CompactGUI wiki “Important Information”
- CompactGUI README DirectStorage caveat
- Microsoft BypassIO docs
- Freaky/Compactor README caveats
- Steam thread: Wildlands + NTFS compression freeze at load

### 3.1 Games that misbehave (compatibility, not ratio)

| Title | What happens | Usual fix | Notes |
|---|---|---|---|
| **Tom Clancy’s Ghost Recon Wildlands** | Main menu loads; cannot proceed. Freeze on load or benchmark. | Decompress the folder. Later CompactGUI comment: decompressing only `PCgr_terrainlin1.tbf` was enough; LZNT1 on that one file also worked. | Patch/delta-style terrain file, similar to MMORPG `patch0`/`patch1` files. Whole-folder skip is the safe user-facing answer. Hardcoding the `.tbf` name is too brittle for v1. |
| **Ghost Recon** (unspecified, issue #21) | Same family; reporter grouped it with Wildlands. | Treat as a user folder exclusion. | Do not assume every Ghost Recon title is Wildlands. Breakpoint is also AnvilNext (`.forge`) and may or may not share the terrain bug. Original 2001 Ghost Recon is a different engine (`.rsb`). |
| **Guild Wars 2** | Compresses *very* well, then **decompresses itself at launch** and hangs while it does so. | Do not compress the install. CompactGUI wiki calls this the textbook “some games don’t like this.” | Self-unpack on launch is a different failure mode from Wildlands. The bulk is one file, `Gw2.dat`. |
| **Secret World Legends** | Decompresses itself at launch. | Decompress. | Same pattern as GW2. Funcom `ClientPatcher.exe` rewrites data at launch. |
| **Lord of the Rings Online** | Patching freezes; game crashes. | Decompress. | In-place binary patching + WOF is a recurring theme. CompactGUI #101 also names Elder Scrolls Online for the same patcher-freeze. |

Freaky’s Compactor README, almost verbatim:

> If a game uses large files and in-place binary patching for updates, it might be worth adding to the exclusions list.

That is the Wildlands / LOTRO class. **Do not hardcode Ubisoft (or any) titles.** Install paths vary (Steam, Ubisoft Connect, Epic, Xbox PC) and the set changes with patches.

#### What is actually going wrong (three failure modes, not five games)

These reports collapse into three mechanical WOF behaviours. The title list is just how they showed up in CompactGUI #101.

**Mode A — write-unpack of a live archive.** WOF (`compact /exe`, `FSCTL_SET_EXTERNAL_BACKING`) is a reparse overlay. Opening the file with `GENERIC_WRITE` — even if the process writes nothing — makes `wof.sys` fully materialise the uncompressed file before the handle is returned. Hang time is roughly `(logical size) / disk write speed`. Compactor’s README states this directly; CompactGUI discussion #509 restates it: *“if it tries to modify any of its files, those will be decompressed by the filesystem automatically.”*

- **Guild Wars 2.** `Gw2.dat` is an ArenaNet MFT archive, not a static pack. Wiki: the file *“is created and modified by adding and changing files inside of it dynamically”* and *“many files are downloaded in a compressed format and will only be decompressed when you encounter them in-game.”* The client opens it writable at launch. CompactGUI’s “uncompresses the entire game” is this one file expanding (it *is* the game). Header magic is stable: version byte + `AN\x1A` (commonly `3AN\x1A` = `33 41 4E 1A`). MFT identifier at the table is `Mft\x1A`.
- **Secret World Legends.** Funcom `ClientPatcher.exe` mutates the data tree at launch. Same WOF write-unpack, spread across more than one file.

**Mode B — in-place binary patching.** A launcher applies a delta into an existing file (seek + overwrite, not rewrite-via-temp). WOF backing cannot accept sparse in-place overwrites the way a normal file can; the patcher freezes or the file ends up corrupt.

- **LOTRO** (Turbine/SSG `client_dat*.dat` + patcher). CompactGUI #101: patcher never finishes; after a successful uncompressed patch, recompressing makes the launcher hang.
- **ESO** (same thread, Steam version): *“MMOs or anything that needs to patch has a hard time with compressed files.”*
- Compactor README’s “large files and in-place binary patching” sentence.

**Mode C — WOF-hostile random access, LZNT1-tolerant.** This is the Wildlands one-file bug, and it is *not* Mode A or B.

- Steam depot: `PCgr_terrainlin0.tbf` ≈ 31 MiB (base, **WOF-ok**), `PCgr_terrainlin1.tbf` ≈ 5.4 GiB (delta/extension, **WOF-broken**). CompactGUI 2018-02-16: decompressing *only* `PCgr_terrainlin1.tbf` unblocks the game; compressing that one file with Explorer’s “Compress contents” (LZNT1, `FILE_ATTRIBUTE_COMPRESSED`) also works.
- Reporter’s own analogy: *“an extension to the original terrain file, like updates in mmorpgs (patch0, patch1,…) which are also known to have issues with compact.”*
- Adjacent `.forge` AnvilNext archives (`DataPC.forge` 7.6 GiB, `DataPC_patch_01.forge`, …) were **not** named as the fix, so they are probably WOF-fine. A `*patch*` filename glob would have skipped the wrong files.
- Why LZNT1 works and WOF does not: LZNT1 is in-cluster NTFS compression with 64 KiB units and no reparse point. WOF is an overlay filter. A reader that `mmap`s, uses `FILE_FLAG_NO_BUFFERING`, or assumes a seekable uncompressed mapping of a multi-gigabyte stream can survive LZNT1 and lock up on WOF. Do **not** take this as a product cue to fall back to LZNT1 (see §5 evgen_b / §8 “bad ideas”).

Ghokury’s “Ghost Recon” in issue #21 is unspecified. Wildlands is AnvilNext; Breakpoint is AnvilNext; 2001 Ghost Recon is not. Folder exclusion, not a franchise rule.

#### File-level trends (what a heuristic could even look at)

| Signal | Hits | Misses / danger | Verdict |
|---|---|---|---|
| Folder / exe name (`Wildlands`, `Gw2-64.exe`, Ubisoft) | These five titles | Every other language, launcher, DLC folder, future title | **No.** This is the CompactGUI wiki. Do not start one. |
| Extension `.tbf` | Wildlands terrain | Unknown other Anvil users; two `.tbf` files of which only one is hostile | **No** as a global skip. |
| Extension `.dat` | GW2, LOTRO, some SWL | Thousands of read-only `.dat`s, registry hives (`ntuser.dat` is `regf`), random game data | **No** as a global skip. |
| Extension `.forge` | Wildlands / Breakpoint archives | Those archives were not the broken file | **No.** |
| Filename `*patch*` / `*lin1*` | Wildlands `terrainlin1` | Wildlands `DataPC_patch_01.forge` (not the bug); every game with a patch pak | **No.** |
| Size > N GiB | `Gw2.dat`, `terrainlin1.tbf` | Read-only `.pak` / `.arc` / video that compress fine and are the product | **No.** |
| “Sibling numbered pair, N+1 much larger” | Wildlands `lin0`/`lin1` | False positives in every split archive | **No.** Too cute. |
| First 16 bytes `SQLite format 3\0` | Extensionless Chrome `History` / `Cookies`, game saves named `save` | Almost none | **Yes, later.** Format skip, same class as `_DATABASES`. See §4. |
| First 4 bytes ArenaNet DAT (`xx AN \x1A`, typically `3AN\x1A`) on a large `.dat` | `Gw2.dat`, GW1 `Gw.dat`, `Local.dat` | Coincidence on a 4-byte magic is rare if also size-gated (≥ 1 MiB) | **Maybe, later.** Format skip of a *writable* archive, not a game name. Do **not** ship in the exclusions PR. |
| Turbine/SSG DAT magic, Funcom container magic | LOTRO, SWL | Not researched here. Do not guess. | **Research only.** |
| `dstorage.dll` in the same tree | DirectStorage titles | None interesting | **Warn, don’t skip.** Already §3.2 / README backburner. |
| User directory prefix | Everything, including future titles | Requires the user to act | **v1. This is issue #21.** |

The only non-blacklist automatic skip that is in the same spirit as the existing product is **magic-byte classification of live/writable containers**, the way we already classify databases by extension. ArenaNet DAT is the one case where the magic is public and the write behaviour is documented. Even that is a follow-up: getting the 4-byte probe wrong is worse than making the user exclude `C:\Program Files\Guild Wars 2`.

For Wildlands Mode C there is no honest automatic skip. The broken file is one of two `.tbf`s, identified by a filename that will rot, with no public header spec. The user-facing answer is the folder exclusion plus a decompress button so they can undo a one-click run.

#### What current code would have to change to even try a magic skip

Classification happens in Rust (`fast_walk` `classify` / `skip_extension_name`) **by extension only**. There is no header probe. Adding one means extra I/O on the walk. Cheap if restricted to `.dat` files over a size floor (four bytes). Expensive if applied to every extensionless file (SQLite). Either way it is a walker change, not a Python-only filter after the fact — a Python-side reopen of every candidate would defeat `fast_walk`.

Do not mix this into the exclusions PR.

---

### 3.2 DirectStorage — a different problem, Microsoft-documented

This is not “the game crashes.” It is “the fast I/O path is disabled.”

Microsoft BypassIO docs (`learn.microsoft.com/windows-hardware/drivers/ifs/bypassio`):

- BypassIO is the Windows 11 infrastructure DirectStorage uses to skip the filter / volume stack on NVMe.
- **You cannot enable NTFS compression on a BypassIO-active file.**
- The file system vetoes BypassIO for NTFS-compressed files (also encrypted, sparse, paging files, DAX).
- The **WOF minifilter itself** is a documented non-participant: `fsutil bypassIo state c:\` can report `Driver: wof.sys` / “The specified minifilter does not support bypass IO.”

So compressing a DirectStorage game with `compact /exe` forces the traditional I/O path. CompactGUI’s README caveat is correct, not marketing.

Public titles that actually use DirectStorage (still a short list as of 2025–2026):

- *Ratchet & Clank: Rift Apart*
- *Forspoken*
- *Forza Motorsport*
- *Horizon Forbidden West Complete Edition*

Auto-detecting these (e.g. `dstorage.dll` in the install) is already on this repo’s README backburner. Do **not** guess from folder names in the first exclusion feature. A user path exclusion covers it if someone hits it.

**Would this repo compress them today?** Yes. `dstorage.dll` is a DLL, not in `SKIP_EXTENSIONS`, and the game’s `.pak` / `.ucas` assets are exactly the files the product exists to compress. Entropy may skip already-compressed textures; it will not skip the uncompressed bits, and BypassIO is vetoed per-file on whatever WOF did touch.

**Proposed later change (not v1):** while planning, if any candidate path’s filename is `dstorage.dll` or `dstoragecore.dll` (case-insensitive), emit a warning naming the parent directory. Do not drop the tree. The DLL itself will already be in the candidate list (it is > 8 KiB), so this needs **no walker change** — inspect `plan` / `candidates` in `plan_compression` or the GUI analysis pipeline.

- File: `src/compression/compression_planner.py` (`plan_compression`) and/or `src/gui/pipelines/analysis.py`
- Also: `src/stats.py` (a `warnings` list or a `DirectorySkipRecord`-like `notice` with `category='directstorage'`)
- Dry-run / `-v` should print it. GUI analysis summary should surface it.
- Do not auto-skip: Microsoft’s veto is file-level; a silent skip of the whole game is surprising.

### 3.3 Unreal `.pak` / `.ucas` / `.utoc` — do not blacklist globally

CompactGUI issue #573 (2025) asked to skip `.pak`, `.ucas`, `.utoc` because some UE4/5 games become **unreadable** if compressed “too much” (reporter described ~80% squash turning into dummy data). That is a *broken reader* failure, not a poor-ratio failure.

Entropy already skips incompressible packs. Many UE games compress fine. A global extension ban would throw away a lot of legitimate savings. If a specific title breaks, the user excludes that **folder**.

**No code change.** `.pak` / `.ucas` / `.utoc` stay compressible. Confirm `SKIP_EXTENSIONS` in `src/config.py` is never given these. If CompactGUI #573-style “unreadable after 80% squash” shows up against *this* repo, that is a WOF-reader bug in that title and the fix is the user exclusion, not an extension.

### 3.4 What is *not* a “game doesn’t work” report

- Poor compression ratio (Cyberpunk ~0.98× in CompactGUI community data). Entropy already handles this.
- Game updates undoing compression (“compression decay”). WOF `/exe` does **not** inherit onto rewritten files. CompactGUI’s Background Watcher exists because of this. Re-running Trash-Compactor is already safe (DRY / already-compressed skip). A watcher is a later product, not part of exclusions.
- Copying the folder to another drive unpacks it. Fundamental WOF behaviour. There is no supported way to copy the backing as-is.
- Linux `ntfs3` `mmap(write)` on compressed files (e.g. Proton + *Generation Zero*). Out of scope for a Windows-only tool; worth a FAQ line if dual-boot users show up.

**Proposed change:** FAQ / README only. File: `README.md` FAQ section. No function. Three short entries: compression decay, copy unpacks WOF, Proton/ntfs3 mmap-write. Do not add a Proton detector.

### 3.5 Why hardcoding game names is a bad idea

- Paths differ per launcher and per language.
- Wildlands’ actual bug was one patch file, not the whole tree.
- New titles (and patches) will keep appearing.
- CompactGUI maintains a community wiki of 100k+ submissions and still cannot keep a complete denylist. This project should not start one.

The user-facing contract should be: **if a game or app misbehaves, add its folder to exclusions and re-run. Compression is reversible with `compact /u`.**

Until a decompress UI exists, that second sentence is a lie in-app: the user has to drop to `cmd`. Exclusions without uncompact is “don’t make it worse next time.” See §8 item 2.

`compact /u` without `/exe` only clears LZNT1. WOF needs `compact /u /exe`. Document that in the FAQ the moment exclusions ship, even before the decompress button.

---

## 4. Databases and write-heavy files

### Microsoft, not folklore

[SQL Server KB 231347](https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/database-file-operations/support-databases-compressed-volumes):

- Read/write SQL Server databases on compressed volumes are **unsupported**.
- Error 5118: *“The file is compressed but does not reside in a read-only database or filegroup. The file must be decompressed.”*
- Reason: compressed volumes do not guarantee sector-aligned writes, which recovery requires.

This is about NTFS compression in general. WOF is still compression as far as those checks are concerned. Skipping `.mdf` / `.ldf` is correct. Path-excluding `…\MSSQL\DATA` is the belt.

### SQLite

Freaky/Compactor issue #40: one report of **data corruption on an open SQLite file**. Author could not reproduce; exclusive file locking was added in Compactor 0.10. Compact.exe already fails on many locked files, but “open for write” is enough to make WOF decompress-on-write hang (Compactor README: opening a compressed file in write mode waits until the whole file is uncompressed, even if nothing is changed).

Habr commenter **mixsture**: small in-place writes on compressed files are CPU-heavy because the backing is rewritten. Databases are the textbook case.

Implication for this tool:

- Keep the database extension skip.
- Path exclusions cover extensionless SQLite and whole data directories.
- Do **not** make exclusive locking the primary SQLite fix in the first exclusions PR. Compactor added it after one unreproduced report. Path skip is the user-facing answer.

Also skip-worthy in the same “modifiable files” bucket (Compactor README): logs, VM images, anything with in-place binary patching. VM images are already extension-skipped (`.vdi`, `.vmdk`, `.vhdx`, …). Logs are not, and should not be globally skipped — they are often compressible and cold.

#### SQLite-by-header (the good kind of “not a blacklist”)

Issue #21’s `.db` half is only half-solved because Chrome `History` / `Cookies` / `Web Data` and many game saves have **no extension**. That is the same class of problem as GW2’s `Gw2.dat`: the file type is knowable from bytes, not from the title of the app.

SQLite’s header is 16 bytes, ASCII `SQLite format 3\0`. False positives are essentially zero. This is a format skip, like `_DATABASES` in `src/config.py`, not a game wiki.

**Do not put this in the exclusions PR.** It is a walker classification change and wants its own test.

Proposed follow-up:

- File: `fast_walk/src/lib.rs` (`scan_dir` / a new `skip_magic` helper next to `skip_extension_name`)
- Probe **only** files with an empty extension (or a tiny extra name list: `Cookies`, `History`, `Web Data`, `favicons`) **and** `size >= MIN_COMPRESSIBLE_SIZE`. Reading 16 bytes from every JPEG is not acceptable.
- New category: `CAT_MAGIC` / `skip_magic` (or reuse `CAT_EXTENSION` with a distinct Python reason). Prefer a new category so `-vvv` can say “SQLite header” not “extension”.
- Python: `file_scan.py` `CAT_*` constants; `compression_planner.py` bulk-skip bucket; `stats.py` `SkipBulkLedger` field; `skip_logic.log_directory_skips` does not apply (this is per-file).
- Also skip sibling `-wal` / `-shm` / `-journal` files when the stem matches a detected SQLite file. Those often have no extension either (`History-wal`).
- SQL Server KB 231347 still wants path-excluding `…\MSSQL\DATA` via the user list; header skip will not catch `.mdf` (already extension-skipped) and does not need to.

---

## 5. Habr comments that actually imply product changes

Most of the 11-upvote thread is NTFS trivia (NT 3.1 vs XP, Stacker, DriveSpace). The useful ones:

| Comment | Point | What to do |
|---|---|---|
| **mixsture** | In-place small writes on compressed files are expensive (DBMSs). | Path exclusions + existing DB extensions. Do not compress live databases. |
| **mixsture** on Dota 2 | “Saved” game space is fake: a patch rewrites files uncompressed, the user fills the apparent free space, then the update fills the disk to zero. | FAQ / article honesty. Do not build CompactGUI’s Background Watcher as the first follow-up. Re-run is already safe. |
| **evgen_b** | Classic folder compression *inherits* onto new files (LZNT1); write-heavy nested dirs (registry) then need explicit decompress. | Do **not** “fix” WOF decay by flipping folders to LZNT1. That is the slow fragmenting algorithm this tool exists to avoid. CompactGUI wiki: `/exe` does not auto-apply to new or updated files. |
| **mvv-rus** | `compact.exe` already takes name patterns; admins use scripts and GPO; GUI is for advanced users. | **CLI `--exclude` is as important as a GUI list.** The article advertises Task Scheduler / fleet use. GUI-only exclusions miss that audience. |
| **Darkness_Paladin** | Compressing Windows / Program Files on XP was a disaster; only compress rarely-used uncompressed data. | Do **not** add Program Files as a default user exclusion. Entropy + CompactOS-as-opt-in is the existing answer. |
| **Wolf4D / Lordzero / Kerman** | Disk recovery of compressed vs uncompressed. Compressed units lose more logical data per bad sector; uncompressed files have more blocks so a higher chance of *some* damage. | FAQ, not code. |
| **Shrizt** | Compressing everything blindly is stupid; picking by hand is also bad. | Validates the product thesis. Exclusions are the escape hatch when the heuristic is wrong. |
| **liyafomina** | “Eats CPU and kills SSDs.” | Already addressed by skip lists + entropy + already-compressed detection. Keep saying it. |

Author replies already covered the 256 GB / Dota 2 / zoo-of-office-PCs use case. No code change implied there.

Specific landings for the rows that *do* imply a change:

| Comment | File | Function | What to add |
|---|---|---|---|
| mixsture (DBMS writes) | `src/config.py` `_DATABASES` (already), user exclusions, optional SQLite magic in `fast_walk` | see §4 and §7 | No new extension. Path skip is the product. |
| mixsture (Dota 2 decay) | `README.md` FAQ | n/a | One paragraph: WOF does not re-apply on rewrite; re-run is free; do not fill the “saved” space and then patch. |
| evgen_b (LZNT1 inherit) | none | none | Explicit non-goal. Do not add `compact /c /s` without `/exe` after a run. |
| mvv-rus (CLI for admins) | `main.py` `build_parser`; `src/launch_flags.py` | `build_parser`, `LaunchState` | Repeatable `--exclude PATH`. Env `TRASH_COMPACTOR_EXCLUDE` (`;`-separated) for Task Scheduler. GUI-only exclusions miss the audience the Habr article sold. |
| Darkness_Paladin (Program Files) | `src/one_click.py` `resolve_targets` | leave as-is | Do not add Program Files to the default user-exclusion list. Entropy + CompactOS-opt-in stays the answer. |
| Wolf4D / recovery | `README.md` FAQ | n/a | One line, not code. |
| Shrizt | user exclusions | §7 | Escape hatch when the heuristic is wrong. |
| liyafomina (CPU / SSD) | none | none | Already answered by skip list + entropy + already-compressed. Keep saying it in README. |

---

## 6. What CompactGUI and Compactor did (copy the right parts)

| | CompactGUI | Freaky’s Compactor | Trash-Compactor today |
|---|---|---|---|
| Skip incompressible types | User + community extension lists | Statistical sample | Built-in extensions + entropy |
| User **path** exclusions | No | Glob list (`*:\Windows*`) | System paths only |
| Prune excluded dirs in the walk | N/A | Added in 0.8 after they walked them anyway ([issue #8](https://github.com/Freaky/Compactor/issues/8)) | Rust walker already prunes system dirs |
| Broken-game wiki | Yes | User’s problem | Nothing — keep it that way |
| DirectStorage warning | README + Steam folder check | No | README backburner |
| Decompress / uncompact | One click | Yes | No |
| File lock before compact | ? | Exclusive lock since 0.10 (SQLite report) | Relies on `compact.exe` failing on locked files |

Copy Compactor’s **path-prefix prune in the walker**, not CompactGUI’s game wiki. A hardcoded Wildlands path will be wrong on half of installs.

Compactor’s glob `*:\Windows*` is how users deleted the OS exclusion and then compressed `C:\Windows`. System exclusions in this project must stay **uneditable**.

Walker prune is already the right shape: `ExclusionIndex` in `fast_walk/src/lib.rs` takes a `Vec<String>` of prefixes. v1 exclusions are “hand more prefixes in,” not “rewrite the walker.” Compactor issue #8 is the warning: if Python filters after the walk, excluded trees are still read. `iter_files` must merge the user list *before* `walk_and_filter`.

---

## 7. Recommended first feature: user path exclusions

This is the issue #21 implementation, scoped so it does not turn into a game database.

### Do

- User **directory prefixes**, same matching as existing system exclusions (`normcase` + `normpath` + `startswith`).
- Feed them into the **Rust walker** so excluded trees never reach Python (Compactor issue #8).
- Persist under `%APPDATA%\TrashCompactor\` next to `incompressible.db`.
- Expose **both**:
  - GUI: Settings page, add/remove folders.
  - CLI: `--exclude PATH` (repeatable) and/or `TRASH_COMPACTOR_EXCLUDE` for scheduled tasks.
- Keep system exclusions **uneditable** and always applied.
- Record skips as `category='user'` (or similar), visible at `-v`.
- Docs / FAQ: *if a game or app breaks, add its folder; `.db` by name is already skipped; compression is reversible.*

### Do not (in that PR)

- Hardcode Wildlands / Ghost Recon / Guild Wars 2 / Ubisoft.
- Blacklist `.pak` / `.ucas` / `.exe` / `.dll`. Compressing those is the product.
- Invent DirectStorage detection from Steam IDs or folder names.
- Support full glob/regex in v1. Prefix matching is what the walker already does.
- Build a decompress UI. Needed later, separate issue.
- Build a background re-compress watcher.
- Skip open files by exclusive locking as the *primary* SQLite fix.
- Let users remove `C:\Windows` from the protected set.

### Optional thinner follow-up, not v1

- One-file exceptions inside an otherwise compressible game (the Wildlands `.tbf` case). Nice, easy to get wrong, not required to close #21.
- Warn if `dstorage.dll` is present under the target (README To-Do).
- FAQ: compression decay after updates; WOF files decompress on copy; Linux `ntfs3` mmap-write failures.

### Interaction with existing machinery

- One-click mode must honour the same user list (Program Files / AppData / Downloads / … will otherwise compress the game the user just excluded).
- Dry-run must show user exclusions in the plan / verbose output.
- `spec.md` section 2 (static directory exclusions) will need a sibling section for user exclusions. Code wins if they disagree.
- Locales: new strings in all of `locales/*.json`.

### Concrete v1 shape (files, functions, blocks)

New module `src/exclusions.py` (keep it out of `file_utils.py`; that file is already the system-path + NTFS-size dump):

- Persist at `%APPDATA%\TrashCompactor\exclusions.txt`, same directory as `incompressible.db` (`skip_logic.get_incompressible_cache` already computes that root). Fallback `~\.cache\TrashCompactor\exclusions.txt`.
- UTF-8, one path per line, `#` comments, blank lines ignored. No globs in v1. Users editing it by hand for Task Scheduler is a feature (mvv-rus).
- `load_user_exclusions() -> list[str]`, `save_user_exclusions(paths)`, `merged_exclude_directories(extra: Iterable[str]) -> list[str]` = system + persisted + extra, each run through `_normalize_for_compare`, de-duplicated, **system entries not removable**.
- Env `TRASH_COMPACTOR_EXCLUDE` split on `;`. CLI `--exclude` appended last so a scheduled task can add a one-off without rewriting the file.

Wire-up:

| File | Function | Block |
|---|---|---|
| `src/compression/file_scan.py` | `iter_files` | Replace `excluded = [_normalize_for_compare(path) for path in DEFAULT_EXCLUDE_DIRECTORIES]` with `merged_exclude_directories(cli_extra)`. Today this is the **only** list the walker sees. |
| `src/file_utils.py` | `should_skip_directory`, `_match_exclusion`, `validate_target_path` | `_match_exclusion` currently iterates `_DEFAULT_EXCLUDE_MAP` only. Build a combined map, or pass extra prefixes. If the *target itself* is user-excluded, `validate_target_path` must refuse with a distinct message (“this folder is in your exclusion list”), not the system-protected wording. |
| `src/skip_logic.py` | `maybe_skip_directory`, `append_directory_skip_record`, `log_directory_skips` | New `category='user'` (the comment in `maybe_skip_directory` already has `system` / `high_entropy` / else). Log at `-v`, not only `-vvv` (system is `-vvv` to stay quiet; user skips are the thing the user asked to see). |
| `fast_walk/src/lib.rs` | `ExclusionIndex`, `walk_and_filter` | **No structural change** if Python passes the merged vector. Prefix match is already `normcase` + `startswith`. Do not add glob parsing in Rust. |
| `main.py` | `build_parser` | `parser.add_argument("--exclude", action="append", default=[], metavar="PATH", help=...)`. Thread onto whatever calls `iter_files` / `create_compression_plan`. |
| `src/launch_flags.py` | `FLAG_METADATA`, `LONG_FLAG_KEYS`, `LaunchState` | Long-only `--exclude` is enough; `-e` is free but easy to typo. Interactive mode should accept the flag next to `-m`. |
| `src/one_click.py` | `resolve_targets` + the compress loop | Load the persisted list once; every target of `ProgramFiles` / `AppData` / … is still walked, but the Rust walker prunes excluded subtrees inside them. |
| `src/gui/handlers.py` | `dispatch_request` + new handlers | `GetExclusions` / `AddExclusion` / `RemoveExclusion`. Folder picker already exists (`Action.choose_folder` / `launch.pick_directory_dialog`). Refuse to add `C:\Windows`. |
| `src/gui/message_types.py` | new request/response dataclasses | Mirror `StartCompressionRequest` style. |
| `src/gui/ui/index.html` + `app.js` + `style.css` | Settings page | New `setting-item` **Excluded folders** under the existing units control: `<ul>` of paths, Add / Remove. Settings page already has the pattern (`Min_Savings`, `No_LZX`, `Single_Worker`). |
| `locales/*.json` | n/a | Every new string. |
| `spec.md` | §2 sibling | “User directory exclusions”: persisted prefixes, merged into the walker, uneditable system set, CLI `--exclude`, category `user`. |
| `docs/1. file scanning.md` | Discovery | One paragraph: user prefixes join the Rust exclusion vector. |

Skip stats: `stats.record_file_skip_counters` / `SkipBulkLedger` do not currently have a `user` bucket because user skips happen at directory-prune time (files never appear). Directory-level `DirectorySkipRecord(category='user')` is the right grain. Verbose listing of every file inside an excluded tree is how Compactor issue #8 happened — don’t.

Tests worth having: (1) excluded child of the scan root never appears in `iter_files`; (2) excluding the scan root itself is a validation error; (3) `C:\Windows` cannot be removed; (4) one-click + an excluded Steam game folder does not compact it; (5) dry-run `-v` prints the user skip.

---

## 8. Further development — ranked, with why

Pulled from this repo’s README To-Do, competitor gaps, and the feedback above. Not a commitment; a map.

### Next (closes real user pain)

1. **User path exclusions** — this document, issue #21. Concrete map in §7.
2. **Decompress / uncompact a folder** — CompactGUI’s “it broke, undo” button. Without it, the exclusion feature tells people to add a folder *after* they already compressed it, and they have no in-app way back. Do not mix this into the exclusions PR.

   WOF vs LZNT1 matters: `compact /u` without `/exe` only clears classic NTFS compression. WOF needs `compact /u /exe`. A file can be one or the other (Wildlands’ LZNT1 workaround is the latter surviving on a tree of the former). Robust uncompact tries WOF first, then LZNT1.

   | File | Function | Block |
   |---|---|---|
   | `src/compression/compression_executor.py` | new `uncompress_file` / `execute_uncompress_plan` next to `compress_file` | Reuse `_run_compact`, `_batch_limits`, `_compact_path`, hidden `STARTUPINFO`. Command: `compact /u /a /exe` then, on non-zero, `compact /u /a`. Same 100/4000 batching. Verify with `is_file_compressed` becoming false (on-disk size ≥ logical **and** attr 0x800 clear **and** no WOF reparse). |
   | `src/compression_module.py` | new wrapper parallel to `execute_compression_plan_wrapper` | Scan is inverted: we *want* already-compressed files. `iter_files` currently re-tags those as `CAT_ALREADY_COMPRESSED` and the planner drops them. Uncompact needs a walk that **keeps** `CAT_ALREADY_COMPRESSED` and ignores entropy. Either a `walk_mode='uncompact'` flag on `iter_files` or a thin sibling that does not call `plan_compression`. |
   | `fast_walk/src/lib.rs` | `finalize_entry` | Today eligible+debug get flipped to `CAT_ALREADY_COMPRESSED`. For uncompact that flip is the *keep* set. Do not require a second walker; Python can treat category 4 as the plan. |
   | `main.py` | `build_parser` | `--uncompact` mutually exclusive with default compress. Do not default this on. |
   | `src/gui/handlers.py` + `ui/index.html` | new `StartUncompact` | Button on the Compress page after analysis, enabled when `already_compressed` count > 0. Not a Settings item. |
   | `src/file_utils.py` | `is_file_compressed` | Reuse as the verifier. Do not add LZNT1-as-compression-algorithm anywhere else. |

3. **Document the known-bad patterns** in FAQ / README (GW2 self-unpack, Wildlands / patch files, DirectStorage, SQL 5118, compression decay, `compact /u /exe`). No code. Stops issue #21 from being re-filed as “Wildlands is broken.” File: `README.md` FAQ. Also a short “if a game breaks” paragraph on the GUI About page (`index.html` already has a “rarely change” note — extend it).

### After that (quality)

4. **DirectStorage detection research** — already on the README backburner. Presence of `dstorage.dll` (or a BypassIO query) → warn, do not auto-skip the whole tree unless the user asked. Microsoft’s veto is file-level; a warning is honest, a silent skip is surprising.

   Cheap implementation that needs no walker change: in `plan_compression`, after the candidate loop, if any path’s basename is `dstorage.dll` / `dstoragecore.dll`, record a notice on `stats` with the parent directory. GUI analysis (`src/gui/pipelines/analysis.py`) and CLI dry-run print it. `fsutil bypassIo query <file>` is the more honest check but requires admin and is per-file; do not do that in v1 of the warning.

5. **Clearer error when WOF is not attached** — on some Server 2016/2019 data volumes `compact /exe` returns “The file system does not support compression” because `wof.sys` is only attached to `C:`. `fltmc attach Wof X:` is the workaround. The tool already rejects non-NTFS; a dedicated message would help the fleet case from the article.

   | File | Function | Block |
   |---|---|---|
   | `src/compression/compression_executor.py` | `_run_compact` / `_record_failure` | Capture stderr when `returncode != 0` (today stdout/stderr are `DEVNULL` unless `capture=True`). If the text contains `does not support compression` **and** `get_volume_details_fast` already said NTFS, log a dedicated `_()` string naming `fltmc attach Wof X:` with `X` from the path’s drive. Do this once per volume, not per file. |
   | `src/file_utils.py` | `validate_target_path` (optional pre-flight) | Could `fltmc instances` up front. Heavier, needs admin to even look sometimes. Prefer the executor-side message first. |

6. **Resume interrupted compression** — README To-Do. Compactor has pause/resume. The executor already isolates failed batches; persisting the remaining plan is the missing piece.

   | File | Function | Block |
   |---|---|---|
   | `src/compression/compression_executor.py` | `execute_compression_plan` | After each successful batch, write remaining `(path, size, algo)` tuples to `%APPDATA%\TrashCompactor\resume.json`. Delete on completion. On start, if the file exists and the target matches, offer to continue. |
   | `src/gui/backend.py` | pause is already an event (`pause_event`) | Pause today is in-memory. Resume-across-process is the missing piece; in-session pause should keep working. |
   | Do not | n/a | Do not invent this to close #21. |

### Later / do not start from this feedback

7. **Background watcher / scheduled re-compress after game updates** — CompactGUI’s answer to compression decay. Large product. This tool’s current answer (“re-run is free”) is coherent. Do not build a tray service to close #21. The Habr article already sold Task Scheduler + CLI; `--exclude` + a scheduled `trash-compactor.exe C:\Games -y` is the watcher for this project.
8. **Native `FSCTL_SET_EXTERNAL_BACKING` / WofApi instead of spawning `compact.exe`** — README long-term. Compactor already does this. Gains: no cmdline length / batching limits, better per-file error, possible exclusive lock. Cost: now you own WOF edge cases (filter not attached, reparse points, ADS). Not a prerequisite for exclusions.
9. **Community skip-extension wiki** — CompactGUI’s model. This project’s entropy pass is the substitute. Adding a live community list means hosting, trust, and stale data.
10. **Mixing NTFS compression with Windows Dedup** — Habr @gotch: worse savings and a corruption risk if compression is volume-wide. Out of scope; a one-line warning if someone ever asks.

### Optional heuristics that are *not* a game denylist (ranked, all after exclusions)

These exist so a later session does not reinvent a Ubisoft list. None of them close #21.

| # | Heuristic | Closes | File / function | Ship? |
|---|---|---|---|---|
| H1 | SQLite magic on extensionless files | Issue #21 `.db` remainder | `fast_walk` `scan_dir` + new category | Follow-up, high confidence |
| H2 | ArenaNet DAT magic (`xxAN\x1A`) on `.dat` ≥ 1 MiB | GW2 self-unpack of `Gw2.dat` without naming the game | `fast_walk` `skip_magic` next to `skip_extension_name` | Follow-up, medium. Document as “writable MFT archive,” not “Guild Wars.” |
| H3 | `dstorage.dll` basename in the plan | DirectStorage BypassIO veto | `plan_compression` | Follow-up warning, no skip |
| H4 | Turbine/Funcom container magics | LOTRO / SWL | unknown; research first | Not until a header spec is in hand |
| H5 | Skip `.tbf` / hardcode `PCgr_terrainlin1.tbf` | Wildlands Mode C | n/a | **No.** Filename rot. Folder exclusion. |
| H6 | Skip all `.dat` / files > 2 GiB / `*patch*` | Feels productive | `config.SKIP_EXTENSIONS` | **No.** Throws away the product. |

H2 sketch, for when it is time (not v1):

```rust
// fast_walk/src/lib.rs, next to skip_extension_name
fn skip_writable_archive(name: &OsStr, path: &Path, size: u64) -> bool {
    if size < 1024 * 1024 { return false; }
    let ext = Path::new(name).extension().and_then(|e| e.to_str()).unwrap_or("");
    if !ext.eq_ignore_ascii_case("dat") { return false; }
    let mut hdr = [0u8; 4];
    let Ok(mut f) = std::fs::File::open(path) else { return false; };
    use std::io::Read;
    if f.read(&mut hdr).ok() != Some(4) { return false; }
    // ArenaNet: version byte + b"AN\x1a"  (GW2 is typically b"3AN\x1a")
    hdr[1] == b'A' && hdr[2] == b'N' && hdr[3] == 0x1A
}
```

Call it from `scan_dir` after the extension check, before `classify`. Map a true to a new category so Python can say “writable archive header” at `-vvv`. Do not reuse `CAT_EXTENSION`.

Wildlands will still compact under H2. That is acceptable: Mode C is not a live-archive problem, and the honest fix is the user folder. Do not “complete the set” by adding `.tbf`.

### Explicitly out of scope / bad ideas caught in review

- Default-excluding Program Files or AppData.
- Switching folders to classic LZNT1 so new files inherit compression. That reintroduces XP-era fragmentation and CPU cost. The Wildlands report that LZNT1 on `PCgr_terrainlin1.tbf` *worked* is a user-level curiosity, not a cue to mix algorithms. File that would be wrongly tempted: `compression_executor.compress_file` growing a `/exe`-less branch.
- Skipping all `.exe`/`.dll` “for anti-cheat.” Game-Compressor-Tauri does this; it throws away a large part of the savings and is not what CompactGUI or this tool do. `SKIP_EXTENSIONS` must not gain `.exe` / `.dll`.
- Using `WimBootCompress.ini` as a default user list (Compactor issue #60). That file is for CompactOS / WIMBoot, not for arbitrary user trees.
- Treating Habr “SSD wear / CPU” scepticism as a reason to compress less aggressively. The skip list + entropy + already-compressed check *is* the answer; keep it.
- A built-in denylist of game titles, Steam IDs, Ubisoft folder names, or `PCgr_terrainlin1.tbf`. CompactGUI has a 100k-submission wiki and still cannot keep one. This repo’s answer is user prefixes + (later) format magics.
- Falling back to LZNT1 for “files that look like patches.” Same as the inherit trap, plus now you are guessing.

---

## 9. `ironing_out.md` — closed, do not reopen

Verified in tree before the file was deleted (v0.7.0 / v0.7.1):

| Item | Where it landed |
|---|---|
| Per-file entropy pass re-reading sampled files | `already_sampled` in `_filter_certainly_incompressible_files` |
| Skipped root not cascading to subdirs | `_filter_high_entropy_directories` seeds the root skip |
| Dead `_chunk(size=…)` | Dropped; `_batch_limits()` is the single batch policy |
| Full HDD probe on every CLI run | `confirm_hdd_usage` → `get_volume_details_fast` first |
| Verification stats race | `_record_failure` takes `stats_lock` |
| `GetCompressedFileSizeW` at ≥ 4 GiB | `GetLastError` distinction is correct. **Do not “fix” this into a bug.** |
| Entropy constants duplicated in Python and Rust | `EntropySamplingParams` passed into `probe_directories_parallel` |

If a later change touches those areas, read `spec.md` first. The code still wins if they disagree.

---

## 10. One-paragraph brief for a future implementation session

Issue #21 is user **directory exclusions**, not a game denylist and not another extension table. `.db` by suffix is already skipped; extensionless databases and broken game trees are not. One-click mode *will* compact Wildlands / GW2 / SWL / LOTRO today: `.tbf`, `.forge`, `.dat` are eligible, entropy *wants* them, and they live under `ProgramFiles*`. The failures are three WOF mechanics (write-unpack of a live archive, in-place binary patching, WOF-hostile mmap on one 5.4 GiB terrain delta) — not “Ubisoft is special.” Implement prefix matching identical to the system exclusions, prune inside `fast_walk` by handing a merged vector to `walk_and_filter`, persist next to the incompressible cache, expose CLI `--exclude` and a Settings UI, keep `Windows` uneditable. Do not ship decompress, a watcher, glob syntax, `.tbf`/`.dat` extension bans, or ArenaNet magic in the same change. Follow-ups, in order: FAQ, decompress UI (`compact /u /exe` then `/u`), SQLite header on extensionless files, `dstorage.dll` warning, then maybe ArenaNet DAT magic as a writable-archive skip.

---

## 11. Point-by-point implementation map

Every numbered item in this file, with the file / function / block to add or the explicit non-change. This is the session-brief for whoever implements; it is not a commitment to do it all at once. **PR 1 is still just user path exclusions.**

### §1 Issue #21 request

Two problems in one issue. Split them in the implementation, not in the issue tracker.

| Sub-request | v1? | Where |
|---|---|---|
| Exclude a game folder (Wildlands, Ghost Recon) | Yes | §7 / `src/exclusions.py` + walker merge |
| `.db` glitches | Partially already | `config._DATABASES`. Remainder = user path + later SQLite magic (§4) |

No new file for the issue text itself.

### §2 What the program already does

| Existing piece | File | Touch in exclusions PR? |
|---|---|---|
| `SKIP_EXTENSIONS` | `src/config.py` | **No.** Do not add `.tbf` / `.dat` / `.pak` / `.forge`. |
| System prune | `src/file_utils.py` `_default_excluded_directories`; `fast_walk` `ExclusionIndex` | **Read, don’t rewrite.** Merge user prefixes *alongside*. |
| Entropy / incompressible cache | `compression_planner._filter_high_entropy_directories`; `skip_logic.get_incompressible_cache` | **No.** Wrong tool for compatibility. |
| CompactOS | `src/one_click.py` | Honour user exclusions; do not exclude Program Files by default. |
| `iter_files` excluded list | `src/compression/file_scan.py` lines that build `excluded` from `DEFAULT_EXCLUDE_DIRECTORIES` only | **Yes. This is the load-bearing line.** |

### §3.1 Games

| Title | Hits current tool? | Automatic skip possible without naming it? | v1 action |
|---|---|---|---|
| Wildlands | Yes (`.tbf` 5.4 GiB is eligible, LZX) | No honest one. Mode C, filename-specific, LZNT1-tolerant | User folder. FAQ names the one file as trivia, not as a rule. |
| Ghost Recon (unspecified) | Maybe | No | User folder. Do not alias to Wildlands. |
| Guild Wars 2 | Yes, enthusiastically (huge compressible `Gw2.dat`) | Later: ArenaNet DAT magic (H2) | User folder now. Magic later, not as “GW2.” |
| Secret World Legends | Yes | Not until Funcom magic is known | User folder. |
| LOTRO | Yes | Not until Turbine DAT magic is known | User folder. |

No `GAMES_SKIP` list anywhere. No `src/games.py`.

### §3.2 DirectStorage

- Not v1. Warning-only follow-up in `plan_compression` on basename `dstorage.dll` / `dstoragecore.dll`.
- Do not query Steam IDs. Do not skip `.ucas` because Forspoken uses them.

### §3.3 Unreal `.pak` / `.ucas` / `.utoc`

- No change. Keep compressing. User folder if a specific title’s reader is broken.

### §3.4 Not a “game doesn’t work”

- FAQ only: decay, copy unpacks, Proton mmap. `README.md`. GUI About paragraph in `index.html`.

### §3.5 Why hardcoding names is a bad idea

- Binding constraint on every later heuristic. If a proposed skip needs a title string, it is rejected.

### §4 Databases

| Piece | File | Function | v1? |
|---|---|---|---|
| Keep `_DATABASES` | `src/config.py` | existing | Already done |
| User path for `MSSQL\DATA`, extensionless SQLite dirs | `src/exclusions.py` | user list | Yes, as a capability, not a default entry |
| SQLite 16-byte header | `fast_walk/src/lib.rs` | new `skip_magic`, empty-extension files only | Follow-up |
| Exclusive lock | `compression_executor.compress_file` | `CreateFile` with no share | **Not v1.** Compactor 0.10 did this after one unreproduced report |
| Default-skip `*.log` | `config.py` | `_flatten` | **No** |

### §5 Habr comments

Covered by the table in §5. Only mvv-rus (`--exclude` CLI) and mixsture (path skip for DBs, FAQ for decay) change code. evgen_b is a non-goal: no `compact /c /s` without `/exe` after a run (would live in `compression_executor` if someone tried).

### §6 Copy the right parts

| Copy | Don’t copy |
|---|---|
| Compactor path-prefix prune in the walker (`ExclusionIndex` already exists) | CompactGUI game wiki |
| Compactor “excluded dirs skipped entirely” (issue #8) | Compactor glob `*:\Windows*` as a *user*-editable default |
| CompactGUI README DirectStorage caveat (later warning) | CompactGUI Background Watcher |
| — | Compactor exclusive lock as the SQLite answer |

### §7 User path exclusions — the PR

See the file/function/block table in §7. Summary of new surface:

```python
# src/exclusions.py (new)
def load_user_exclusions() -> list[str]: ...
def save_user_exclusions(paths: list[str]) -> None: ...
def merged_exclude_directories(extra: Iterable[str] = ()) -> list[str]:
    # DEFAULT_EXCLUDE_DIRECTORIES + file + env + extra
    # system paths always win / cannot be removed
```

```python
# src/compression/file_scan.py  (iter_files)
excluded = merged_exclude_directories(cli_excludes)
for batch in fast_walk.walk_and_filter(
    os.fspath(root), excluded, sorted(SKIP_EXTENSIONS), ...
):
```

```python
# main.py  (build_parser)
parser.add_argument("--exclude", action="append", default=[], metavar="PATH",
                    help=_("Directory to skip (repeatable). Also TRASH_COMPACTOR_EXCLUDE."))
```

```python
# src/skip_logic.py  (maybe_skip_directory)
# if user prefix matches:
#   DirectorySkipRecord(..., category='user')
# log_directory_skips: print 'user' at verbosity >= 1
```

GUI: Settings `setting-item` list + `GetExclusions`/`AddExclusion`/`RemoveExclusion` in `handlers.py`. One-click must load the same file.

### §8 Ranked further development

| # | Item | First file to touch | Same PR as exclusions? |
|---|---|---|---|
| 1 | User exclusions | `src/exclusions.py`, `file_scan.iter_files` | **This PR** |
| 2 | Decompress | `compression_executor.py` new `execute_uncompress_plan`; `compact /u /exe` then `/u` | No |
| 3 | FAQ | `README.md`, GUI About | Can be a tiny docs commit next to #1 |
| 4 | DirectStorage warning | `plan_compression` basename check | No |
| 5 | WOF-not-attached message | `compression_executor._run_compact` capture stderr | No |
| 6 | Resume plan | `execute_compression_plan` writes `resume.json` | No |
| 7 | Watcher | none | Never, from this feedback |
| 8 | Native WofApi | README long-term | No |
| 9 | Community wiki | none | Never |
| 10 | Dedup warning | FAQ one-liner if asked | No |
| H1 | SQLite magic | `fast_walk` | No |
| H2 | ArenaNet DAT magic | `fast_walk` `skip_writable_archive` | No |
| H3 | dstorage basename | `plan_compression` | = #4 |
| H4–H6 | Funcom/Turbine magics, `.tbf`, size/extension bans | none | No / never |

### §9 ironing_out.md

Closed. Do not reopen. If exclusions touch the walker, re-read `spec.md` §2 and §4 (walk behaviour) so the new prefixes use the same `normcase`/`normpath`/`startswith` rules, including the `Windows.old.` dotted namespace — user exclusions should **not** grow a dotted-namespace rule; only exact prefix.

### Locales, spec, docs (cross-cutting for PR 1)

| File | What |
|---|---|
| `locales/en.json` (and `de`, `es`, `fr`, `pt`, `ru`) | `--exclude` help, Settings “Excluded folders”, “Add folder”, “Cannot exclude a protected system path”, “Skipped N user-excluded directories”, dry-run lines |
| `spec.md` | New §2.1 User Directory Exclusions. Existing §2 stays the uneditable system set. |
| `docs/1. file scanning.md` | Discovery: merged exclusion vector. |
| `docs/6. one click mode.md` | One-click honours the same list. |
| `docs/7. gui mode.md` | Settings page exclusion editor. |
| `README.md` FAQ | If a game breaks: add the folder, re-run; uncompact is `compact /u /exe /s:"path"` until the UI exists. Name GW2 / Wildlands / DirectStorage / SQL 5118 as examples, not as a denylist. |

### Suggested PR cut

1. **PR 1 (closes #21):** `src/exclusions.py` + walker merge + CLI `--exclude` + env + Settings UI + `category='user'` logging + locales + spec/docs. No decompress, no magic bytes, no `dstorage` warning, no `.tbf`.
2. **Docs note** (can ride with PR 1): README FAQ with the three failure modes and `compact /u /exe`.
3. **PR 2:** Decompress UI / `--uncompact`.
4. **PR 3:** SQLite header + (optional) `dstorage.dll` warning.
5. **PR 4 (maybe never):** ArenaNet DAT magic. Only if GW2 reports start hitting *this* repo after exclusions have shipped and people refuse to add a folder.
