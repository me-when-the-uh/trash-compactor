import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from colorama import Fore, Style

from . import entropy_dry_run, execute_compression_plan_wrapper
from .i18n import _
from .skip_logic import log_directory_skips
from .stats import (
    CompressionStats,
    log_estimated_savings,
    print_compression_summary,
    print_dry_run_summary,
    print_entropy_dry_run,
)
from .timer import PerformanceMonitor, TimingStats
from .config import COMPRESSION_ALGORITHMS


@dataclass(frozen=True)
class OneClickTargets:
    directories: tuple[Path, ...]


def _clear_screen() -> None:
    if getattr(sys.stdout, "isatty", lambda: False)():
        os.system("cls" if os.name == "nt" else "clear")


def resolve_targets() -> OneClickTargets:
    candidates: list[Path] = []

    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        raw = os.environ.get(env_key)
        if raw:
            candidates.append(Path(raw))

    user_profile = os.environ.get("USERPROFILE")
    home = Path(user_profile) if user_profile else Path.home()
    candidates.append(home / "AppData")
    candidates.append(home / "Downloads")
    candidates.append(home / "Documents")

    prog_data = os.environ.get("ProgramData")
    if prog_data:
        candidates.append(Path(prog_data))

    # Deduplicate while preserving order, and only keep paths that exist
    seen: set[str] = set()
    selected: list[Path] = []
    for candidate in candidates:
        normalized = os.path.normcase(os.path.normpath(str(candidate)))
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.exists():
            selected.append(candidate)

    return OneClickTargets(tuple(selected))


def _spawn_compactos_window() -> None:
    if os.name != "nt":
        return

    import tempfile
    comp_log = Path(tempfile.gettempdir()) / "compactos_result.txt"
    os.environ["COMPACTOS_LOG"] = str(comp_log)

    # Keep a separate window open so the user can see CompactOS output
    ps = (
        f"Start-Process -FilePath 'powershell.exe' "
        f"-ArgumentList @('-Command','compact.exe /compactos:always | Tee-Object -FilePath \"{comp_log}\"; Write-Host \"\"; Write-Host -ForegroundColor Green \"Compression finished. This window will close in 5 minutes...\"; Start-Sleep -Seconds 300') "
        f"-WindowStyle Normal"
    )

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ]
        )
    except OSError:
        # Fallback to cmd if PowerShell isn't available
        try:
            cmd = f'compact.exe /compactos:always > "{comp_log}" & type "{comp_log}" & echo. & echo Compression finished. This window will close in 5 minutes... & timeout /t 300'
            subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/c", cmd])
        except OSError:
            return


def _attention_beep() -> None:
    if os.name != "nt":
        sys.stdout.write("\a")
        sys.stdout.flush()
        return

    try:
        import winsound

        # Hoping this doesn't break
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        sys.stdout.write("\a")
        sys.stdout.flush()


