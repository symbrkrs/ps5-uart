"""Build the small ARM stubs this package uploads to the controller.

Three modules -- `nd_fast`, `nd_efcscan` and `hwdev` -- each compile a handful of
C into a position-fixed thumb blob for the Cortex-R5 and read symbol addresses
back out.  Each used to carry its own copy of the command line, and each of those
copies began

    wsl bash -c "cd /home/<user>/<checkout> && arm-none-eabi-gcc ..."

which hard-coded one operator's home directory, and hard-coded the assumption
that the toolchain is reachable only through WSL -- so the same code could not
build on the Linux machine whose toolchain it was borrowing.

What this does instead, in order:

  1. run `arm-none-eabi-gcc` directly if it is on PATH.  This is the normal case
     on Linux and on a Windows box with the toolchain installed, and it is the
     only path that works when there is no WSL at all.
  2. otherwise, on Windows, retry the same command through `wsl bash -c`, with
     the working directory translated by `wslpath` rather than assumed.
  3. otherwise raise `ToolchainMissing`, naming what was looked for and how to
     override it.

$TITANIA_ARM_PREFIX renames the triple (default `arm-none-eabi-`), so a
`arm-linux-gnueabi-` or a vendor toolchain can be used without editing code.

The stubs themselves are not built at import: `nd_drain.c`, `nd_scan.c` and
`tdev_scan.c` are only compiled when something is about to upload them, and a
missing toolchain is therefore an error on the hardware path only.  Nothing
offline needs any of this.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["ToolchainMissing", "ARM_PREFIX", "CHECKOUT_ROOT", "have_toolchain",
           "compile_stub", "parse_nm"]


class ToolchainMissing(RuntimeError):
    """No ARM toolchain could be reached, and the message says what to install."""


#: Cross-toolchain prefix.  Overridable so this is not a statement about one box.
ARM_PREFIX = os.environ.get("TITANIA_ARM_PREFIX", "arm-none-eabi-")

#: The directory the package sits in.  Still used to put a checkout on
#: `sys.path` for the root-level UART modules; NOT where the stubs live.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent

#: Where the `.c` stubs live: INSIDE the package, resolved from `__file__`, so
#: the same path works in a checkout, under `pip install -e .` and in an
#: unpacked wheel.  They used to sit at the checkout root, which meant an
#: installed titania could not drive hardware at all -- `parent.parent` is
#: `site-packages` there, and no .c file has ever been in it.  Same argument
#: `nd_tables` makes for the measured tables, and the same fix.
STUB_DIR = Path(__file__).resolve().parent / "stubs"

_TIMEOUT = 180

#: Set on first use by `_build_dir` when the stub directory is read-only.
_TMP_BUILD = None


def _tool(name):
    return ARM_PREFIX + name


def have_toolchain():
    """Is the compiler runnable directly (no WSL hop)?  -> bool"""
    return shutil.which(_tool("gcc")) is not None


def _wsl_available():
    return sys.platform == "win32" and shutil.which("wsl") is not None


def _wsl_path(path):
    """A Windows path as WSL sees it.  -> str, or None if it cannot be told."""
    try:
        r = subprocess.run(["wsl", "wslpath", "-a", str(path)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _run(script, cwd, timeout):
    """Run a `sh` script in `cwd`, natively if possible, else through WSL.

    -> CompletedProcess.  Raises ToolchainMissing when neither route exists.
    """
    if have_toolchain():
        return subprocess.run(["sh", "-c", script] if sys.platform != "win32"
                              else ["bash", "-c", script],
                              cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
    if _wsl_available():
        there = _wsl_path(cwd)
        if there is not None:
            # cd into the translated directory -- not a written-down one -- so
            # this works from any checkout, on any drive, under any user.
            return subprocess.run(
                ["wsl", "bash", "-c", "cd %s && %s" % (_sh_quote(there), script)],
                capture_output=True, text=True, timeout=timeout)
    raise ToolchainMissing(
        "%s is not on PATH and no WSL toolchain could be reached.\n"
        "Install an ARM bare-metal toolchain (Debian/Ubuntu: "
        "`apt install gcc-arm-none-eabi`), or set $TITANIA_ARM_PREFIX if yours "
        "uses a different triple." % _tool("gcc"))


def _sh_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def parse_nm(text, want=None):
    """`nm` output -> {symbol: address}, text symbols only.

    `want` filters to a set of names.  Only `T`/`t` entries are returned: a
    stub's entry point has to be code, and picking up a same-named data symbol
    would upload a blob and jump into a table.
    """
    out = {}
    for ln in text.splitlines():
        f = ln.split()
        if len(f) == 3 and f[1] in ("T", "t"):
            if want is None or f[2] in want:
                try:
                    out[f[2]] = int(f[0], 16)
                except ValueError:
                    continue
    return out


def _build_dir(src_dir):
    """Somewhere the compiler may write.  -> Path

    The .elf and .bin land beside the source when that is writable, which is
    the checkout case and keeps a build inspectable after the fact. An
    installed package is the other case: site-packages is frequently read-only,
    and writing build products into it would be wrong even where it is not, so
    that falls back to one temp directory reused for the life of the process.
    """
    probe = src_dir / ".armbuild-write-test"
    try:
        probe.touch()
        probe.unlink()
        return src_dir
    except OSError:
        global _TMP_BUILD
        if _TMP_BUILD is None:
            import tempfile
            _TMP_BUILD = Path(tempfile.mkdtemp(prefix="titania-stubs-"))
        return _TMP_BUILD


def compile_stub(source, entry, text_addr, out_stem=None, cwd=None,
                 symbols=(), timeout=_TIMEOUT, extra_cflags=()):
    """Compile one stub.  -> (blob bytes, {symbol: address})

    `source`      the .c file name, relative to `cwd`
    `entry`       the elf entry symbol (`-e`)
    `text_addr`   where .text is linked, which is where the blob must be uploaded
    `out_stem`    base name for the .elf/.bin (default: the source's stem)
    `symbols`     names to resolve out of `nm`; the entry is always included

    The .elf and .bin are written beside the source, as they always were, so a
    build can be inspected after the fact.
    """
    src_dir = Path(cwd) if cwd is not None else STUB_DIR
    src = src_dir / source
    if not src.exists():
        raise ToolchainMissing("missing stub source %s" % src)
    stem = out_stem or src.stem
    # Compile in a writable directory, with the source copied in rather than
    # named by an absolute path: the command keeps using a BARE filename, which
    # is what lets the WSL hop in `_run` work by translating one directory.
    cwd = _build_dir(src_dir)
    if cwd != src_dir:
        import shutil as _shutil
        _shutil.copy2(src, cwd / src.name)
    script = (
        "{cc} -mthumb -mcpu=cortex-r5 -O2 -nostdlib -nostartfiles -e {entry} "
        "-Wl,--section-start=.text={addr:#x} {extra}-o {stem}.elf {src} && "
        "{objcopy} -O binary {stem}.elf {stem}.bin && "
        "{nm} {stem}.elf"
    ).format(cc=_tool("gcc"), objcopy=_tool("objcopy"), nm=_tool("nm"),
             entry=entry, addr=text_addr, stem=stem, src=src.name,
             extra=("".join(c + " " for c in extra_cflags)))

    r = _run(script, cwd, timeout)
    if r.returncode != 0:
        raise ToolchainMissing(
            "%s did not compile:\n%s" % (src.name, (r.stderr or "")[-1200:]))
    blob = (Path(cwd) / (stem + ".bin")).read_bytes()
    syms = parse_nm(r.stdout, want=set(symbols) | {entry} if symbols else None)
    return blob, syms
