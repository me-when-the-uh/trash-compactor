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
    media_type: Optional[int] = None  # MSFT_PhysicalDisk: 3=HDD, 4=SSD, 5=SCM, 0=unspec
    spindle_speed: Optional[int] = None  # 0 for SSDs
    bus_type: Optional[str] = None
    detection_method: str = ''  # e.g. 'seek_penalty', 'msft_physical', 'powershell', 'metadata'

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

def _volume_details_base(path: str) -> VolumeDetails:
    anchor = _volume_anchor(path)
    letter = _drive_letter(path)
    if not anchor:
        logging.debug("Unable to resolve volume anchor for %s", path)
        return VolumeDetails(None, letter, DRIVE_UNKNOWN, None, None)

    drive_type = KERNEL32.GetDriveTypeW(anchor)
    filesystem = None
    if drive_type not in {DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR}:
        filesystem = _filesystem_name(anchor)

    return VolumeDetails(anchor, letter, drive_type, filesystem, None, None, None, None, '')


def get_volume_details_fast(path: str) -> VolumeDetails:
    """Resolve drive type and filesystem without WMI/IOCTL rotational probes."""
    return _volume_details_base(path)


def get_volume_details(path: str) -> VolumeDetails:
    details = _volume_details_base(path)
    if details.anchor is None:
        return details

    anchor = details.anchor
    letter = details.drive_letter
    drive_type = details.drive_type
    filesystem = details.filesystem
    rotational = None
    media_type = None
    spindle_speed = None
    bus_type = None
    detection_method = ''
    if drive_type == DRIVE_FIXED and letter and len(letter) == 2 and letter[1] == ':':
        try:
            inspector = DriveInspector(letter)
            rotational = inspector.seek_penalty()
            if rotational is not None:
                detection_method = 'seek_penalty'
            if rotational is None:
                rotational = inspector.by_metadata()
                if rotational is not None:
                    detection_method = 'metadata'
            if rotational is None:
                rotational = inspector.by_latency()
                if rotational is not None:
                    detection_method = 'latency'
            if rotational is None:
                inspector.note_alignment()

            # try modern MSFT_PhysicalDisk for authoritative media/spindle
            mt, ss, bt, meth = inspector._msft_physical_disk_info()
            if mt is not None:
                media_type = mt
                spindle_speed = ss
                bus_type = bt
                if meth:
                    detection_method = meth if not detection_method else detection_method + '+' + meth
            # fallback
            if media_type is None:
                mt2, ss2, bt2, meth2 = inspector._powershell_physical_disk_info()
                if mt2 is not None:
                    media_type = mt2
                    spindle_speed = ss2
                    bus_type = bt2
                    detection_method = meth2 if not detection_method else detection_method + '+' + meth2
        except Exception as exc:
            logging.debug(
                "Drive inspection skipped for %s (WMI unavailable or failed): %s",
                letter,
                exc,
            )

    return VolumeDetails(anchor, letter, drive_type, filesystem, rotational, media_type, spindle_speed, bus_type, detection_method)

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

    if details.media_type == 3:  # HDD
        logging.debug("MSFT/Physical media_type=3 (HDD) for %s", details.drive_letter or drive_path)
        return True
    if details.media_type in (4, 5):  # SSD or SCM
        return False
    if details.spindle_speed is not None and details.spindle_speed > 0:
        return True
    if details.spindle_speed == 0:
        return False

    if details.rotational is True:
        return True

    if details.rotational is False:
        return False

    logging.debug(
        "Unable to definitively identify drive %s as HDD, assuming SSD/flash (method=%s)",
        details.drive_letter or drive_path,
        details.detection_method or 'none',
    )
    return False

