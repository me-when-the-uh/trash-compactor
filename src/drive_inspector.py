import ctypes
import os
import logging
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

try:
    import wmi
except ImportError:
    wmi = None

KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
PROPERTY_STANDARD_QUERY = 0
STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

@dataclass(frozen=True)
class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ('PropertyId', ctypes.c_int),
        ('QueryType', ctypes.c_int),
        ('AdditionalParameters', ctypes.c_byte * 1),
    ]

@dataclass(frozen=True)
class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ('Version', wintypes.DWORD),
        ('Size', wintypes.DWORD),
        ('IncursSeekPenalty', wintypes.BOOLEAN),
    ]

@dataclass(frozen=True)
class VolumeDetails:
    anchor: Optional[str]
    drive_letter: Optional[str]
    drive_type: int
    filesystem: Optional[str]
    rotational: Optional[bool]

def _volume_anchor(path: str) -> Optional[str]:
    if not path:
        return None
    drive, _ = os.path.splitdrive(path)
    if not drive:
        return None
    drive = drive.rstrip('\\')
    if not drive:
        return None
    return f"{drive}\\"

def _filesystem_name(anchor: str) -> Optional[str]:
    volume_name = ctypes.create_unicode_buffer(256)
    fs_name = ctypes.create_unicode_buffer(256)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not KERNEL32.GetVolumeInformationW(
        anchor,
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_name,
        len(fs_name),
    ):
        error = ctypes.get_last_error()
        if error:
            logging.debug("GetVolumeInformationW failed for %s: %s", anchor, ctypes.WinError(error))
        return None
    name = fs_name.value.strip()
    return name.upper() if name else None

