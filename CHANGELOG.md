# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.5.0-beta] - 2025-11-25
### Added
- A simple benchmark to test whether the computer is too slow to use LZX compression

### Changed
- Refactored launch logic to remove WET code and have a centralised menu

### Fixed
- Bugs that were occuring with the old approach to the CLI interface because of two different menus


## [v0.4.5] - 2025-11-24
### Added
- Simple directory browser for choosing which directory to compress with Windows Explorer for better compatibility with Windows' varying flavours of CLI terminals


## [v0.4.4] - 2025-11-24
### Added
- French localization
- German localization
- Spanish localization
- Portuguese localization
- Russian localization


## [v0.4.3] - 2025-11-23
### Changed
- Replaced spinner with a timer for progress report
- Now reports entropy scanning progress
- Primitive cache directory checks removed

### Fixed
- Total directory size is now counted correctly after removing the cache directory check
- Further scanning speed improvements - it's like 30% faster idk thanks to removing cache directory checks (aka optimising code) and other tweaks
- Progress report fixes


## [v0.4.2] - 2025-11-22
### Changed
- Minimum savings threshold increased from 10% to 15%
- Shuffled verbose output levels around to make it more sensible
- More readable documentation provided

### Fixed
- Estimated savings are counted accurately now because previous changes broke total directory size calculation
- Initial scanning speed is improved by 68.8% thanks to alleviating bottlenecks and disabling faux multi-threading - overall scanning performance improved by some 8%
- Various bug fixes, necessary foundation for further development if God wills it


## [v0.4.1] - 2025-11-20
### Changed
- Entropy scanning is now being done in three 16KB bites - at the start, the middle, and the end of the file, instead of just the first 64KB of the file, improving the accuracy of entropy analysis
- Stats are now displayed during `--dry-run` operation as well

### Fixed
- Estimated savings are counted accurately now as a result of the change to the entropy scanning approach
- Scanning speed is now 61% faster just from optimising the entropy scanning by switching to a lightweight `zlib` implementation instead of counting up the entropy by hand
- The spinner now spins at a fixed rate and the progress report isn't being spammed during entropy analysis stage anymore


## [v0.4.0] - 2025-10-12
### Added
- `-m/--min-savings` CLI flag and interactive control to configure the minimum percentage a directory must save before compression runs
- Estimated savings are now recorded in skip statistics and surfaced in verbose mode so you can review why directories were skipped
- Additional verbose and debug messaging around entropy sampling helps validate cache avoidance decisions in real time
- Dry-run entropy analysis (`-d/--dry-run`) to preview projected savings across subdirectories without compressing files
- Estimated savings during dry-run analysis (works badly)

### Changed
- Verbosity level guidance and summaries report savings and entropy figures in percentages instead of raw bit counts for easier interpretation
- Interactive launcher preserves the configured savings threshold between adjustments and final execution
- Base directory entropy sampling now ignores files at the root level so subdirectories drive the savings estimate
- Entropy sampling that runs for every directory, expanding the sample window to deliver consistent savings estimates


## [v0.4.0-beta2] - 2025-10-11

## [0.4.0-beta] - 2025-10-10

## [0.3.2] - 2025-10-08
### Added
- `-s/--single-worker` flag (and interactive toggle) to run compression sequentially when fragile storage needs a gentler touch
- HDD safeguard now offers an opt-in prompt to downgrade to single-worker mode and honours manual overrides automatically

### Changed
- Volume detection inspects only the target drive, blocks remote or non-NTFS paths up front, and logs flash controllers that omit seek-penalty hints
- Compression and branding worker pools respect the new runtime worker cap, with user-facing messaging when throttling is active

## [0.3.1] - 2025-10-06

### Added
- Now using batch compression for a reduction in separate compact.exe calls, resulting in a further 20-25% performance increase
- Activating flags straight from the UI

### Changed
- Reworked the progress info
- UI completely overhauled a bit

### Fixed
- Stats fixes

## [0.3.0] - 2025-10-05
### Added
- XPRESS compression multi-threading (!!!)
- Performance monitoring block that displays total time, per-stage timings, and throughput after each run

### Changed
- Reworked the compression pipeline to plan work up front, split XPRESS and LZX batches, and run them in dedicated thread pools for better throughput
- Refined the non-verbose spinner to greatly improve performance show processed/total counters
- Normalized configuration constants with typed collections so large files default to LZX when the CPU check allows it
- Refactored the code to remove some AI-generated slop code

