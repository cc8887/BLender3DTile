"""Explicit termination of an isolated native-engine subprocess."""
import os
import sys


def finish_process(exit_code: int):
    """Preserve the real status while avoiding bpy's Windows teardown crash.

    The pinned Windows wheel can crash in DLL detach after rendering, even with
    os._exit. All jobs have already closed their files and written worker_result
    before reaching this point. TerminateProcess bypasses DLL detach; Windows
    reclaims this dedicated worker's native resources. Never call this from a
    host process or before its build/check results have been finalized.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt" and "bpy" in sys.modules:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        if not kernel32.TerminateProcess(kernel32.GetCurrentProcess(), int(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        raise RuntimeError("Self termination unexpectedly returned")
    raise SystemExit(exit_code)
