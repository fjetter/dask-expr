from collections import OrderedDict
from itertools import product

import numpy as np
import pytest

from dask_expr import from_pandas
from dask_expr._expr import OpAlignPartitions
from dask_expr._repartition import RepartitionDivisions
from dask_expr._shuffle import Shuffle, divisions_lru
from dask_expr.tests._util import _backend_library, assert_eq

# Set DataFrame backend for this module
pd = _backend_library()


@pytest.fixture
def pdf():
    pdf = pd.DataFrame({"x": range(100)})
    pdf["y"] = pdf.x // 7  # Not unique; duplicates span different partitions
    yield pdf


@pytest.fixture
def df(pdf):
    yield from_pandas(pdf, npartitions=10)


@pytest.mark.parametrize("op", ["__add__", "add"])
def test_broadcasting_scalar(pdf, df, op):
    df2 = from_pandas(pdf, npartitions=2)
    result = getattr(df, op)(df2.x.sum())
    assert_eq(result, pdf + pdf.x.sum())
    assert len(list(result.expr.find_operations(OpAlignPartitions))) == 0

    divisions_lru.data = OrderedDict()
    result = getattr(df.set_index("x"), op)(df2.x.sum())
    # Make sure that we don't touch divisions
    assert len(divisions_lru.data) == 0
    assert_eq(result, pdf.set_index("x") + pdf.x.sum())
    assert len(list(result.expr.find_operations(OpAlignPartitions))) == 0

    if op == "__add__":
        # Can't avoid expensive alignment check, but don't touch divisions while figuring it out
        divisions_lru.data = OrderedDict()
        result = getattr(df.set_index("x"), op)(df2.set_index("x").sum())
        # Make sure that we don't touch divisions
        assert len(divisions_lru.data) == 0
        assert_eq(result, pdf.set_index("x") + pdf.set_index("x").sum())
        assert len(list(result.expr.find_operations(OpAlignPartitions))) > 0

        assert (
            len(
                list(
                    result.optimize(fuse=False).expr.find_operations(
                        RepartitionDivisions
                    )
                )
            )
            == 0
        )

    # Can't avoid alignment, but don't touch divisions while figuring it out
    divisions_lru.data = OrderedDict()
    result = getattr(df.set_index("x"), op)(df2.set_index("x"))
    # Make sure that we don't touch divisions
    assert len(divisions_lru.data) == 0
    assert_eq(result, pdf.set_index("x") + pdf.set_index("x"))
    assert len(list(result.expr.find_operations(OpAlignPartitions))) > 0

    assert (
        len(
            list(result.optimize(fuse=False).expr.find_operations(RepartitionDivisions))
        )
        > 0
    )


@pytest.mark.parametrize("sorted_index", [False, True])
@pytest.mark.parametrize("sorted_map_index", [False, True])
def test_series_map(sorted_index, sorted_map_index):
    base = pd.Series(
        ["".join(np.random.choice(["a", "b", "c"], size=3)) for x in range(100)]
    )
    if not sorted_index:
        index = np.arange(100)
        np.random.shuffle(index)
        base.index = index
    map_index = ["".join(x) for x in product("abc", repeat=3)]
    mapper = pd.Series(np.random.randint(50, size=len(map_index)), index=map_index)
    if not sorted_map_index:
        map_index = np.array(map_index)
        np.random.shuffle(map_index)
        mapper.index = map_index
    expected = base.map(mapper)
    dask_base = from_pandas(base, npartitions=1, sort=False)
    dask_map = from_pandas(mapper, npartitions=1, sort=False)
    result = dask_base.map(dask_map)
    assert_eq(expected, result)


def test_assign_align_partitions():
    pdf = pd.DataFrame({"x": [0] * 20, "y": range(20)})
    df = from_pandas(pdf, npartitions=2)
    s = pd.Series(range(10, 30))
    ds = from_pandas(s, npartitions=df.npartitions)
    result = df.assign(z=ds)[["y", "z"]]
    expected = pdf.assign(z=s)[["y", "z"]]
    assert_eq(result, expected)


def test_assign_unknown_partitions(pdf):
    pdf2 = pdf.sort_index(ascending=False)
    df2 = from_pandas(pdf2, npartitions=3, sort=False)
    df1 = from_pandas(pdf, npartitions=3).clear_divisions()
    df1["new"] = df2.x
    expected = pdf.copy()
    expected["new"] = pdf2.x
    assert_eq(df1, expected)
    assert len(list(df1.optimize(fuse=False).expr.find_operations(Shuffle))) == 2

    pdf["c"] = "a"
    pdf = pdf.set_index("c")
    df = from_pandas(pdf, npartitions=3)
    df["new"] = df2.x
    with pytest.raises(TypeError, match="have differing dtypes"):
        df.optimize()
