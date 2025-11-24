# Trash-Compactor
  A utility for intelligent file compression on Windows 10/11 systems using the built-in NTFS compression algorithms and Windows' built-in "compact.exe" utility. Unlike [CompactGUI](https://github.com/IridiumIO/CompactGUI) (another tool that is based on compact.exe and primarily designed for compressing Steam games), this utility automatically selects the optimal compression algorithm based on file size - this lets you squeeze the most out of the compression algorithms and get even smaller file sizes, all while avoiding unnecessary compression and preventing excessive SSD wear, keeping things DRY.

  ## Features

  - Automated compression using Windows NTFS compression
  - Smart algorithm selection based on file size
  - Entropy analysis to evaluate compression potential and choose to compress only the files that will make sense to compress
  - Configurable minimum savings threshold (`--min-savings`) with interactive controls
  - Multiple operation modes for different use cases
  - Skips poorly-compressed file formats (zip, media files, etc.)
  - Skips already-compressed files
  - Detailed compression statistics and per-run throughput metrics

  ## Requirements

  - Windows 10/11
  - Administrator privileges

## Installation

### Option 1: Using the Executable (Recommended)

1. [Download the latest release](https://github.com/misha1350/trash-compactor/releases/latest)
2. Run the executable file

### Option 2: Running from Source

1. Open PowerShell as Administrator
2. Clone and navigate to the repository:
    ```powershell
    git clone https://github.com/misha1350/trash-compactor.git
    cd trash-compactor
    ```
3. Run the program:
    ```powershell
    python main.py
    ```

Note: For Option 2, ensure Git and Python 3.8+ are installed on your system.

Optional: you can compile the app yourself as I did, using PyInstaller:
    ```powershell
    python -m PyInstaller trash-compactor.spec
    ```
    or, since we're interested in squeezing programs into small packages, you can install a build of [UPX](https://github.com/upx/upx) to build an app with compressed binaries:
    ```powershell
    python -m PyInstaller trash-compactor.spec --upx-dir 'c:\path\to\upx-win64'
    ```

## Usage

1. Run the program as Administrator.
2. Enter the directory path you want to compress.
3. The program will automatically:
    - Scan all files recursively
    - Skip poorly compressible files
    - Apply optimal compression algorithms
    - Display compression statistics

### Interactive configuration

Launching without arguments opens an interactive shell that lets you browse to the target directory, toggle flags, and adjust the minimum savings threshold before starting.

- Enter a path directly, optionally followed by flags (for example: `D:\Games -vx`).
- Use `--min-savings=<percent>` to change the skip threshold on the fly, or rely on the default 15% savings.
- Press `s` or hit enter on an empty line to begin once the directory and flags look good.

### Operation Modes

Trash-Compactor offers three distinct operation modes to handle different scenarios:

#### Normal Mode (Default)
For first-time compression of directories with optimal performance.
Most users can just compress once and forget about it.
Be aware that temporarily disabling the anti-virus or whitelisting this program is going to greatly improve the compression speed.
```powershell
.\trash-compactor.exe C:\path\to\compress
```

#### Dry-run Mode (-d)
To check how well a directory will compress without writing anything to the drive. SSDs have a finite amount of data that can be written, so some users might check if it's worth bothering to compress their directory.
```powershell
.\trash-compactor.exe -d C:\path\to\compress
```

#### Disabling (-x) or Forcing (-f) LZX Compression
LZX compression is turned **on** for large files by default.
LZX compression is resource-intensive and files will take some time to compress, though it does result in better compression of both compressible binaries and the files that XPRESS16K doesn't compress as well. But if you have a computer that was build or made before AD 2021, or if battery life is absolutely critical for you (a big problem on Intel Coffee Lake laptops), you may want to disable it

#### Running with a single worker (-s) for HDDs
HDDs read data sequentially and they can't handle the extreme I/O that the program will hammer the drive with, so to avoid excessive fragmentation, one should use this flag to reduce fragmentation somewhat. 

### Additional Options

- `-v, --verbose`: Show exclusion decisions with entropy sampling (supports 4 levels of verbosity, up to `-vvvv` for debug logs)
- `--min-savings <percent>`: Set the minimum estimated savings (0-90, default 15). Directories predicted to save less are skipped automatically

## Development

To contribute to this project:

1. Create a new branch for your feature.
2. Submit a pull request.

## To-Do

### Short-term Goals
- Let users start compression after a dry run without having to relaunch the program
- Add basic test suite for core functionality
  - Implement a single-thread benchmark to check if the CPU is fast enough to use LZX (to check if the CPU is not an Intel Atom with numerous, but weak cores)
  - Test compression detection accuracy
  - Verify that API calls work correctly
  - Check error handling paths

### Long-term Goals
- Create a 1-click/unattended mode of operation:
  - Automatically discover large folders (replacing WizTree and having to manually scour through folders)
  - Avoid compressing specific folders, such as ones mentioned in short-term goals
  - Make life easier for The Greatest Technicians That Have Ever Lived
- Research advanced compression methods:
  - Evaluate alternative NTFS compression APIs, like [UPX](https://github.com/upx/upx)
  - Consider filesystem-agnostic approaches (moving compressed files in/out of the source drive unpacks them)
  - Benchmark different compression strategies
  - Research possibilities for custom compression algorithms
  - Investigate integration with other Windows compression features
- Quality of Life features:
  - Saving user configuration with an optional `.ini` file
  - Add resume capability for interrupted operations
- Localization support depending on system language
- Security and Reliability:
  - Implement proper error handling for network paths
  - Add verification of filesystem compatibility
