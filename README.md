<div align="center">

# 🗑️ Trash-Compactor

**Intelligent filesystem-level compression for Windows 10/11** - find the compressible files nobody knew were there, and shrink them without touching a thing you care about.

[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-blue?style=for-the-badge&logo=windows&logoColor=white&labelColor=1b1f27&color=2f81f7)](https://www.microsoft.com/windows)
[![NTFS](https://img.shields.io/badge/filesystem-NTFS%20only-blueviolet?style=for-the-badge&labelColor=1b1f27&color=7d5cfc)](#limitations)<!-- [![Python](https://img.shields.io/badge/python-3.11%20-%203.13-yellow?style=for-the-badge&logo=python&logoColor=white&labelColor=1b1f27&color=f0c94a)](#option-2-running-from-source)
[![Rust](https://img.shields.io/badge/rust%20engine-fast__walk-orange?style=for-the-badge&logo=rust&logoColor=white&labelColor=1b1f27&color=f0753a)](#native-rust-engine) -->
[![License](https://img.shields.io/badge/license-MIT-brightgreen?style=for-the-badge&labelColor=1b1f27&color=4bc34b)](LICENSE)
[![Release](https://img.shields.io/badge/latest-v0.7.0-blue?style=for-the-badge&labelColor=1b1f27&color=2f81f7)](https://github.com/me-when-the-uh/trash-compactor/releases/latest)
[![Build date](https://img.shields.io/badge/build%20date-who%20cares-blue?style=for-the-badge&labelColor=1b1f27&color=6cb6ff)](src/version.py)
[![RAM](https://img.shields.io/badge/RAM%20usage-25%25%20lower-brightgreen?style=for-the-badge&labelColor=1b1f27&color=4bc34b)](#native-rust-engine)
<!-- [![Stars](https://img.shields.io/github/stars/me-when-the-uh/trash-compactor?style=for-the-badge&labelColor=1b1f27&color=2f81f7&logo=star&logoColor=white)](https://github.com/me-when-the-uh/trash-compactor/stargazers)
[![Issues](https://img.shields.io/github/issues/me-when-the-uh/trash-compactor?style=for-the-badge&labelColor=1b1f27&color=e5534b&logo=github&logoColor=white)](https://github.com/me-when-the-uh/trash-compactor/issues)
[![Forks](https://img.shields.io/github/forks/me-when-the-uh/trash-compactor?style=for-the-badge&labelColor=1b1f27&color=8f9bb3&logo=git&logoColor=white)](https://github.com/me-when-the-uh/trash-compactor/network/members) -->
*A utility for intelligent file compression on Windows 10/11 using the built-in NTFS compression algorithms and Windows' built-in `compact.exe` utility.*

<!-- ### Ten thousand stars can't be wrong

<sup>*(stars may not exist yet, but the ambition is there)*</sup> -->

</div>

---

## Table of Contents

- [What is this, actually?](#what-is-this-actually)
- [Features](#but-for-real-though-here-are-the-features)
- [The Competition](#the-competition)
- [Limitations](#limitations)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [How it works (under the hood)](#how-it-works-under-the-hood)
- [Development](#development)
- [FAQ](#faq)
- [Fun Facts](#fun-facts)
- [To-Do](#to-do)

---

## What is this, actually?

Compressing files at the **filesystem level** is quite different from your average `.zip` or `.7z` compression - which is strictly for archival purposes. NTFS compression is **seamless**: you won't see a difference, but the apps will shrink in size without deleting anything. Every program that can read the file still can. Windows just stores it more cleverly.

Unlike [CompactGUI](https://github.com/IridiumIO/CompactGUI) and [Compactor](https://github.com/Freaky/Compactor) - tools based on `compact.exe` and primarily designed for compressing Steam games - this program **automatically scans through the files**, evaluating their compressibility with a complex but fast algorithm, and picking the optimal compression algorithm based on file size. This lets you squeeze the most out of the compression algorithms and get even smaller file sizes, all while avoiding unnecessary compression, maximising overall system performance in comparison with the aforementioned tools, and preventing excessive SSD wear, keeping things DRY.

Compressing large directories to gain extra storage space will be so free and without downsides that it'll be the closest thing to having **"free real estate"**.

### Why filesystem compression beats archives

| | Trash-Compactor (NTFS) | ZIP / 7Z archive |
|---|---|---|
| Visible to apps | ✅ Fully transparent - apps read files normally | ❌ Files hidden inside an archive |
| Space freed | ✅ Immediately on disk | ✅ Only while archive exists |
| Original file | ✅ Stays in place, smaller | ❌ Moved into a `.zip`, originals deleted |
| One-time vs ongoing | ✅ Compress once, forget forever | ❌ Re-archive every time data changes |
| Nothing to remember | ✅ No "open the archive" step | ❌ Must remember where things are |
| SSD wear | ✅ Skipped unless clearly worth it | ⚠️ Writes everything again |

### Performance by the numbers

> **Warning:** the following chart was conjured up with a level of statistical rigour usually reserved for marketing departments. Values are real, methodology is... *flexible*

| Metric | Improvement |
|---|---|
| RAM usage during scan | **25% lower** (sliding-window algorithm) |
| Directory scan throughput | **~60% faster** (Rust 🚀🚀🚀🚀🚀🚀🚀🚀) |
| Entropy scan speed | **exists** |
| Compression batches | **50% faster** (multi-threaded `compact.exe`) |



---

## The Competition

Let's be real - this isn't a one-horse race. Two other tools do roughly the same thing with `compact.exe`:

- **[CompactGUI](https://github.com/IridiumIO/CompactGUI)** - a GUI that compresses whatever you point it at. No scanning, no entropy analysis, no guardrails. It's the sledgehammer; we're the scalpel. With a laser. And a spreadsheet.
- **[Compactor](https://github.com/Freaky/Compactor)** - compresses Steam games. Great if all you do is play Steam games. If you want the whole drive re-architected for density, that's where Trash-Compactor comes in.

### Head-to-head

| | Trash-Compactor | Compactor | CompactGUI |
|---|---|---|---|
| Automatic scan | ✅ | ❌ | ❌ |
| Entropy analysis | ✅ | ❌ | ❌ |
| Smart algorithm per file | ✅ | ❌ | ❌ |
| Whole-drive focus | ✅ | ❌ | Steam-only |
| HDD mode | ✅ | ❌ | ❓ |
| Dry-run preview | ✅ | ❌ | ❌ |
| Developer's biased opinion | ✅ | 😐 | 😐 |

---

## But for real, though. Here are the features

- **Automated compression** using Windows NTFS compression
- **Simple and intuitive interface** - a GUI that a grandparent could operate, and a CLI for the rest of us
- **Smart algorithm selection** based on file size
- **Entropy analysis** to evaluate compression potential and compress only the files that make sense to compress
- **Configurable minimum savings threshold** (`--min-savings`) with interactive controls
- **Multiple operation modes** for different use cases
- **Skips poorly-compressible formats** (zip, media files, etc.)
- **Skips already-compressed files**
- **Skips LZX compression** on computers identified as too slow to handle it without performance losses *(taking care of users)*
- **Detailed compression and file throughput stats**
- **GUI** with progress bar, scan/entropy timing breakdown, and Defender performance notice
- **Native Rust scan and entropy engine** (`fast_walk`) for high-throughput directory analysis
- **HDD mode** - a gentler single-worker pipeline for spinning drives with a defrag hint after the run
- **Deterministic CLI exit codes** and a `-y` flag for scripted dry-runs
- **Shared path validation** for CLI and GUI before any scanning begins

---

## Limitations

- It's only for storage devices with an **NTFS** file system, like your system drive and external flash drives and SSDs if they're formatted to use NTFS. If it's FAT32, exFAT or ReFS - it won't work for you.
- It's best to assume that it likely won't work on **network drives** even if they are formatted to NTFS *(haven't tested it)*.
- **Spinning hard drives** might get fragmented, negatively impacting read performance - that's why HDD mode exists, and why it gives you a defrag hint afterwards.

---

## Requirements

- **Windows 10/11**
- **Optional:** Administrator privileges are optional for normal compression runs. They are **required** for compressing Windows system binaries using CompactOS (`compact.exe /compactos:always`) in 1-click mode.

---

## Installation

### Option 1: Using the Executable (Recommended)

1. [Download the latest release](https://github.com/me-when-the-uh/trash-compactor/releases/latest)
2. Run the executable file

### Option 2: Running from Source

Supported on Windows 10/11 (64-bit). The native `fast_walk` extension is **required** - the pure-Python fallback was removed.

#### The easy way: let the build script do it

The only prerequisites are **Python 3.11-3.13 (64-bit)** from [python.org](https://www.python.org/downloads/windows/) and **Rust** via [rustup](https://rustup.rs) (MSVC toolchain, with Visual Studio Build Tools). The script checks for both and tells you exactly what's missing if you forgot.

1. Clone and navigate to the repository:

   ```powershell
   git clone https://github.com/me-when-the-uh/trash-compactor.git
   cd trash-compactor
   ```

2. Run the build script:

   ```powershell
   powershell -ExecutionPolicy Bypass -File build.ps1
   ```
   or simply:
   ```
   ./build.ps1
   ```

   That single command installs the Python dependencies, builds the Rust extension, bundles the single-file executable, and verifies the frozen build before calling it done.

3. Let it cook. After a while, `dist\trash-compactor.exe` is ready for action.

(The exit-code ladder of every possible failure mode lives in the [Development](#development) section. Also, even though malware rarely happens nowadays, don't just blindly build or run stuff even if the code is out in the open...)

#### The arduous way - run the build commands yourself

If you'd rather download dependencies and type out the build commands on your own, or you want to run from source instead of bundling an executable, here's how it goes:

1. Install **Python 3.11-3.13 (64-bit)** from [python.org](https://www.python.org/downloads/windows/). The Rust extension is built for the interpreter you run with.
2. Install **Rust** via [rustup](https://rustup.rs) (MSVC toolchain) and ensure Visual Studio Build Tools with the *"Desktop development with C++"* workload is present.
3. Clone and navigate to the repository:

   ```powershell
   git clone https://github.com/me-when-the-uh/trash-compactor.git
   cd trash-compactor
   ```

4. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   python -m pip install maturin
   ```

5. Build and install the Rust extension:

   ```powershell
   cd fast_walk
   maturin build --release
   python -m pip install target\wheels\fast_walk-*.whl --force-reinstall
   cd ..
   ```

6. Run from source:

   ```powershell
   python main.py C:\path\to\compress
   ```

   Or bundle a single-file executable:

   ```powershell
   python -m PyInstaller --clean --noconfirm trash-compactor.spec
   ```

   The development machine also needs the [Microsoft WebView2 runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) for the GUI path (it's preinstalled on Windows 11; usually present on Windows 10). CLI mode works without it.

<details>
<summary>📜 Verify the frozen build</summary>

```powershell
$env:TRASH_COMPACTOR_DIAGNOSTIC = "1"
.\dist\trash-compactor.exe
```

It should print `frozen=True fast_walk=True fast_walk_version=0.1.0 probe_directories_parallel=True`.

---
</details>

## Usage

1. Run the program.
   - Use Administrator only if you want 1-click mode to launch Windows CompactOS.
2. Choose the mode - either the 1-click run mode to get most things done fast, or choose the directory.
3. The program will automatically:
   - Scan all files recursively
   - Skip poorly compressible files
   - Apply optimal compression algorithms
   - Display compression statistics

### GUI-based Interactive Configuration

Launching **without arguments** opens a GUI window that lets you browse to the target directory, toggle flags, and adjust the minimum savings threshold before starting.

- Choose the directories you wish to compress or click on **"Quick Compression"**.
- Tweak settings such as **Minimum Savings %** to change the skip threshold, disable LZX compression so as to only dynamically use XPRESS4K/8K/16K algorithms, or use a single worker if you have a spinning hard drive.
- Press **"Analyse"** to see what can be compressed.

  | Projected value colour | Meaning |
  |---|---|
  | 🟡 **Yellow** | Some files may compress, but you'll save *less* than the Minimum Savings % on average. |
  | 🟢 **Green** | You'll save **1.01×-1.99×** of the Minimum Savings % on average. |
  | 🔵 **Blue** | You'll save **more than double** the Minimum Savings % on average. *Go for it!* |

- If it looks good to you, press **"Compress"** to compress the files in the directory.

### CLI-based Scripting Operation Modes

Trash-Compactor offers three distinct operation modes to handle different scenarios:

#### 1-Click / Unattended Mode (Preferred)

Pressing or passing `1` upon starting will run this mode. Most users can just compress their directories once and forget about it. Designed to be extremely simple to use for a casual user, a system administrator, refurbisher, or the so-called *family tech support*.

This mode will automatically compress the following directories:

| Directory | Notes |
|---|---|
| `Program Files` | |
| `Program Files (x86)` | including your Steam folder |
| `AppData` | |
| `Downloads` | |
| `Documents` | |
| `ProgramData` | |
| `Windows` | via CompactOS, **only when running as Administrator** *(you will be prompted first)* |

Expect **at least 15 GB** to be saved on stock Windows installations.

- If launched **without** Administrator privileges, 1-click mode still performs regular compression on accessible files and directories, while CompactOS is skipped automatically.
- If launched **with** Administrator privileges, 1-click mode asks whether to start CompactOS before scanning begins. If you accept, CompactOS starts immediately while the directory scan/analysis runs. In **CLI** 1-click mode this opens a separate PowerShell window; in **GUI** quick compression it runs hidden with a live status indicator in the app.

#### Normal Mode

For first-time compression of directories with optimal performance.

Be aware that temporarily disabling the anti-virus or whitelisting this program is going to greatly improve the compression speed.

```powershell
.\trash-compactor.exe C:\path\to\compress
```

#### Dry-run Mode (`-d`)

To check how well a directory will compress **without writing anything** to the drive. SSDs have a finite amount of data that can be written, so some users might check if it's worth bothering to compress their directory.

```powershell
.\trash-compactor.exe -d C:\path\to\compress
```

#### Disabling (`-x`) or Forcing (`-f`) LZX Compression

LZX compression is turned **on** for large files by default.

LZX compression is resource-intensive and files will take some time to compress, though it does result in better compression of both compressible binaries and the files that XPRESS16K doesn't compress as well. But if you have a computer that was built or made before AD 2021, or if battery life is absolutely critical for you *(a big problem on Intel Coffee Lake laptops)*, you may want to disable it.

#### Running with a single worker (`-s`) for HDDs

HDDs read data sequentially and can't handle the random I/O that parallel queries generate - every seek costs more IOPS than parallelism gains. When a spinning drive is detected, **HDD mode** runs the whole pipeline single-worker: the scan visits directories in discovery order, entropy sampling reads files in order, and compression uses smaller batches, so the disk head moves forward instead of jumping between folders.

Use `-s` to additionally force sequential scan/entropy on very old drives where even ordered reads thrash *(HDD mode already does this)*.

### Additional Scripting Options

| Flag | Description |
|---|---|
| `-v` / `--verbose` | Show exclusion decisions with entropy sampling. Supports 4 levels of verbosity, up to `-vvvv` for debug logs. |
| `-m` / `--min-savings <percent>` | Set the minimum estimated savings (0-90, default 15%). Directories predicted to save less space are skipped automatically. |
| `-y` / `--yes` / `--no-prompt` | Proceed with compression after a dry-run analysis without prompting. |

### Exit Codes

Deterministic exit codes for scripting:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Validation or internal error |
| `2` | Usage error (bad arguments) |
| `130` | Cancelled by the user |

### Incompressible Cache Database

Trash-compactor stores high-entropy directory decisions in an on-disk cache to avoid re-sampling the same low-value directories on future runs. It's an append-only text file, one entry per line: `xxhash64(path + volume serial) [mtime]`

- **Located in** `%APPDATA%\TrashCompactor\incompressible.db`
- **Fallback path**, if `%APPDATA%` is not set: `~/.cache/TrashCompactor/incompressible.db`
- The **volume serial binds an entry to its drive**, so the same path on a different drive never collides
- Each entry records the directory's time when it was modified; a newer `mtime` resets the cache entry

The cache is loaded into memory when the program is started. New high-entropy entries are staged during analysis and written to disk only after a compression run completes.

---

## Inner workings

### The pipeline

1. **Scan** - a native Rust walker (`fast_walk`) traverses the tree with a pool of threads, classifying every file by extension, size, and whether it's already compressed, and grabbing the NTFS on-disk size in the same pass.
2. **Entropy analysis** - candidate directories are sampled (LZ4 gate, then zlib level 2) on multiple 16 KiB windows placed at the start, middle, and end of the largest files. Directories that can't clear the Minimum Savings % threshold are skipped in bulk. Every candidate file that survives the directory gate (≥ 8 KiB) is then probed individually, largest first per directory; a file is dropped only when **every** sampled window fails the LZ4 gate (certainly incompressible), so a pre-compressed multi-GB archive next to compressible files doesn't drag the whole directory through `compact.exe` for nothing.
3. **Compression** - the plan is grouped by algorithm and executed in batches of `compact.exe` calls, with per-algorithm concurrency limits and a timeout on every invocation.

### Native Rust engine

The two hottest phases - walking and entropy probing - are extracted into a small Rust crate, `fast_walk`, exposed to Python via PyO3. It uses **rayon** for parallelism and **mmap** for large files, and streams results back to Python as small batches so the GUI stays responsive without buffering an entire multi-gigabyte tree in memory. Measured effects: higher absolute throughput *and* lower RAM residency.

> **Heads-up for packagers:** the wheel is *mandatory* for source runs. `fast_walk/__init__.py` re-exports the native module from site-packages and raises a clear error with build instructions when the wheel is missing.

### The pipeline, but with more arrows

Because every project needs a diagram:

```text
┌─────────────┐     ┌─────────────────┐     ┌──────────────────────┐
│   SCAN      │ ──▶ │  ENTROPY CHECK  │ ──▶│      COMPRESSION     │
│  (Rust)     │     │  (also Rust)    │     │ (compact.exe batches)│
└─────────────┘     └─────────────────┘     └──────────────────────┘
        │                    │                        │
        │  "compressible?"   │  "worth it?"           │  "done!"
        ▼                    ▼                        ▼
    skip stuff          skip more stuff           victory over Big AI's SSD bribes
```

<!-- ---

## Trophy Gallery

**Awards so prestigious that I made them up myself:**

<div align="center">

[![Award](https://img.shields.io/badge/🏆-Best%20in%20Show%20(e%2Dwaste%20division)-gold?style=for-the-badge&labelColor=1b1f27)](https://github.com/me-when-the-uh/trash-compactor)
[![Award](https://img.shields.io/badge/🥇-Gold%20Medal%20for%20Free%20Real%20Estate-silver?style=for-the-badge&labelColor=1b1f27)](https://github.com/me-when-the-uh/trash-compactor)
[![Award](https://img.shields.io/badge/🎖️-Honorary%20PhD%20in%20Squeezing-silver?style=for-the-badge&labelColor=1b1f27)](https://github.com/me-when-the-uh/trash-compactor)

</div>

**And the physical trophies I didn't have budget for:**

```text
        ___
       /   \          ╭─────────╮          ╱╲╱╲
      |  🏆  |         │  DRY    │         ╲╱╲╱
       \___/          │  LEGEND │          │  │
        | |           ╰─────────╯          ╰──╯
   Best in Show      Best Practices       Free Real Estate
```

---

## Hall of Fame of Technical Details

The kind of details that belong in a README's README:

- The **first byte** of every file is read with the same enthusiasm as the last - that's what NTFS compression is built on.
- The scan runs in **Rust**, the GUI runs in **webview**, and the whole thing is orchestrated by **Python**. It's the tech-stack equivalent of a three-legged race, and somehow it wins.
- Files below **8 KiB** are never even considered - they're too small to matter, and they know it.
- The **incompressible cache** remembers every directory you've ever written off. It's the only grudge this tool holds.
- **CompactOS** is offered only when you're an Administrator - because compressing `C:\Windows` without permission is a one-way ticket to a blue screen.
- Every `compact.exe` call has a **timeout**, because some files are so stubborn they'd rather hang than compress.
- **HDD mode** reads directories in discovery order so the disk head moves forward like it's walking a hallway, not playing ping-pong. -->

<details>
<summary>📚 Read the fine print (technical specifications)</summary>

| Spec | Value |
|---|---|
| Minimum file size | 8 KiB |
| Entropy sample window | 16 KiB (customisable) |
| Sample windows | 3 to 20 |
| Entropy probe | LZ4 gate → zlib level 2 |
| Worker baseline | all CPU cores - 1 |
| Cache | `xxhash64(path + volume serial) [mtime]` |
| Minimum savings threshold | 15% (configurable 0-90%) |

</details>

<!-- ---

## Testing

**Status:** swearing it works on my machine.

| Test | Result |
|---|---|
| `python main.py C:\Games` | ✅ Compresses |
| `python main.py -d C:\Games` | ✅ Analyses, doesn't touch anything |
| `-y` after a dry run | ✅ Proceeds without asking |
| Running as Administrator | ✅ CompactOS offered |
| Running without Administrator | ✅ Everything else still works |
| HDD mode on a spinning drive | ✅ Slower, but gentle |
| Folder picker in the GUI | ✅ Pick a folder, see results |
| Unplugging the drive mid-run | ❓ Not tested - don't try this at home |

> The last row is a threat. It's also an invitation. Pick one. -->

---

## Development

To contribute to this project:

1. Create a new branch for your feature.
2. Submit a pull request.

### The build script: a ladder with 8 rungs

`build.ps1` compiles the Rust extension, bundles the executable, and verifies it actually runs. It returns a different exit code for each way it can fail, because I like to be specific about failures:

| Code | What went wrong |
|---|---|
| `0` | Success |
| `1` | Python 3.11+ (64-bit) not found |
| `2` | Rust toolchain not found |
| `3` | MSVC linker missing or extension build failed |
| `4` | Python dependency install failed |
| `5` | `fast_walk` wheel build/install failed |
| `6` | PyInstaller build failed |
| `7` | Frozen executable verification failed |

Run it with `powershell -ExecutionPolicy Bypass -File build.ps1` - or skip the verification with `-SkipVerify` if you're feeling reckless.

### Like this project?

Put a "star" if you find this project helpful or cool. I don't know what they do, apart from giving some small bragging rights, but maybe it might get introduced to more people, which will be great, as the collective storage savings will contribute to less e-waste - especially if an SSD is soldered onto the laptop's motherboard and is not replaceable, turning the laptop into a paperweight if it fails prematurely - which it surely will.

---

## FAQ

**Will my files still open normally?**
Yes. NTFS compression is fully transparent - every program reads the files exactly as before, Windows just stores them more compactly.

**Will this wear out my SSD?**
That's the whole point of the entropy analysis. The program only compresses files that are clearly worth it, skipping low-yield and already-compressed data. You can also check before committing with dry-run mode.

**Do I need to run as Administrator?**
Only if you want the 1-click mode to also compress Windows system binaries via CompactOS. Everything else works as a normal user.

**What about my spinning hard drive?**
HDD mode runs the whole pipeline single-worker so the disk head moves forward instead of jumping between folders, and it nudges you to defragment afterwards.

**Does it work with non-NTFS drives?**
No. FAT32, exFAT and ReFS are out - NTFS only.

**Can I schedule it?**
Yes - it's a CLI. Pair the deterministic exit codes with a scheduled task or a script, point it at your directories, and let it run.

---

## Fun Facts

- The project is written in **Python**, with a **Rust** extension, shoddily glued together by **PyInstaller**, presenting a **webview** GUI. It's the tech-stack equivalent of duct-taping a rocket to a bicycle - and somehow it still gets where it's going.
- It's called **Trash-Compactor**, not because it compacts trash, but because the trash in your `Downloads` folder finally gets its day. (remember to delete it)
- The GUI does its best to be "modern and user-friendly", which in practice means it has like two buttons, a progress bar and a folder picker. Baby steps.
- The `-y` flag stands for "yes, I know what I'm doing", and the `--no-prompt` alias stands for "also, I'm in a script".

<details>
<summary>📜 Read the fine print (disclaimer)</summary>

**Disclaimer:** This project comes with no warranty, express or implied, except the warranty that your disk will be *more compact*. I'm not responsible for: (1) blue screens of death, (2) lost save files, (3) the existential horror of watching your `AppData` shrink, or (4) the smug feeling you'll get when your free space goes up. The developer reserves the right to be wrong about "who cares" being a valid build date. Probably. Probably not.

</details>

---

## To-Do

### Long-term Goals
- Research advanced compression methods:
  - Evaluate native NTFS/WOF compression APIs as an alternative to spawning `compact.exe`
  - Consider filesystem-agnostic approaches (moving compressed files in/out of the source drive unpacks them)
  - Research possibilities for custom compression algorithms
  - Investigate integration with other Windows compression features
- Quality of Life features:
  - Add resume capability for interrupted operations
- Security and Reliability:
  - Improve handling and messaging for network/NAS paths
  - Add verification of filesystem compatibility
- Backburner:
  - DirectStorage detection research

---

<div align="center">

**🗑️ Trash-Compactor** - compress once, forget for~~ever~~ awhile.

</div>