def countdown_to_compress(seconds: int = 300) -> bool:
    """Return True to proceed, False to cancel."""
    seconds = max(0, int(seconds))

    if os.name != "nt":
        print(_("\nAuto-starting compression in {seconds}s.").format(seconds=seconds))
        answer = input(_("Proceed? [Y/n]: ")).strip().lower()
        return answer in {"", "y", "yes"}

    import msvcrt

    _attention_beep()
    print(
        Fore.YELLOW
        + _("\nAuto-starting compression in {seconds}s.").format(seconds=seconds)
        + Style.RESET_ALL
    )
    print(_("Press [Y] to start now, [N] to cancel."))

    deadline = time.monotonic() + seconds
    last_shown: Optional[int] = None

    while True:
        remaining = max(0, int(round(deadline - time.monotonic())))
        if remaining != last_shown:
            if remaining in {300, 120, 60, 30, 10, 5, 4, 3, 2, 1}:
                _attention_beep()
            sys.stdout.write("\r" + _( "Time remaining: {remaining:3d}s" ).format(remaining=remaining) + " " * 10)
            sys.stdout.flush()
            last_shown = remaining

        if remaining <= 0:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return True

        if msvcrt.kbhit():
            key = msvcrt.getwch()
            key = key.lower()

            if key in {"y", "\r", "\n"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return True
            if key in {"n", "\x1b"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return False

        time.sleep(0.1)


# def _check_battery() -> bool:
#     if os.name != "nt":
#         return True
#     import ctypes
#     class SYSTEM_POWER_STATUS(ctypes.Structure):
#         _fields_ = [
#             ("ACLineStatus", ctypes.c_byte),
#             ("BatteryFlag", ctypes.c_byte),
#             ("BatteryLifePercent", ctypes.c_byte),
#             ("SystemStatusFlag", ctypes.c_byte),
#             ("BatteryLifeTime", ctypes.c_ulong),
#             ("BatteryFullLifeTime", ctypes.c_ulong),
#         ]
#     status = SYSTEM_POWER_STATUS()
#     if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
#         # 1 usually means AC power, 0 is battery, 255 is unknown
#         return status.ACLineStatus != 0
#     return True


def run_one_click_mode(*, verbosity: int, min_savings: float) -> None:
    # if not _check_battery():
    #     print(Fore.YELLOW + _("Warning: Analyzing and compressing on battery power can rapidly drain it.") + Style.RESET_ALL)
    #     if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
    #         answer = input(_("Proceed anyway? [y/N]: ")).strip().lower()
    #         if answer not in {"y", "yes"}:
    #             return

    targets = resolve_targets()

    _clear_screen()
    print(Fore.CYAN + Style.BRIGHT + _("1-click mode (unattended)") + Style.RESET_ALL)
    if not targets.directories:
        print(Fore.YELLOW + _("No default targets were found on this system.") + Style.RESET_ALL)
        return

    print(_("The following directories will be analysed for compression:"))
    for directory in targets.directories:
        print(f"  - {directory}")

    print()
    print(Fore.YELLOW + _("Starting Windows CompactOS in a separate window...") + Style.RESET_ALL)
    _spawn_compactos_window()

    per_dir: list[tuple[Path, CompressionStats, list[tuple[Path, int, str]]]] = []

    total_original = 0
    total_compressed_lzx = 0
    total_compressed_xpress = 0
    total_timing = TimingStats()

    for directory in targets.directories:
        print(Fore.CYAN + _("\nDry-run: {directory}").format(directory=str(directory)) + Style.RESET_ALL)
        stats, monitor, plan = entropy_dry_run(
            str(directory),
            verbosity=verbosity,
            min_savings_percent=min_savings,
        )

        print_entropy_dry_run(stats, min_savings, verbosity)
        log_directory_skips(stats, verbosity, min_savings)
        # Intentionally do not print per-directory performance summaries in 1-click mode.

        total_original += int(stats.entropy_projected_original_bytes or 0)
        total_compressed_lzx += int(stats.entropy_projected_compressed_bytes or 0)
        total_compressed_xpress += int(stats.entropy_projected_compressed_bytes_conservative or 0)
        total_timing.total_time += float(getattr(monitor.stats, 'total_time', 0.0) or 0.0)
        total_timing.file_scan_time += float(getattr(monitor.stats, 'file_scan_time', 0.0) or 0.0)
        total_timing.entropy_analysis_time += float(getattr(monitor.stats, 'entropy_analysis_time', 0.0) or 0.0)
        total_timing.total_files += int(getattr(monitor.stats, 'total_files', 0) or 0)
        total_timing.files_analyzed_for_entropy += int(getattr(monitor.stats, 'files_analyzed_for_entropy', 0) or 0)

        per_dir.append((directory, stats, plan))

    print_dry_run_summary(
        min_savings_percent=min_savings,
        projected_original_bytes=total_original,
        projected_compressed_lzx_bytes=total_compressed_lzx,
        projected_compressed_xpress_bytes=total_compressed_xpress,
        title=_("Dry Run Summary (all targets)"),
    )
    total_timing.print_dry_run_metrics(min_percent=0.5)

    if not countdown_to_compress(300):
        print(Fore.CYAN + _( "\nCompression cancelled." ) + Style.RESET_ALL)
        return

    print(Fore.CYAN + _( "\nStarting compression..." ) + Style.RESET_ALL)

    total_comp_stats = CompressionStats()
    total_comp_stats.min_savings_percent = float(min_savings)
    total_comp_timing = TimingStats()
    any_compression = False

    for directory, stats, plan in per_dir:
        if not plan:
            print(Fore.YELLOW + _( "Skipping {directory}: nothing scheduled for compression." ).format(directory=str(directory)) + Style.RESET_ALL)
            continue

        monitor = PerformanceMonitor()
        monitor.start_operation()
        monitor.stats.total_files = len(plan)

        any_compression = True

        stats, monitor = execute_compression_plan_wrapper(
            stats,
            monitor,
            plan,
            verbosity_level=max(0, int(verbosity)),
            interactive_output=True,
            min_savings_percent=min_savings,
        )

        # `execute_compression_plan_wrapper` fills in compressed/skipped counts, but the total
        # files isn't known unless we set it.
        monitor.stats.total_files = int(monitor.stats.files_compressed + monitor.stats.files_skipped)

        monitor.print_summary()

        total_comp_stats.compressed_files += stats.compressed_files
        total_comp_stats.skipped_files += stats.skipped_files
        total_comp_stats.already_compressed_files += stats.already_compressed_files
        total_comp_stats.total_original_size += stats.total_original_size
        total_comp_stats.total_compressed_size += stats.total_compressed_size
        total_comp_stats.total_skipped_size += stats.total_skipped_size
        total_comp_stats.skip_extension_files += stats.skip_extension_files
        total_comp_stats.skip_low_savings_files += stats.skip_low_savings_files
        total_comp_stats.errors.extend(stats.errors)

        total_comp_timing.total_time += monitor.stats.total_time
        total_comp_timing.compression_time += monitor.stats.compression_time
        total_comp_timing.total_files += monitor.stats.total_files
        total_comp_timing.files_compressed += monitor.stats.files_compressed
        total_comp_timing.files_skipped += monitor.stats.files_skipped

    if any_compression:
        print_compression_summary(total_comp_stats)
        total_comp_timing.print_summary()

    comp_log = os.environ.get("COMPACTOS_LOG")
    if comp_log and Path(comp_log).exists():
        try:
            content = Path(comp_log).read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                if "bytes of data" in line.lower() or "ratio" in line.lower() or "compression" in line.lower():
                    print(Fore.GREEN + f"CompactOS: {line.strip()}" + Style.RESET_ALL)
            Path(comp_log).unlink(missing_ok=True)
        except Exception:
            pass

    print(Fore.CYAN + _( "\n1-click mode finished." ) + Style.RESET_ALL)
