"""OpenGL texture upload -- the only module in nandgui that touches PyOpenGL.

The whole viewer draws its two big pictures (the 4000 x 12 drive map and the
12 x pages block stripe) as a single textured quad each, because drawing 48000
imgui widgets per frame is not a thing you do.  Everything that makes that
honest lives here:

  * GL_NEAREST, unconditionally and with no way to ask for anything else.
    Linear filtering blends two adjacent NAND cells into a third colour that
    corresponds to no page state at all -- exactly the "merged state" mistake
    this project has already made twice (190 MB of zeros reported as content;
    a metadata-driven skip that nearly lost the boot block).  A halfway colour
    between SKIPPED and ERASED is a lie that looks authoritative.  GL_LINEAR is
    not imported by this module, `upload(..., nearest=False)` raises, and the
    mip range is pinned to level 0 so a generated mip -- which is an average of
    four cells, i.e. the same lie one octave down -- can never be sampled
    either.
  * GL_CLAMP_TO_EDGE on both axes, so panning past block 3999 or past CE 11
    repeats the edge cell instead of wrapping CE 11 around next to CE 0 (dead)
    and inventing an adjacency that does not exist in the die.
  * Strict dtype/shape checking.  A float32 array reinterpreted as bytes would
    paint plausible-looking garbage; better to raise where the palette bug is.
  * params_of()/check_all() read the sampler state back OUT of GL, so "we draw
    with hard edges" is a measurement and not a comment.  Every upload also
    re-asserts the parameters, so a backend or a future GL caller that stomped
    them is corrected on the next re-colourise.

What this module cannot make crisp: anything drawn on top of the quad by
imgui's draw list.  imgui's own line/rect primitives are anti-aliased (the
hatch, the CE separators and the grid in mapview/blockview all fade their
edges over ~2 px with alpha), so a soft or gradient-looking map is an overlay
question, not a sampling one.  Nothing here can fix that and nothing here
should try -- see mapview._draw_stipple / blockview._draw_canvas.

The quad itself is drawn untinted: imgui.image(tex, size, uv0, uv1) with no
tint argument, which in imgui-bundle 1.92 means vertex colour 0xFFFFFFFF.  The
texel that reaches the screen is therefore the exact palette byte that was
uploaded -- no alpha, no modulation, no third colour.

Only ever called on the GL/UI thread -- a GL context is thread-affine, and the
worker thread never sees this module.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from OpenGL.GL import (
    GL_CLAMP_TO_EDGE,
    GL_NEAREST,
    GL_RGBA,
    GL_RGBA8,
    GL_TEXTURE_2D,
    GL_TEXTURE_BASE_LEVEL,
    GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MAX_LEVEL,
    GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_WRAP_S,
    GL_TEXTURE_WRAP_T,
    GL_UNPACK_ALIGNMENT,
    GL_UNPACK_ROW_LENGTH,
    GL_UNSIGNED_BYTE,
    glBindTexture,
    glDeleteTextures,
    glGenTextures,
    glGetTexParameteriv,
    glPixelStorei,
    glTexImage2D,
    glTexParameteri,
    glTexSubImage2D,
)
# NB GL_LINEAR is not imported.  There is no code path in this
# module that can select it, and `grep GL_LINEAR nandgui/` returning nothing is
# the cheapest proof of that anyone reviewing the palette contract can run.

__all__ = [
    "Texture",
    "upload",
    "destroy",
    "destroy_all",
    "texture_count",
    "refresh_params",
    "params_of",
    "check_all",
    "EXPECTED_PARAMS",
]


@dataclasses.dataclass
class Texture:
    """A live GL texture object.  `id` is 0 once destroyed, which is the one
    value imgui will never try to sample."""

    id: int
    width: int
    height: int

    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def imgui_id(self) -> int:
        # imgui-bundle's imgui.image() takes the raw GLuint as an ImTextureID
        # (an int on this backend).  A destroyed texture reports 0 so a stale
        # view draws nothing rather than sampling a recycled id that now holds
        # some other view's pixels -- a wrong picture is worse than no picture.
        return int(self.id)


# id -> Texture, so destroy_all() can zero the ids in the dataclasses the views
# are still holding.  A view that kept a reference across shutdown then sees
# id == 0 instead of a dangling handle.
_LIVE: dict[int, Texture] = {}


def _check(rgba: np.ndarray) -> np.ndarray:
    if not isinstance(rgba, np.ndarray):
        raise TypeError(f"gltex.upload: expected numpy array, got {type(rgba)!r}")
    if rgba.dtype != np.uint8:
        # Refuse rather than reinterpret: a float or int32 colour array squeezed
        # into GL_UNSIGNED_BYTE produces colours that belong to no page state.
        raise ValueError(
            f"gltex.upload: rgba must be uint8, got {rgba.dtype} "
            "(palette.colorize/page_colors return uint8)"
        )
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(
            f"gltex.upload: rgba must be (H, W, 4), got shape {rgba.shape}"
        )
    if rgba.shape[0] == 0 or rgba.shape[1] == 0:
        raise ValueError(f"gltex.upload: empty texture, shape {rgba.shape}")
    # No-copy when it is already contiguous, which is the normal case; palette
    # promises C-contiguous but a caller's slice/transpose would silently shear
    # the image otherwise (row N drawn at row N+1: every CE off by one).
    return np.ascontiguousarray(rgba)


# The complete sampler state of every texture this module hands out.  Both a
# specification (what _set_params writes) and an oracle (what params_of must
# read back).  int() because PyOpenGL's constants are numpy-ish enums and the
# readback is a plain Python int.
EXPECTED_PARAMS: dict[int, int] = {
    int(GL_TEXTURE_MIN_FILTER): int(GL_NEAREST),
    int(GL_TEXTURE_MAG_FILTER): int(GL_NEAREST),
    int(GL_TEXTURE_WRAP_S): int(GL_CLAMP_TO_EDGE),
    int(GL_TEXTURE_WRAP_T): int(GL_CLAMP_TO_EDGE),
    int(GL_TEXTURE_BASE_LEVEL): 0,
    int(GL_TEXTURE_MAX_LEVEL): 0,
}

_PARAM_NAMES: dict[int, str] = {
    int(GL_TEXTURE_MIN_FILTER): "GL_TEXTURE_MIN_FILTER",
    int(GL_TEXTURE_MAG_FILTER): "GL_TEXTURE_MAG_FILTER",
    int(GL_TEXTURE_WRAP_S): "GL_TEXTURE_WRAP_S",
    int(GL_TEXTURE_WRAP_T): "GL_TEXTURE_WRAP_T",
    int(GL_TEXTURE_BASE_LEVEL): "GL_TEXTURE_BASE_LEVEL",
    int(GL_TEXTURE_MAX_LEVEL): "GL_TEXTURE_MAX_LEVEL",
}


def _set_params() -> None:
    """Write the one and only sampler state.  Caller must have bound `tex`.

    There is no `nearest` argument any more, and no branch: minification and
    magnification are both GL_NEAREST, always.  A cell that is 0.29 px wide
    (the default fit zoom) still resolves to exactly one palette colour, so
    every boundary in the picture is a step, never a ramp.

    BASE_LEVEL == MAX_LEVEL == 0 pins the mip range.  MIN_FILTER=GL_NEAREST
    never samples a mip today, but a mip level is an average of four cells --
    the merged-state lie at half resolution -- so making it unreachable costs
    two calls and removes the whole class.
    """
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    # Clamp, never repeat: block 3999 must not sit next to block 0, and CE 11
    # must not wrap onto dead CE 0.
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_BASE_LEVEL, 0)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0)


def upload(tex: Texture | None, rgba: np.ndarray, nearest: bool = True) -> Texture:
    """Upload `rgba` (uint8, (H, W, 4)) into `tex`, allocating if needed.

    Reuses the same GL texture id whenever the shape is unchanged, so the
    steady-state cost of a re-colourise is one glTexSubImage2D (~0.25 ms for
    4000x12 on this box) and imgui's ImTextureID stays stable across frames.
    Returns the Texture to store back on the view state.

    `nearest` is kept only so the existing call sites (which all pass True)
    still read as a statement of intent.  Passing False raises: filtering a
    state map is not a preference this codebase offers, and silently ignoring
    the argument would be worse than either honouring or refusing it.
    """
    if not nearest:
        raise ValueError(
            "gltex.upload: nearest=False is not supported. Linear filtering "
            "averages two adjacent NAND cells into a colour that belongs to no "
            "page state; see the module docstring. If a view wants a smoother "
            "picture it must reduce in numpy first (mapview._reduce_columns, "
            "blockview._reduce_states), which picks a real cell instead of "
            "inventing one."
        )
    rgba = _check(rgba)
    height, width = rgba.shape[0], rgba.shape[1]

    fresh = tex is None or tex.id == 0 or tex.width != width or tex.height != height
    if fresh:
        # Drop the old allocation first; a mode switch that changed the shape
        # must not leak a texture per switch.
        destroy(tex)
        tid = int(glGenTextures(1))
        tex = Texture(id=tid, width=width, height=height)
        _LIVE[tid] = tex

    assert tex is not None
    glBindTexture(GL_TEXTURE_2D, tex.id)
    # Rows are width*4 bytes so alignment is already 4, but the block stripe is
    # re-uploaded at whatever page count the block has; force 1 so an odd width
    # can never skew each row by a byte and shift the palette one channel over
    # (which would recolour every state into a different state).
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
    # Re-asserted on every upload, not just on allocation: the imgui backend
    # binds our texture id once per draw command, and any GL code that ever
    # left a filter set on the active unit would otherwise persist for the life
    # of the texture. Six glTexParameteri calls per re-colourise is free.
    _set_params()
    if fresh:
        glTexImage2D(
            GL_TEXTURE_2D, 0, GL_RGBA8, width, height, 0,
            GL_RGBA, GL_UNSIGNED_BYTE, rgba,
        )
    else:
        glTexSubImage2D(
            GL_TEXTURE_2D, 0, 0, 0, width, height,
            GL_RGBA, GL_UNSIGNED_BYTE, rgba,
        )
    glBindTexture(GL_TEXTURE_2D, 0)
    return tex


def refresh_params(tex: Texture | None) -> None:
    """Re-assert the sampler state on a live texture without re-uploading.

    upload() already does this, but a view only uploads when its picture
    changed; on an idle frame nothing rewrites the parameters.  A caller that
    suspects some other GL code touched the active texture unit can call this
    instead of forcing a full re-colourise.  No-op on a dead/absent texture.
    """
    if tex is None or tex.id == 0:
        return
    glBindTexture(GL_TEXTURE_2D, int(tex.id))
    _set_params()
    glBindTexture(GL_TEXTURE_2D, 0)


def params_of(tex: Texture | None) -> dict[int, int]:
    """Read the live sampler state back OUT of GL -> {pname: value}.

    The point is that "the map is drawn with hard edges" becomes a measurement.
    Returns {} for a dead/absent texture (nothing to measure); otherwise the
    six pnames in EXPECTED_PARAMS.  Errors are not swallowed: a GL failure here
    means the answer is unknown, and an unknown reported as a pass is how a
    filtering regression would reach the user unnoticed.
    """
    if tex is None or tex.id == 0:
        return {}
    glBindTexture(GL_TEXTURE_2D, int(tex.id))
    try:
        out: dict[int, int] = {}
        for pname in EXPECTED_PARAMS:
            value = glGetTexParameteriv(GL_TEXTURE_2D, pname)
            # PyOpenGL hands back a 1-element numpy array for these.
            try:
                out[pname] = int(value[0])
            except (TypeError, IndexError):
                out[pname] = int(value)
        return out
    finally:
        glBindTexture(GL_TEXTURE_2D, 0)


def check_all() -> list[str]:
    """Verify every live texture samples with hard edges -> list of complaints.

    Empty list == every map/stripe texture is GL_NEAREST on both filters,
    clamped on both axes, and pinned to mip level 0.  Intended for
    app._self_test and for the status bar's diagnostics; it costs one bind and
    six glGetTexParameteriv per texture (there are 2-3), so it is cheap enough
    to run per frame if anyone ever wants to.

    Must run on the UI thread with the GL context current, like everything else
    in this module.
    """
    problems: list[str] = []
    for tex in list(_LIVE.values()):
        try:
            got = params_of(tex)
        except Exception as exc:
            problems.append(
                f"texture {tex.id} ({tex.width}x{tex.height}): could not read "
                f"sampler state back: {exc}")
            continue
        if not got:
            continue
        for pname, want in EXPECTED_PARAMS.items():
            have = got.get(pname)
            if have != want:
                name = _PARAM_NAMES.get(pname, hex(pname))
                problems.append(
                    f"texture {tex.id} ({tex.width}x{tex.height}): {name} is "
                    f"{have}, expected {want}"
                    + (" (GL_NEAREST)" if want == int(GL_NEAREST) else "")
                    + (" (GL_CLAMP_TO_EDGE)"
                       if want == int(GL_CLAMP_TO_EDGE) else ""))
    return problems


def destroy(tex: Texture | None) -> None:
    """Delete one texture and mark it dead (id = 0).  Idempotent."""
    if tex is None or tex.id == 0:
        return
    tid = int(tex.id)
    tex.id = 0
    _LIVE.pop(tid, None)
    try:
        glDeleteTextures([tid])
    except Exception:
        # Shutdown races (context already gone) must not take the app down on
        # the way out; the driver reclaims the object with the context anyway.
        pass


def destroy_all() -> None:
    """Delete every texture handed out by this module.

    app.shutdown() calls this on the UI thread while the GL context is still
    current, before worker.shutdown() -- GL objects can only be freed by the
    thread that owns the context.
    """
    for tex in list(_LIVE.values()):
        destroy(tex)
    _LIVE.clear()


def texture_count() -> int:
    """Live textures, for the status bar.  Should sit at 2-3 (map, stripe,
    maybe histogram); a number that climbs every frame is a leak."""
    return len(_LIVE)
