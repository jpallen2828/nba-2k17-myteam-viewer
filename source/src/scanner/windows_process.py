from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
from typing import Iterator
from functools import lru_cache

from .constants import PROCESS_NAME, SCAN_CHUNK_BYTES
from .models import MemoryRegion, ModuleInfo, ProcessInfo


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100

READABLE_BASE_PROTECTIONS = {
    PAGE_READONLY,
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _require_windows() -> None:
    if not hasattr(ctypes, "WinDLL"):
        raise OSError("Live process scanning is available only on Windows.")


@lru_cache(maxsize=1)
def _kernel32():
    _require_windows()
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    library.OpenProcess.restype = wintypes.HANDLE
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.ReadProcessMemory.restype = wintypes.BOOL
    library.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.VirtualQueryEx.restype = ctypes.c_size_t
    library.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    return library


def close_handle(handle: int | None) -> None:
    if handle and handle != INVALID_HANDLE_VALUE:
        _kernel32().CloseHandle(handle)


def find_processes(name: str = PROCESS_NAME) -> list[tuple[int, str]]:
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "Unable to enumerate Windows processes")
    matches: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == name.casefold():
                matches.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        close_handle(snapshot)
    return matches


def enumerate_modules(pid: int) -> list[ModuleInfo]:
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"Unable to enumerate modules for PID {pid}")
    modules: list[ModuleInfo] = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            modules.append(ModuleInfo(
                name=str(entry.szModule),
                path=str(Path(entry.szExePath)),
                base_address=int(base),
                size=int(entry.modBaseSize),
            ))
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        close_handle(snapshot)
    modules.sort(key=lambda item: item.base_address)
    return modules


def _architecture(handle: int) -> str:
    kernel32 = _kernel32()
    machine_names = {0: "native", 0x014C: "x86", 0x8664: "x64", 0xAA64: "arm64"}
    try:
        function = kernel32.IsWow64Process2
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        process_machine = wintypes.USHORT()
        native_machine = wintypes.USHORT()
        if function(handle, ctypes.byref(process_machine), ctypes.byref(native_machine)):
            value = process_machine.value or native_machine.value
            return machine_names.get(value, f"machine_0x{value:04X}")
    except AttributeError:
        pass
    wow64 = wintypes.BOOL()
    if kernel32.IsWow64Process(handle, ctypes.byref(wow64)):
        return "x86" if wow64.value else ("x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86")
    return "unknown"


def is_readable_region(state: int, protect: int) -> bool:
    base_protect = protect & 0xFF
    return state == MEM_COMMIT and not (protect & PAGE_GUARD) and base_protect in READABLE_BASE_PROTECTIONS


