import os
import re
import subprocess
import sys
import threading
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


def _compactos_log_path() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / f"compactos_result_{os.getpid()}.txt"


def _encoded_ps_command(script: str) -> str:
    """Encode a PowerShell command as -EncodedCommand (base64 UTF-16LE).

    Avoids quoting/injection issues when the script embeds paths derived from
    environment variables (e.g. TMP).
    """
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _spawn_compactos_window() -> None:
    """Spawn a visible CompactOS window (CLI fallback)."""
    if os.name != "nt":
        return

    comp_log = _compactos_log_path()
    os.environ["COMPACTOS_LOG"] = str(comp_log)

    # Keep a separate window open so the user can see CompactOS output
    ps_command = (
        f"Write-Host -ForegroundColor Cyan 'Compressing OS binaries... This may take a while.'; "
        f"compact.exe /compactos:always | Tee-Object -FilePath '{comp_log}'; "
        f"Write-Host ''; Write-Host -ForegroundColor Green 'Compression finished. This window will close in 5 minutes...'; "
        f"Start-Sleep -Seconds 300"
    )

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                _encoded_ps_command(ps_command),
            ],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    except OSError:
        # Fallback to cmd if PowerShell isn't available
        try:
            msg = "Compressing OS binaries... This may take a while."
            cmd = f'echo {msg} & compact.exe /compactos:always > "{comp_log}" & type "{comp_log}" & echo. & echo Compression finished. This window will close in 5 minutes... & timeout /t 300'
            subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/c", cmd])
        except OSError:
            return


def _parse_int_token(token: str) -> int:
    return int(re.sub(r"[^\d]", "", token))


def _parse_compactos_summary(output: str) -> dict[str, object]:
    if not output:
        return {}
    info: dict[str, object] = {}
    m = re.search(r"([\d,]+)\s+files?\s+within\s+([\d,]+)\s+dir", output, re.I)
    if m:
        info["files"] = _parse_int_token(m.group(1))
        info["dirs"] = _parse_int_token(m.group(2))
    m = re.search(
        r"([\d,]+)\s+total bytes of data are stored in ([\d,]+)\s+bytes", output, re.I
    )
    if m:
        orig = _parse_int_token(m.group(1))
        comp = _parse_int_token(m.group(2))
        info["original_bytes"] = orig
        info["compressed_bytes"] = comp
        info["saved_bytes"] = max(0, orig - comp)
    m = re.search(r"compression ratio is ([\d.]+)\s+to\s+1", output, re.I)
    if m:
        info["ratio"] = float(m.group(1))

    if "saved_bytes" not in info:
        for line in output.splitlines():
            numbers = re.findall(r"[\d][\d.,\s]*\d", line)
            parsed = [_parse_int_token(token) for token in numbers if _parse_int_token(token) > 0]
            if len(parsed) >= 2 and parsed[0] > parsed[1] and parsed[0] >= 1_000_000:
                info["original_bytes"] = parsed[0]
                info["compressed_bytes"] = parsed[1]
                info["saved_bytes"] = max(0, parsed[0] - parsed[1])
                break

    if "files" not in info:
        for line in output.splitlines():
            numbers = re.findall(r"\b[\d.,]+\b", line)
            parsed = [_parse_int_token(token) for token in numbers if _parse_int_token(token) > 0]
            if len(parsed) >= 2 and all(value < 1_000_000 for value in parsed[:2]):
                info["files"] = parsed[0]
                info["dirs"] = parsed[1]
                break

    if "ratio" not in info:
        m = re.search(r"([\d.,]+)\s*(?:to|:)\s*1\b", output, re.I)
        if m:
            info["ratio"] = float(m.group(1).replace(",", "."))

    return info


def _human_bytes(n: int) -> str:
    if n >= (1 << 30):
        return f"{n / (1 << 30):.1f} GiB"
    if n >= (1 << 20):
        return f"{n / (1 << 20):.1f} MiB"
    if n >= (1 << 10):
        return f"{n / (1 << 10):.1f} KiB"
    return f"{n} B"


