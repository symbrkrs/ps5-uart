"""The UNKNOWN sentinel.

This project's central discipline in one object: "not measured" is not the same
as "measured, and there are none".  UNKNOWN is falsey so `if not x` still
guards, but it REFUSES len(), iteration and arithmetic, so a gap in the survey
cannot be silently summed into a total and reported as a finding.

Every one of these raises is load-bearing.  If a future change makes UNKNOWN
merely falsey, the failures move from a TypeError at the call site to a wrong
number in a report, which is the failure mode the whole class exists to prevent.
"""
import pytest


@pytest.fixture(scope="module")
def U(ftl):
    return ftl.UNKNOWN


def test_is_falsey_but_not_none_and_not_empty(U):
    assert not U
    assert U is not None
    assert U != []
    assert U != 0
    assert U != False                                     # noqa: E712


def test_is_a_singleton(U, ftl):
    assert ftl.Unknown() is U
    assert ftl.Unknown() is ftl.Unknown()


def test_prints_loudly(U):
    assert repr(U) == "<unknown>"
    assert str(U) == "<unknown>"
    assert "unknown" in "%s" % U


def test_len_raises(U):
    with pytest.raises(TypeError) as e:
        len(U)
    assert "never measured" in str(e.value)


def test_iter_raises(U):
    with pytest.raises(TypeError) as e:
        iter(U)
    assert "never measured" in str(e.value)
    with pytest.raises(TypeError):
        list(U)
    with pytest.raises(TypeError):
        for _ in U:                                       # pragma: no cover
            break


@pytest.mark.parametrize("op", [
    lambda u: u + 1,
    lambda u: 1 + u,
    lambda u: u - 1,
    lambda u: 1 - u,
    lambda u: int(u),
    lambda u: sum([u, 1]),
])
def test_arithmetic_raises(U, op):
    with pytest.raises(TypeError):
        op(U)


def test_unpacking_a_result_that_might_be_unknown_fails_loudly(U):
    """The realistic misuse: `a, b = ftl.something()` when nothing was measured."""
    with pytest.raises(TypeError):
        _a, _b = U


def test_membership_and_indexing_are_not_silently_true(U):
    """`x in UNKNOWN` must not answer False as if the container were empty."""
    with pytest.raises(TypeError):
        _ = 5 in U
