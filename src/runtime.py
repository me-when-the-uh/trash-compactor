import logging
import os
from typing import Optional

from colorama import Fore, Style

from . import config
from .console import EscapeExit, announce_cancelled, read_user_input
from .file_utils import (
    DRIVE_FIXED,
    DRIVE_REMOTE,
    get_protection_reason,
    get_volume_details,
    is_protected_path,
)
from .compression_module import set_worker_cap


def sanitize_path(path: str) -> str:
    return os.path.normpath(path.strip(" '\""))


def is_admin() -> bool:
    try:
        return os.getuid() == 0
    except AttributeError:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())


def is_windows_system_path(directory: str) -> bool:
    return is_protected_path(directory)


def describe_protected_path(directory: str) -> Optional[str]:
    return get_protection_reason(directory)


def configure_lzx(choice_enabled: bool, force_lzx: bool, cpu_capable: bool, physical: int, logical: int) -> bool:
    if not choice_enabled:
        if force_lzx:
            logging.info("Ignoring -f because -x disables LZX explicitly")
        print(Fore.YELLOW + "-x: LZX compression disabled via command line flag.")
        config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
        return False

    if cpu_capable or force_lzx:
        config.COMPRESSION_ALGORITHMS['large'] = 'LZX'
        if force_lzx and not cpu_capable:
            logging.info(
                "Forcing LZX compression despite CPU having only %s cores and %s threads",
                physical,
                logical,
            )
        else:
            logging.info(
                "Using LZX compression (CPU deemed capable - it has %s cores and %s threads)",
                physical,
                logical,
            )
        return True

    config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
    print(Fore.YELLOW + f"\nNotice: Your CPU has {physical} cores and {logical} threads.")
    print(f"LZX compression requires at least {config.MIN_PHYSICAL_CORES_FOR_LZX} cores and {config.MIN_LOGICAL_CORES_FOR_LZX} threads.")
    print("LZX compression has been disabled for better system responsiveness.")
    print("Use -f flag to force LZX if you're feeling adventurous.")
    return False


def resolve_directory(argument: Optional[str]) -> str:
    pending = argument
    while True:
        try:
            source = pending if pending is not None else read_user_input("Enter directory path to compress: ")
        except (KeyboardInterrupt, EscapeExit):
            announce_cancelled()
            raise SystemExit(0)
        path = sanitize_path(source)
        if path:
            return path
        print(Fore.RED + "Directory path cannot be empty." + Style.RESET_ALL)
        pending = None


def confirm_hdd_usage(directory: str, force_serial: bool) -> bool:
    details = get_volume_details(directory)
    throttle_requested = force_serial  # Carry over manual single-worker overrides
    target_label = details.drive_letter or directory

    if details.anchor is None:
        logging.error("Unable to resolve volume for %s", directory)
        print(Fore.RED + "Unable to resolve the target volume. Please verify the path." + Style.RESET_ALL)
        return False

    if details.drive_type == DRIVE_REMOTE:
        logging.error("Network shares are not supported for compression targets: %s", directory)
        print(Fore.RED + "Network shares are not supported targets for compression." + Style.RESET_ALL)
        print("Please select a local NTFS volume instead.")
        return False

    if details.filesystem and details.filesystem != 'NTFS':
        logging.error(
            "Compression requires NTFS, but %s reports %s",
            details.drive_letter or directory,
            details.filesystem,
        )
        print(Fore.RED + "Windows compression requires NTFS." + Style.RESET_ALL)
        print(f"Detected filesystem: {details.filesystem or 'unknown'}")
        return False

    if details.drive_type != DRIVE_FIXED:
        logging.info(
            "Volume %s is not a fixed disk (type=%s); skipping HDD warning.",
            target_label,
            details.drive_type,
        )
        if throttle_requested:
            set_worker_cap(1)
            logging.info("Single-worker mode honored even though the drive is not fixed media.")
        return True

    if details.rotational is not True:
        if details.rotational is None:
            logging.debug(
                "Drive %s did not report seek penalty; treating as non-HDD."
                " Flash controllers such as eMMC and SD readers may often omit this flag.",
                target_label,
            )
        if throttle_requested:
            set_worker_cap(1)
            logging.info("Single-worker mode requested explicitly for %s.", target_label)
        return True

    print(Fore.YELLOW + "Detected a traditional spinning hard drive for this path." + Style.RESET_ALL)
    print("Sustained compression can thrash the disk heads, fragment files, and slow app/game launches." + Style.RESET_ALL)
    print(Fore.YELLOW + "\nRecommendation:" + Style.RESET_ALL)
    print("• Run the task during idle hours and use the single-worker mode (-s)")
    print("• Defragment the drive once compression finishes")
    print("• Prefer compressing rarely modified folders on HDDs")


    print("\n" + Fore.YELLOW + "Do you want to proceed anyway? (y/n): " + Style.RESET_ALL, end="")
    try:
        response = read_user_input("").strip().lower()
    except (KeyboardInterrupt, EscapeExit):
        announce_cancelled()
        return False
    if response not in {"y", "yes"}:
        print(Fore.CYAN + "Operation cancelled." + Style.RESET_ALL)
        return False

    if not throttle_requested:
        print(Fore.YELLOW + "\nThrottle compression to a single worker to avoid disk fragmentation? (Y/n): " + Style.RESET_ALL, end="")
        try:
            throttle_response = read_user_input("").strip().lower()
        except (KeyboardInterrupt, EscapeExit):
            announce_cancelled()
            return False
        throttle_requested = throttle_response in {"", "y", "yes"}

    if throttle_requested:
        set_worker_cap(1)
        logging.info("Single-worker mode engaged for %s due to HDD safeguards.", target_label)
        if not force_serial:
            print(Fore.YELLOW + "Running sequentially to keep fragmentation in check." + Style.RESET_ALL)

    print(Fore.YELLOW + "\nProceeding with compression on HDD. This may impact system performance." + Style.RESET_ALL)
    return True