COMPACTOS_TIMEOUT_SECONDS = 2 * 60 * 60  # CompactOS can run for over an hour


def run_compactos_hidden(
    progress_callback=None,
    line_callback=None,
    timeout: int = COMPACTOS_TIMEOUT_SECONDS,
) -> tuple[bool, str, dict]:
    """Run compact.exe /compactos:always hidden (no visible window)."""
    if os.name != "nt":
        return False, "Not Windows"

    comp_log = _compactos_log_path()

    ps_command = "compact.exe /compactos:always 2>&1"

    try:
        proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_command,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        output_lines: list[str] = []
        timed_out = False

        def _read_output() -> None:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                output_lines.append(line)
                if line_callback:
                    line_callback(line)
                if progress_callback:
                    progress_callback(line, None)

        if progress_callback:
            progress_callback(_("Compressing Windows binaries..."), None)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=max(0, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)

        output = "\n".join(output_lines)
        if timed_out:
            output += _("\n[timed out after {seconds}s]").format(seconds=timeout)
        success = not timed_out and proc.returncode == 0
        parsed = _parse_compactos_summary(output)

        if progress_callback:
            if success:
                progress_callback(_("CompactOS compression complete."), 100.0)
            else:
                progress_callback(
                    _("CompactOS compression failed (exit code {code}).").format(code=proc.returncode),
                    100.0,
                )

        # Write log for CLI fallback
        try:
            comp_log.write_text(output, encoding="utf-8")
            os.environ["COMPACTOS_LOG"] = str(comp_log)
        except Exception:
            pass

        return success, output, parsed

    except OSError:
        return False, "Failed to start compact.exe", {}


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


def _prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{prompt} {suffix}: ").strip().lower()
        except EOFError:
            return default

        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False

        print(_("Please answer with Y or N."))


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


def run_one_click_mode(*, verbosity: int, min_savings: float, allow_compactos: bool = False) -> None:
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
    if allow_compactos:
        should_compress_windows = _prompt_yes_no(
            _("Compress Windows binaries now for extra memory savings?"),
            default=False,
        )
        if should_compress_windows:
            print(Fore.YELLOW + _("Starting Windows compression in a separate window...") + Style.RESET_ALL)
            _spawn_compactos_window()
        else:
            print(
                Fore.YELLOW
                + _("Skipping Windows binaries compression by user choice.")
                + Style.RESET_ALL
            )
    else:
        print(
            Fore.YELLOW
            + _("Skipping Windows compression: administrator privileges are required to run 'compact.exe /compactos:always'.")
            + Style.RESET_ALL
        )
        print(
            Fore.YELLOW
            + _("1-click mode will compress accessible user directories.")
            + Style.RESET_ALL
        )

    per_dir: list[tuple[Path, CompressionStats, list[tuple[str, int, str]]]] = []

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
        total_compressed_lzx += int(stats.entropy_projected_size or 0)
        total_compressed_xpress += int(stats.entropy_projected_size_conservative or 0)
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
            parsed = _parse_compactos_summary(content)
            if parsed.get("saved_bytes"):
                print(
                    Fore.GREEN
                    + f"CompactOS: {parsed.get('files', '?')} files, saved {_human_bytes(int(parsed['saved_bytes']))}"
                    + (f" (ratio {parsed['ratio']:.1f})" if parsed.get("ratio") else "")
                    + Style.RESET_ALL
                )
            else:
                for line in content.splitlines():
                    if "bytes of data" in line.lower() or "ratio" in line.lower() or "compression" in line.lower():
                        print(Fore.GREEN + f"CompactOS: {line.strip()}" + Style.RESET_ALL)
            Path(comp_log).unlink(missing_ok=True)
        except Exception:
            pass

    print(Fore.CYAN + _( "\n1-click mode finished." ) + Style.RESET_ALL)