class ProcessReader:
    """Least-privilege, read-only access to a single Windows process."""

    def __init__(self, pid: int, name: str = PROCESS_NAME):
        self.pid = int(pid)
        self.name = name
        self._handle: int | None = None
        self._modules: list[ModuleInfo] | None = None
        self.metrics = None

    @classmethod
    def open_first(cls, name: str = PROCESS_NAME) -> "ProcessReader":
        matches = find_processes(name)
        if not matches:
            raise RuntimeError(f"{name} is not running")
        reader = cls(matches[0][0], matches[0][1])
        reader.open()
        return reader

    def open(self) -> None:
        if self._handle:
            return
        rights = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
        handle = _kernel32().OpenProcess(rights, False, self.pid)
        if not handle:
            code = ctypes.get_last_error()
            raise OSError(code, f"NBA2K17.exe was found, but read-only access failed for PID {self.pid}")
        self._handle = handle

    def close(self) -> None:
        close_handle(self._handle)
        self._handle = None

    def __enter__(self) -> "ProcessReader":
        self.open()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @property
    def handle(self) -> int:
        if not self._handle:
            raise RuntimeError("Process handle is closed")
        return self._handle

    @property
    def modules(self) -> list[ModuleInfo]:
        if self._modules is None:
            self._modules = enumerate_modules(self.pid)
        return self._modules

    def refresh_modules(self):
        self._modules = enumerate_modules(self.pid)
        return self.main_module

    @property
    def main_module(self) -> ModuleInfo:
        for module in self.modules:
            if module.name.casefold() == self.name.casefold():
                return module
        if self.modules:
            return self.modules[0]
        raise RuntimeError(f"No modules were visible for PID {self.pid}")

    def process_info(self) -> ProcessInfo:
        module = self.main_module
        return ProcessInfo(
            pid=self.pid,
            name=self.name,
            executable_path=module.path,
            architecture=_architecture(self.handle),
            modules=list(self.modules),
        )

    def read(self, address: int, size: int, *, allow_partial: bool = False) -> bytes | None:
        if self.metrics is not None:
            self.metrics.count('read_calls')
            self.metrics.count('read_bytes_requested', size)
        if size < 0:
            raise ValueError("Read size cannot be negative")
        if size == 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        received = ctypes.c_size_t()
        ok = _kernel32().ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(int(address)),
            buffer,
            size,
            ctypes.byref(received),
        )
        if not ok:
            if allow_partial and received.value:
                return buffer.raw[:received.value]
            return None
        if not allow_partial and received.value != size:
            return None
        return buffer.raw[:received.value]

    def read_or_raise(self, address: int, size: int) -> bytes:
        data = self.read(address, size)
        if data is None:
            raise OSError(ctypes.get_last_error(), f"Read failed at 0x{address:X} ({size} bytes)")
        return data

    def _unpack(self, address: int, format_code: str):
        size = struct.calcsize(format_code)
        data = self.read(address, size)
        return None if data is None else struct.unpack(format_code, data)[0]

    def u8(self, address: int): return self._unpack(address, "<B")
    def i8(self, address: int): return self._unpack(address, "<b")
    def u16(self, address: int): return self._unpack(address, "<H")
    def i16(self, address: int): return self._unpack(address, "<h")
    def u32(self, address: int): return self._unpack(address, "<I")
    def i32(self, address: int): return self._unpack(address, "<i")
    def u64(self, address: int): return self._unpack(address, "<Q")
    def i64(self, address: int): return self._unpack(address, "<q")
    def float32(self, address: int): return self._unpack(address, "<f")
    def float64(self, address: int): return self._unpack(address, "<d")
    def pointer(self, address: int): return self.u64(address)

    def string(self, address: int, max_bytes: int = 256, encoding: str = "utf-8") -> str | None:
        data = self.read(address, max_bytes, allow_partial=True)
        if data is None:
            return None
        terminator = b"\0\0" if encoding.casefold().replace("-", "") == "utf16le" else b"\0"
        if terminator == b"\0\0":
            end = next((offset for offset in range(0, len(data) - 1, 2)
                        if data[offset:offset + 2] == terminator), len(data))
        else:
            end = data.find(terminator)
            if end < 0:
                end = len(data)
        return data[:end].decode(encoding, errors="replace")

    def ascii(self, address: int, max_bytes: int = 256) -> str | None:
        return self.string(address, max_bytes, "ascii")

    def utf8(self, address: int, max_bytes: int = 256) -> str | None:
        return self.string(address, max_bytes, "utf-8")

    def utf16le(self, address: int, max_bytes: int = 512) -> str | None:
        return self.string(address, max_bytes, "utf-16-le")

    def _category(self, base: int, region_type: int) -> tuple[str, str | None]:
        for module in self.modules:
            if module.base_address <= base < module.end_address:
                return ("main_module" if module.name.casefold() == self.name.casefold()
                        else "loaded_dll"), module.name
        if region_type == MEM_PRIVATE:
            return "private_memory", None
        if region_type == MEM_MAPPED:
            return "mapped_data", None
        if region_type == MEM_IMAGE:
            return "image_memory", None
        return "unknown", None

    def iter_regions(self, *, readable_only: bool = True) -> Iterator[MemoryRegion]:
        address = 0
        max_address = 0x00007FFFFFFFFFFF if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x7FFFFFFF
        mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)
        kernel32 = _kernel32()
        while address < max_address:
            mbi = MEMORY_BASIC_INFORMATION()
            result = kernel32.VirtualQueryEx(self.handle, ctypes.c_void_p(address), ctypes.byref(mbi), mbi_size)
            if not result:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize or 0)
            if size <= 0:
                break
            category, module_name = self._category(base, int(mbi.Type))
            region = MemoryRegion(
                base_address=base,
                region_size=size,
                allocation_base=int(mbi.AllocationBase or 0),
                allocation_protect=int(mbi.AllocationProtect),
                state=int(mbi.State),
                protect=int(mbi.Protect),
                type=int(mbi.Type),
                category=category,
                module_name=module_name,
            )
            if not readable_only or is_readable_region(region.state, region.protect):
                yield region
            next_address = base + size
            if next_address <= address:
                break
            address = next_address

    def readable_regions(self) -> list[MemoryRegion]:
        return list(self.iter_regions(readable_only=True))

    def allocation_key(self, address: int) -> tuple[int, ...]:
        """Targeted allocation identity/protection probe, without enumeration."""
        mbi = MEMORY_BASIC_INFORMATION()
        if not _kernel32().VirtualQueryEx(self.handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            raise OSError(ctypes.get_last_error(), f'Cannot query allocation at 0x{address:X}')
        if not is_readable_region(int(mbi.State), int(mbi.Protect)):
            raise ValueError(f'Allocation is no longer readable at 0x{address:X}')
        if self.metrics:
            self.metrics.count('allocation_queries')
        return (int(mbi.AllocationBase or 0), int(mbi.BaseAddress or 0), int(mbi.RegionSize),
                int(mbi.State), int(mbi.Protect), int(mbi.Type))

    def query_region(self, address: int) -> MemoryRegion:
        """Re-query a failed range, including unmapped/non-readable results."""
        mbi = MEMORY_BASIC_INFORMATION()
        if not _kernel32().VirtualQueryEx(self.handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            raise OSError(ctypes.get_last_error(), f'Cannot re-query {address:#x}')
        return MemoryRegion(int(mbi.BaseAddress or 0), int(mbi.RegionSize),
                            int(mbi.AllocationBase or 0), int(mbi.AllocationProtect),
                            int(mbi.State), int(mbi.Protect), int(mbi.Type))

    def is_readable_pointer(self, address: int, size: int = 1) -> bool:
        if not address or address < 0:
            return False
        for region in self.iter_regions(readable_only=True):
            if region.base_address <= address and address + size <= region.end_address:
                return True
            if region.base_address > address:
                return False
        return False

    def iter_chunks(
        self,
        regions: list[MemoryRegion] | None = None,
        chunk_size: int = SCAN_CHUNK_BYTES,
        overlap: int = 0,
        cancel_event=None,
    ) -> Iterator[tuple[int, bytes, MemoryRegion]]:
        for region in regions or self.readable_regions():
            offset = 0
            while offset < region.region_size:
                if cancel_event is not None and cancel_event.is_set():
                    return
                read_size = min(chunk_size, region.region_size - offset)
                data = self.read(region.base_address + offset, read_size, allow_partial=True)
                if data:
                    yield region.base_address + offset, data, region
                advance = read_size - overlap if read_size > overlap else read_size
                if advance <= 0:
                    break
                offset += advance