class DriveInspector:
    def __init__(self, drive_letter: str):
        self.drive_letter = drive_letter
        self.conn = None
        self._disk_number: Optional[int] = None
        if wmi is None:
            logging.debug("wmi module not installed; skipping WMI drive inspection for %s", drive_letter)
            return
        try:
            self.conn = wmi.WMI()
        except Exception as exc:
            logging.debug("WMI connection failed for %s: %s", drive_letter, exc)

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
        if self.conn is None:
            return None
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
        if self.conn is None:
            return None
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
        if self.conn is None:
            return None
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
        if self.conn is None:
            return
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

    def _msft_physical_disk_info(self) -> tuple[Optional[int], Optional[int], Optional[str], str]:
        """Query MSFT_PhysicalDisk via WMI (root\\Microsoft\\Windows\\Storage) for MediaType/SpindleSpeed.
        Returns (media_type, spindle_speed, bus_type, method) or (None,...)."""
        if self.conn is None:
            return None, None, None, ''
        disk_number = self._physical_disk_number()
        if disk_number is None:
            return None, None, None, ''
        try:
            try:
                storage_conn = wmi.WMI(namespace=r'root\Microsoft\Windows\Storage')
            except Exception:
                storage_conn = self.conn  # may not have class, will fail gracefully
            for pd in storage_conn.MSFT_PhysicalDisk():
                try:
                    # DeviceId or FriendlyName may contain number; match by number in DeviceId
                    dev = getattr(pd, 'DeviceId', '') or ''
                    if str(disk_number) in str(dev):
                        mt = getattr(pd, 'MediaType', None)
                        ss = getattr(pd, 'SpindleSpeed', None)
                        bt = getattr(pd, 'BusType', None)
                        try:
                            mt = int(mt) if mt is not None else None
                        except (TypeError, ValueError):
                            pass
                        try:
                            ss = int(ss) if ss is not None else None
                        except (TypeError, ValueError):
                            pass
                        logging.debug("MSFT_PhysicalDisk for %s: MediaType=%s Spindle=%s Bus=%s", self.drive_letter, mt, ss, bt)
                        if mt is not None or ss is not None:
                            return mt, ss, (str(bt) if bt else None), 'msft_physical'
                except Exception:
                    continue
        except Exception as exc:
            logging.debug("MSFT_PhysicalDisk query failed: %s", exc)
        return None, None, None, ''

    def _powershell_physical_disk_info(self) -> tuple[Optional[int], Optional[int], Optional[str], str]:
        """Fallback using built-in PowerShell Get-PhysicalDisk (no extra deps). Parses for matching disk number (or first if unknown)."""
        import subprocess
        disk_number = self._physical_disk_number()
        try:
            if disk_number is not None:
                ps_cmd = [
                    'powershell', '-NoProfile', '-Command',
                    f'Get-PhysicalDisk | Where-Object {{ $_.DeviceId -eq {disk_number} -or $_.FriendlyName -match "{disk_number}" }} | Select-Object FriendlyName,MediaType,BusType,SpindleSpeed,DeviceId | ConvertTo-Json -Compress'
                ]
            else:
                ps_cmd = ['powershell', '-NoProfile', '-Command', 'Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,SpindleSpeed,DeviceId | ConvertTo-Json -Compress']
            cf = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            res = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=5, creationflags=cf)
            out = (res.stdout or '').strip()
            if out and out != 'null':
                import json
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    dev = str(item.get('DeviceId', '') or item.get('FriendlyName', ''))
                    match = (disk_number is None) or (str(disk_number) in dev)
                    if match:
                        mt = item.get('MediaType')
                        ss = item.get('SpindleSpeed')
                        bt = item.get('BusType')
                        try:
                            mt = int(mt) if mt is not None else None
                        except Exception:
                            mt_str = str(mt or '').upper()
                            if 'HDD' in mt_str or 'HARD' in mt_str: mt=3
                            elif 'SSD' in mt_str or 'SOLID' in mt_str: mt=4
                            else: mt=None
                        try:
                            ss = int(ss) if ss is not None else None
                        except Exception:
                            pass
                        logging.debug("PS Get-PhysicalDisk match for %s: mt=%s ss=%s", self.drive_letter, mt, ss)
                        if mt is not None or ss is not None:
                            return mt, ss, (str(bt) if bt else None), 'powershell'
        except Exception as exc:
            logging.debug("PowerShell physical disk probe failed: %s", exc)
        return None, None, None, ''