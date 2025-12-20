# Trash-Compactor
  A utility for intelligent file compression on Windows 10/11 systems using the built-in NTFS compression algorithms and Windows' built-in "compact.exe" utility. Compressing files at the filesystem level is quite different from your average .zip or .7z compression (which is strictly for archival purposes) and it's seamless - you won't see a difference, but the apps will shrink in size without deleting anything. 
  
  Unlike [CompactGUI](https://github.com/IridiumIO/CompactGUI) (another tool that is based on compact.exe and primarily designed for compressing Steam games), this utility automatically selects the optimal compression algorithm based on file size - this lets you squeeze the most out of the compression algorithms and get even smaller file sizes, all while avoiding unnecessary compression and preventing excessive SSD wear, keeping things DRY. 
  
Compressing large directories to gain extra storage space will be so free and without downsides that it'll be the closest thing to having "free real estate".

  ## Features

  - Automated compression using Windows NTFS compression
  - Smart algorithm selection based on file size
  - Entropy analysis to evaluate compression potential and choose to compress only the files that will make sense to compress
  - Configurable minimum savings threshold (`--min-savings`) with interactive controls
  - Multiple operation modes for different use cases
  - Skips poorly-compressed file formats (zip, media files, etc.)
  - Skips already-compressed files
  - Skips using LZX compression if the computer is identified as too slow to handle it without performance losses (taking care of users)
  - Detailed compression and file throughput stats

  ## Limitations

  - It's only for storage devices with an NTFS file system, like your system drive and external flash drives and SSDs if they're formatted to use NTFS. If it's FAT32, exFAT or ReFS - it won't work for you.
  - It's best to assume that it likely won't work on network drives even if they are formatted to NTFS (haven't tested it).
  - Spinning hard drives might get fragmented, negatively impacting read performance.

  ## Requirements

  - Windows 10/11
  - Administrator privileges (but I'm looking for ways to drop this requirement)
  - **Optional: Temporarily disabled antivirus** - it will dramatically speed up the performance (Don't worry about the so-called "viruses" - the source code is right here and you can compile the program yourself)

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

Note: For Option 2, ensure Git and Python 3.9 or higher are installed on your system.

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
2. Choose the mode - either the 1-click run mode to get most things done fast or the manual mode.
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

#### 1-Click / Unattended Mode (Preferred)
Press `1` upon starting to run this mode. Most users can just compress their directories once and forget about it. Designed to be extremely simple to use for a casual user, a system administrator, refurbisher, or the so-called family tech support.
This mode will automatically compress the following directories:
- `Program Files`
- `Program Files (x86)` (including your Steam folder)
- `AppData`
- `Downloads`
- `Windows` (using Windows' built-in CompactOS feature to compress system binaries safely)

Expect at least 15GB to be saved on stock Windows installations.

#### Normal Mode
For first-time compression of directories with optimal performance.
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

- `-v`, or `--verbose`: Show exclusion decisions with entropy sampling (supports 4 levels of verbosity, up to `-vvvv` for debug logs)
- `-m`, or `--min-savings <percent>`: Set the minimum estimated savings (0-90, default 15%). Directories predicted to save less space are skipped automatically

## Development

To contribute to this project:

1. Create a new branch for your feature.
2. Submit a pull request.

### Like this project? 
Put a "star" if you find this project helpful or cool. I don't know what they do, apart for giving some small bragging rights, but maybe it might get introduced to more people, which will be great, as the collective storage savings will contribute to less e-waste - especially if an SSD is soldered onto the laptop's motherboard and is not replaceable, turning the laptop into a paperweight if it fails prematurely - which it surely will.

## To-Do

### Long-term Goals
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
