import ctypes
import os
import sys
import socket
import urllib.request
import urllib.parse
import json
from typing import List, Tuple, Optional

# =====================================================================
# C-COMPATIBLE STRUCTURES
# =====================================================================

class NniPacket(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("sparsity_mask", ctypes.c_ubyte * 16),
        ("scale", ctypes.c_float),
        ("anchor_id", ctypes.c_uint32),
        ("signature", ctypes.c_uint32),
        ("active_values", ctypes.c_float * 9)
    ]

class SharedSlateStruct(ctypes.Structure):
    _fields_ = [
        ("total_slots", ctypes.c_uint32),
        ("flags", ctypes.c_uint32 * 100),
        ("packets", NniPacket * 100)
    ]

class NPUScratchpad(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("registers", ctypes.c_float * 128)
    ]

# =====================================================================
# CORE SDK ENGINE
# =====================================================================

class NniSDK:
    def __init__(self):
        self.dll = self._load_dll()
        self._configure_signatures()

    def _load_dll(self) -> ctypes.CDLL:
        lib_ext = "dll" if sys.platform == "win32" else "dylib" if sys.platform == "darwin" else "so"
        lib_prefix = "" if sys.platform == "win32" else "lib"
        for mode in ["release", "debug"]:
            path = os.path.join(os.path.dirname(__file__), "target", mode, f"{lib_prefix}nni_core.{lib_ext}")
            if os.path.exists(path):
                return ctypes.CDLL(path)
        raise FileNotFoundError("Could not find nni_core binary. Please compile using 'cargo build --release' first.")

    def _configure_signatures(self):
        self.dll.nni_init_slate.restype = ctypes.POINTER(SharedSlateStruct)
        self.dll.nni_init_slate.argtypes = [ctypes.c_char_p]
        
        self.dll.nni_write_slot.argtypes = [ctypes.POINTER(SharedSlateStruct), ctypes.c_uint32, NniPacket]
        self.dll.nni_wait_for_slot.restype = ctypes.c_uint32
        self.dll.nni_wait_for_slot.argtypes = [ctypes.POINTER(SharedSlateStruct), ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(NniPacket)]
        self.dll.nni_read_slot_on_demand.restype = ctypes.c_bool
        self.dll.nni_read_slot_on_demand.argtypes = [ctypes.POINTER(SharedSlateStruct), ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(NniPacket), ctypes.POINTER(ctypes.c_uint32)]
        
        self.dll.nni_compress_sparse.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_size_t, ctypes.c_float, ctypes.c_uint32, ctypes.POINTER(NniPacket)]
        
        self.dll.nni_enclave_encrypt.restype = ctypes.c_bool
        self.dll.nni_enclave_encrypt.argtypes = [ctypes.POINTER(NniPacket), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte)]
        self.dll.nni_enclave_decrypt.restype = ctypes.c_bool
        self.dll.nni_enclave_decrypt.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(NniPacket)]

        self.dll.nni_benchmark_native.restype = ctypes.c_double
        self.dll.nni_benchmark_native.argtypes = [
            ctypes.POINTER(SharedSlateStruct),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t
        ]

_sdk = NniSDK()

# =====================================================================
# DEVELOPER INTERFACE CLASSES (WITH BUILT-IN LICENSE GATE)
# =====================================================================