def _open_physical_drive(number: int) -> Optional[wintypes.HANDLE]:
    device_path = f"\\\\.\\PhysicalDrive{number}"
    handle = KERNEL32.CreateFileW(
        device_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error:
            logging.debug("CreateFileW failed for %s: %s", device_path, ctypes.WinError(error))
        return None
    return handle

def _drive_letter(drive_path: str) -> Optional[str]:
    if not drive_path:
        return None
    drive_letter = os.path.splitdrive(drive_path)[0]
    if drive_letter.endswith('\\'):
        drive_letter = drive_letter[:-1]
    return drive_letter or None

def get_volume_details(path: str) -> VolumeDetails:
    anchor = _volume_anchor(path)
    letter = _drive_letter(path)
    if not anchor:
        logging.debug("Unable to resolve volume anchor for %s", path)
        return VolumeDetails(None, letter, DRIVE_UNKNOWN, None, None)

    drive_type = KERNEL32.GetDriveTypeW(anchor)
    filesystem = None
    if drive_type not in {DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR}:
        filesystem = _filesystem_name(anchor)

    rotational = None
    if drive_type == DRIVE_FIXED and letter and len(letter) == 2 and letter[1] == ':':
        inspector = DriveInspector(letter)
        rotational = inspector.seek_penalty()
        if rotational is None:
            rotational = inspector.by_metadata()
            if rotational is None:
                rotational = inspector.by_latency()
                if rotational is None:
                    inspector.note_alignment()

    return VolumeDetails(anchor, letter, drive_type, filesystem, rotational)

def is_hard_drive(drive_path: str) -> bool:
    try:
        details = get_volume_details(drive_path)
    except Exception as exc:
        logging.error("Error detecting drive type: %s", exc)
        return False

    if details.drive_type != DRIVE_FIXED:
        logging.debug(
            "Volume %s reports drive type %s; treating as non-HDD",
            details.drive_letter or drive_path,
            details.drive_type,
        )
        return False

    if details.rotational is True:
        return True

    if details.rotational is False:
        return False

    logging.debug(
        "Unable to definitively identify drive %s as HDD, assuming SSD/flash",
        details.drive_letter or drive_path,
    )
    return False

class DriveInspector:
    def __init__(self, drive_letter: str):
        if wmi is None:
            raise ImportError("wmi module required for drive inspection")
        self.drive_letter = drive_letter
        self.conn = wmi.WMI()
        self._disk_number: Optional[int] = None

    def seek_penalty(self) -> Optional[bool]:
        disk_number = self._physical_disk_number()
        if disk_number is None:
            return None

        handle = _open_physical_drive(disk_number)
        if handle is None:
            return None

        try:
            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = STORAGE_DEVICE_SEEK_PENALTY_PROPERTY
            query.QueryType = PROPERTY_STANDARD_QUERY

            descriptor = DEVICE_SEEK_PENALTY_DESCRIPTOR()
            returned = wintypes.DWORD()
            success = KERNEL32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(descriptor),
                ctypes.sizeof(descriptor),
                ctypes.byref(returned),
                None,
            )
            if not success:
                error = ctypes.get_last_error()
                if error:
                    logging.debug(
                        "DeviceIoControl(query seek penalty) failed for %s: %s",
                        self.drive_letter,
                        ctypes.WinError(error),
                    )
                return None
            return bool(descriptor.IncursSeekPenalty)
        finally:
            KERNEL32.CloseHandle(handle)

    def by_metadata(self) -> Optional[bool]:
        disk_number = self._physical_disk_number()
        if disk_number is None:
            return None

        device_id = f"\\\\.\\PHYSICALDRIVE{disk_number}"
        disks = self.conn.Win32_DiskDrive(DeviceID=device_id)
        for disk in disks:
            logging.debug(
                "Inspecting disk: %s",
                {
                    'DeviceID': getattr(disk, 'DeviceID', 'N/A'),
                    'InterfaceType': getattr(disk, 'InterfaceType', 'N/A'),
                    'Description': getattr(disk, 'Description', 'N/A'),
                    'MediaType': getattr(disk, 'MediaType', 'N/A'),
                    'Model': getattr(disk, 'Model', 'N/A'),
                },
            )
            verdict = self._metadata_verdict(disk)
            if verdict is not None:
                return verdict
        return None

    def _metadata_verdict(self, disk) -> Optional[bool]:
        interface = getattr(disk, 'InterfaceType', '') or ''
        if 'nvme' in interface.lower():
            logging.debug("Drive %s is NVMe, treating as SSD", self.drive_letter)
            return False

        description = (getattr(disk, 'Description', '') or '').lower()
        if any(term in description for term in ['ssd', 'solid state', 'flash']):
            logging.debug("Drive %s describes itself as SSD/flash", self.drive_letter)
            return False
        if any(term in description for term in ['hard drive', 'hard disk']):
            logging.debug("Drive %s describes itself as HDD", self.drive_letter)
            return True

        media_type = (getattr(disk, 'MediaType', '') or '').lower()
        if any(term in media_type for term in ['ssd', 'solid', 'flash']):
            return False
        if any(term in media_type for term in ['hard', 'hdd', 'rotating']):
            return True

        model = (getattr(disk, 'Model', '') or '').lower()
        if any(term in model for term in ['ssd', 'nvme', 'solid state', 'm.2']):
            return False
        return None

    def by_latency(self) -> Optional[bool]:
        disk_number = self._physical_disk_number()
        if disk_number is None:
            return None

        for physical_disk in self.conn.Win32_PerfFormattedData_PerfDisk_PhysicalDisk():
            if physical_disk.Name == "_Total":
                continue
            try:
                disk_info = physical_disk.Name.split()
                if disk_info and int(disk_info[0]) == disk_number:
                    read_latency = getattr(physical_disk, 'AvgDiskSecPerRead', None)
                    write_latency = getattr(physical_disk, 'AvgDiskSecPerWrite', None)
                    logging.debug(
                        "Performance data for disk %s: read=%s, write=%s",
                        disk_number,
                        read_latency,
                        write_latency,
                    )
                    if read_latency and read_latency > 0.003:
                        logging.debug("Drive %s has HDD-like read latency: %ss", self.drive_letter, read_latency)
                        return True
                    if write_latency and write_latency > 0.003:
                        logging.debug("Drive %s has HDD-like write latency: %ss", self.drive_letter, write_latency)
                        return True
            except (ValueError, IndexError):
                logging.debug("Error processing physical disk performance data")
                continue
        return None

    def _physical_disk_number(self) -> Optional[int]:
        if self._disk_number is not None:
            return self._disk_number

        for relation in self.conn.Win32_LogicalDiskToPartition():
            try:
                if relation.Dependent.DeviceID == self.drive_letter:
                    antecedent = relation.Antecedent
                    disk_id = antecedent.split('PHYSICALDRIVE')[1]
                    number = int(''.join(filter(str.isdigit, disk_id)))
                    logging.debug("Found physical disk number %s for drive %s", number, self.drive_letter)
                    self._disk_number = number
                    return number
            except (AttributeError, IndexError, ValueError):
                logging.debug(
                    "Failed to extract physical disk number from antecedent: %s",
                    getattr(relation, 'Antecedent', 'N/A'),
                )
        return None

    def note_alignment(self) -> None:
        disk_number = self._physical_disk_number()
        if disk_number is None:
            return

        device_id = f"\\\\.\\PHYSICALDRIVE{disk_number}"
        disks = self.conn.Win32_DiskDrive(DeviceID=device_id)
        for disk in disks:
            size = getattr(disk, 'Size', None)
            block = getattr(disk, 'DefaultBlockSize', None)
            logging.debug("Disk %s Size: %s, DefaultBlockSize: %s", getattr(disk, 'DeviceID', 'N/A'), size, block)
            try:
                if size and block and size % block == 0:
                    logging.debug("Drive %s has aligned sectors, common in HDDs", self.drive_letter)
            except (TypeError, ZeroDivisionError):
                logging.debug("Error calculating sector alignment for drive %s", self.drive_letter)