### Fixed
- Branding mode now re-validates each file after `compact` runs so stubborn files are counted under "still unmarked" instead of reported as successful

## [0.2.7] - 2025-05-20
### Added
- Hard Drive check, to discourage using the program with a hard drive so as to keep the performance high (more testing with a hard drive needs to be done to evaluate performance loss)
- More extensions of poorly compressable files that are to be skipped

## [0.2.6] - 2025-03-15
### Added
- New operation modes to handle different use cases:
  - Normal mode (default): Fast compression with basic detection
  - Thorough mode (-t flag): More accurate but slower compression checks for daily/scheduled tasks
  - Branding mode (-b flag): For marking files as compressed after initial compression
- Performance improvements in file compression detection:
  - Avoiding slow "compact /a" checks in normal mode
  - Optimized file detection logic to speed up initial compression runs
- Better guidance on optimal usage patterns for different scenarios:
  - One-time compression of new directories
  - Regular scheduled compression tasks
  - Post-compression branding for Windows compatibility
- Improved command-line argument handling with mutually exclusive operation modes

### Changed
- Reduced unnecessary file checks during normal compression mode
- Made thorough checking an explicit opt-in feature for scheduled tasks
- Clarified help text and documentation for different operation modes
- Updated guidance for optimal configuration based on use case

### Fixed
- Performance bottleneck during compression checks by making thorough checks optional
- Potential reprocessing of already compressed files in daily operations

## [0.2.5] - 2025-03-11
### Added
- New `-b` flag for cleanup mode that only brands files as compressed using legacy method
- Skip main compression when using `-b` flag to avoid redundant work
- More thorough compression verification to prevent re-compression attempts
- Improved compression state detection using legacy Windows APIs
- Better handling of already compressed files to prevent unnecessary reprocessing
- Non-verbose mode now shows a dynamic spinner during compression
- Improved file path display in non-verbose mode:
    - Shows relative paths instead of absolute paths
    - Condenses deep paths with ellipses (e.g., "folder/.../subfolder/file.txt")
    - Maintains full path visibility for files near the root
- Better terminal output handling with proper line clearing

### Changed
- Separated compression and branding into distinct operations
- Improved status reporting for compression verification
- Updated help text and pro tips to include new cleanup mode functionality
- Non-verbose mode now provides better visual feedback during compression
- Improved path formatting for better readability

### Fixed
- Fixed issue where some compressed files were not properly marked in Windows
- Resolved the persistent bug causing files to be recompressed unnecessarily

## [0.2.4] - 2025-03-10
### Added
- Improved CPU detection for LZX compression with minimum threshold requirements
- New `-f` flag to force LZX compression on less capable CPUs
- "Press any key to exit" prompt after compression completes
- More specific error handling for better troubleshooting

### Fixed
- Incorrect reporting of space saved after compression
- More robust file checking with improved error handling
- Better detection of system limitations for compression

## [0.2.3] - 2024-12-04
### Added
- More file formats that are poorly compressable

## [0.2.2] - 2024-12-03
### Added
- Toggling LZX compression on/off with the `-x` flag - useful when your CPU is very slow or old (Intel Atom, Intel Celeron)
- Flag stacking
- A To-Do list of improvements for future releases (because I will not be making any new improvements any time soon)

## [0.2.1] - 2024-12-02
### Added
- CPU capability detection for optimal compression settings
- User prompt to optionally enable LZX on slower systems with dual-core CPUs
- Display of physical cores and logical threads count

### Changed
- XPRESS16K is now used by default on lower-end systems instead of LZX
- Improved system responsiveness on dual-core systems

## [0.2.0] - 2024-12-01
### Added
- A splash screen
- Path sanitizing to handle various input formats
- Unicode support for file paths with non-ASCII characters
- Significant speed improvements in file compression checks

### Changed
- Improved logging format for better readability
- Enhanced compression summary to include already compressed files

### Fixed
- Correctly identify and skip already compressed files
- Handle exceptions more gracefully during file size checks

### Known issues
Some files are erroneously reported as being compressed, when they have already been compressed before. Even though they are not being compressed again, it must be checked 

### To-Do
The app has to sense how many cores there are and run with lower priority, so as not to impact the performance of other apps when the compression is running.

## [0.1.0] - 2024-11-30
### Added
- Initial release with basic file compression functionality.
- Smart algorithm selection based on file size.
- Detailed compression statistics.