class SharedSlate:
    """Manages the creation, connection, and reading/writing of the memory-mapped Slate."""
    def __init__(self, device_id_hex: str, name: str = "nni_shared_slate"):
        # LICENSING CHECK: Verify that the local node has been registered and authorized
        if not self._verify_local_license(device_id_hex):
            raise PermissionError("Access Denied: Unregistered Device ID. Register your Node at http://13.62.157.43/register")
            
        self.name_bytes = name.encode("utf-8")
        self.pointer = _sdk.dll.nni_init_slate(self.name_bytes)
        if not self.pointer:
            raise MemoryError("Could not map or connect to the NNI Shared Slate memory region.")

    def _verify_local_license(self, device_id_hex: str) -> bool:
        """One-Time Licensing Handshake: Checks local cache, or hits your AWS licensing gateway."""
        activation_file = ".nni_activation"
        if os.path.exists(activation_file):
            with open(activation_file, "r") as f:
                cached_key = f.read().strip()
                if cached_key == device_id_hex:
                    return True # Already activated locally -> Proceed offline with 0 latency
                    
        # No local activation found -> Make one-time HTTP call to your AWS Server
        print(f"[Licensing] Verifying Developer Device ID {device_id_hex[:8]}... with global gateway...")
        try:
            url = "http://13.62.157.43/api/licensing/verify"
            data = urllib.parse.urlencode({"device_id": device_id_hex}).encode('utf-8')
            req = urllib.request.Request(url, data=data, method="POST")
            
            with urllib.request.urlopen(req, timeout=4.0) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                
            if res_data.get("status") == "AUTHORIZED":
                # Save activation locally so we never have to run this network call again
                with open(activation_file, "w") as f:
                    f.write(device_id_hex)
                print("[Licensing] Device registration verified successfully. Offline mode unlocked.")
                return True
        except Exception as e:
            print(f"[Licensing Error] Could not connect to licensing gateway: {e}")
            
        return False

    def write_packet(self, slot_id: int, packet: NniPacket):
        _sdk.dll.nni_write_slot(self.pointer, slot_id, packet)

    def read_packet_on_demand(self, slot_id: int, last_version: int) -> Tuple[bool, Optional[NniPacket], int]:
        packet = NniPacket()
        current_version = ctypes.c_uint32(0)
        updated = _sdk.dll.nni_read_slot_on_demand(self.pointer, slot_id, last_version, ctypes.byref(packet), ctypes.byref(current_version))
        if updated:
            return True, packet, current_version.value
        return False, None, last_version

    def wait_for_packet(self, slot_id: int, last_version: int) -> Tuple[NniPacket, int]:
        packet = NniPacket()
        new_version = _sdk.dll.nni_wait_for_slot(self.pointer, slot_id, last_version, ctypes.byref(packet))
        return packet, new_version


class SecureEnclave:
    @staticmethod
    def encrypt(packet: NniPacket) -> Tuple[bytes, bytes, bytes]:
        import os
        nonce = os.urandom(12)
        nonce_buf = (ctypes.c_ubyte * 12).from_buffer_copy(nonce)
        ciphertext_buf = (ctypes.c_ubyte * 64)()
        tag_buf = (ctypes.c_ubyte * 16)()
        
        success = _sdk.dll.nni_enclave_encrypt(
            ctypes.byref(packet),
            nonce_buf,
            ciphertext_buf,
            tag_buf
        )
        if not success:
            raise RuntimeError("Enclave encryption engine failure.")
        return nonce, bytes(ciphertext_buf), bytes(tag_buf)

    @staticmethod
    def decrypt(nonce: bytes, ciphertext: bytes, tag: bytes) -> NniPacket:
        nonce_buf = (ctypes.c_ubyte * 12).from_buffer_copy(nonce)
        ciphertext_buf = (ctypes.c_ubyte * 64).from_buffer_copy(ciphertext)
        tag_buf = (ctypes.c_ubyte * 16).from_buffer_copy(tag)
        decrypted_packet = NniPacket()
        
        verified = _sdk.dll.nni_enclave_decrypt(
            nonce_buf,
            ciphertext_buf,
            tag_buf,
            ctypes.byref(decrypted_packet)
        )
        if not verified:
            raise PermissionError("Security Breach: Cryptographic Integrity Verification Failed!")
        return decrypted_packet


class Compressor:
    @staticmethod
    def compress_sparse(floats: List[float], threshold: float = 0.5, anchor_id: int = 100) -> NniPacket:
        float_array_type = ctypes.c_float * len(floats)
        c_floats = float_array_type(*floats)
        packet = NniPacket()
        _sdk.dll.nni_compress_sparse(c_floats, len(floats), threshold, anchor_id, ctypes.byref(packet))
        return packet

    @staticmethod
    def decompress_sparse(packet: NniPacket) -> List[float]:
        reconstructed_vector = [0.0] * 128
        active_idx = 0
        for i in range(128):
            mask_byte = packet.sparsity_mask[i // 8]
            bit_active = (mask_byte >> (i % 8)) & 1
            if bit_active:
                if active_idx < 9:
                    reconstructed_vector[i] = packet.active_values[active_idx]
                    active_idx += 1
        return reconstructed_vector

    @staticmethod
    def decompress_to_npu(packet: NniPacket, scratchpad: NPUScratchpad) -> None:
        active_idx = 0
        for i in range(128):
            mask_byte = packet.sparsity_mask[i // 8]
            bit_active = (mask_byte >> (i % 8)) & 1
            if bit_active:
                if active_idx < 9:
                    scratchpad.registers[i] = packet.active_values[active_idx]
                    active_idx += 1
            else:
                scratchpad.registers[i] = 0.0
