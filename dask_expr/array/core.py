import operator
from typing import Union

import dask.array as da
import numpy as np
from dask.base import DaskMethodsMixin, named_schedulers
from dask.utils import cached_cumsum, cached_property
from toolz import reduce

from dask_expr import _core as core

T_IntOrNaN = Union[int, float]  # Should be Union[int, Literal[np.nan]]


class Array(core.Expr, DaskMethodsMixin):
    _cached_keys = None

    __dask_scheduler__ = staticmethod(
        named_schedulers.get("threads", named_schedulers["sync"])
    )
    __dask_optimize__ = staticmethod(lambda dsk, keys, **kwargs: dsk)

    def __dask_postcompute__(self):
        return da.core.finalize, ()

    def __dask_postpersist__(self):
        return FromGraph, (self._meta, self.chunks, self._name)

    def compute(self, **kwargs):
        return DaskMethodsMixin.compute(self.simplify(), **kwargs)

    def persist(self, **kwargs):
        return DaskMethodsMixin.persist(self.simplify(), **kwargs)

    def __array_ufunc__(self, numpy_ufunc, method, *inputs, **kwargs):
        raise NotImplementedError()

    def __array_function__(self, *args, **kwargs):
        raise NotImplementedError()

    def __array__(self):
        return self.compute()

    def __getitem__(self, index):
        from dask.array.slicing import normalize_index

        from dask_expr.array.slicing import Slice

        if not isinstance(index, tuple):
            index = (index,)

        index2 = normalize_index(index, self.shape)

        # TODO: handle slicing with dask array

        return Slice(self, index2)

    @cached_property
    def shape(self) -> tuple[T_IntOrNaN, ...]:
        return tuple(cached_cumsum(c, initial_zero=True)[-1] for c in self.chunks)

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def chunksize(self) -> tuple[T_IntOrNaN, ...]:
        return tuple(max(c) for c in self.chunks)

    @property
    def dtype(self):
        if isinstance(self._meta, tuple):
            dtype = self._meta[0].dtype
        else:
            dtype = self._meta.dtype
        return dtype

    def __dask_keys__(self):
        if self._cached_keys is not None:
            return self._cached_keys

        name, chunks, numblocks = self.name, self.chunks, self.numblocks

        def keys(*args):
            if not chunks:
                return [(name,)]
            ind = len(args)
            if ind + 1 == len(numblocks):
                result = [(name,) + args + (i,) for i in range(numblocks[ind])]
            else:
                result = [keys(*(args + (i,))) for i in range(numblocks[ind])]
            return result

        self._cached_keys = result = keys()
        return result

    @cached_property
    def numblocks(self):
        return tuple(map(len, self.chunks))

    @cached_property
    def npartitions(self):
        return reduce(operator.mul, self.numblocks, 1)

    @property
    def name(self):
        return self._name

    def __hash__(self):
        return hash(self._name)

    def optimize(self):
        return self.simplify()

    def rechunk(
        self,
        chunks="auto",
        threshold=None,
        block_size_limit=None,
        balance=False,
        method=None,
    ):
        from dask_expr.array.rechunk import Rechunk

        return Rechunk(self, chunks, threshold, block_size_limit, balance, method)

    def transpose(self, axes=None):
        if axes:
            if len(axes) != self.ndim:
                raise ValueError("axes don't match array")
            axes = tuple(d + self.ndim if d < 0 else d for d in axes)
        else:
            axes = tuple(range(self.ndim))[::-1]

        return Transpose(self, axes)

    @property
    def T(self):
        return self.transpose()

    def __add__(self, other):
        return elemwise(operator.add, self, other)

    def __radd__(self, other):
        return elemwise(operator.add, other, self)

    def __mul__(self, other):
        return elemwise(operator.add, self, other)

    def __rmul__(self, other):
        return elemwise(operator.mul, other, self)

    def __sub__(self, other):
        return elemwise(operator.sub, self, other)

    def __rsub__(self, other):
        return elemwise(operator.sub, other, self)

    def __pow__(self, other):
        return elemwise(operator.pow, self, other)

    def __rpow__(self, other):
        return elemwise(operator.pow, other, self)

    def __truediv__(self, other):
        return elemwise(operator.truediv, self, other)

    def __rtruediv__(self, other):
        return elemwise(operator.truediv, other, self)

    def __floordiv__(self, other):
        return elemwise(operator.floordiv, self, other)

    def __rfloordiv__(self, other):
        return elemwise(operator.floordiv, other, self)

    def __array_ufunc__(self, numpy_ufunc, method, *inputs, **kwargs):
        out = kwargs.get("out", ())
        for x in inputs + out:
            if da.core._should_delegate(self, x):
                return NotImplemented

        if method == "__call__":
            if numpy_ufunc is np.matmul:
                return NotImplemented
            if numpy_ufunc.signature is not None:
                return NotImplemented
            if numpy_ufunc.nout > 1:
                return NotImplemented
            else:
                return elemwise(numpy_ufunc, *inputs, **kwargs)
        elif method == "outer":
            return NotImplemented
        else:
            return NotImplemented

    @cached_property
    def size(self):
        """Number of elements in array"""
        return reduce(operator.mul, self.shape, 1)

    def any(self, axis=None, keepdims=False, split_every=None, out=None):
        """Returns True if any of the elements evaluate to True.

        Refer to :func:`dask.array.any` for full documentation.

        See Also
        --------
        dask.array.any : equivalent function
        """
        from dask_expr.array.reductions import any

        return any(self, axis=axis, keepdims=keepdims, split_every=split_every, out=out)

    def all(self, axis=None, keepdims=False, split_every=None, out=None):
        """Returns True if all elements evaluate to True.

        Refer to :func:`dask.array.all` for full documentation.

        See Also
        --------
        dask.array.all : equivalent function
        """
        from dask_expr.array.reductions import all

        return all(self, axis=axis, keepdims=keepdims, split_every=split_every, out=out)

    def min(self, axis=None, keepdims=False, split_every=None, out=None):
        """Return the minimum along a given axis.

        Refer to :func:`dask.array.min` for full documentation.

        See Also
        --------
        dask.array.min : equivalent function
        """
        from dask_expr.array.reductions import min

        return min(self, axis=axis, keepdims=keepdims, split_every=split_every, out=out)

    def max(self, axis=None, keepdims=False, split_every=None, out=None):
        """Return the maximum along a given axis.

        Refer to :func:`dask.array.max` for full documentation.

        See Also
        --------
        dask.array.max : equivalent function
        """
        from dask_expr.array.reductions import max

        return max(self, axis=axis, keepdims=keepdims, split_every=split_every, out=out)

    def argmin(self, axis=None, *, keepdims=False, split_every=None, out=None):
        """Return indices of the minimum values along the given axis.

        Refer to :func:`dask.array.argmin` for full documentation.

        See Also
        --------
        dask.array.argmin : equivalent function
        """
        from dask_expr.array.reductions import argmin

        return argmin(
            self, axis=axis, keepdims=keepdims, split_every=split_every, out=out
        )

    def argmax(self, axis=None, *, keepdims=False, split_every=None, out=None):
        """Return indices of the maximum values along the given axis.

        Refer to :func:`dask.array.argmax` for full documentation.

        See Also
        --------
        dask.array.argmax : equivalent function
        """
        from dask_expr.array.reductions import argmax

        return argmax(
            self, axis=axis, keepdims=keepdims, split_every=split_every, out=out
        )

    def sum(self, axis=None, dtype=None, keepdims=False, split_every=None, out=None):
        """
        Return the sum of the array elements over the given axis.

        Refer to :func:`dask.array.sum` for full documentation.

        See Also
        --------
        dask.array.sum : equivalent function
        """
        from dask_expr.array.reductions import sum

        return sum(
            self,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            split_every=split_every,
            out=out,
        )

    def mean(self, axis=None, dtype=None, keepdims=False, split_every=None, out=None):
        """Returns the average of the array elements along given axis.

        Refer to :func:`dask.array.mean` for full documentation.

        See Also
        --------
        dask.array.mean : equivalent function
        """
        from dask_expr.array.reductions import mean

        return mean(
            self,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            split_every=split_every,
            out=out,
        )

    def std(
        self, axis=None, dtype=None, keepdims=False, ddof=0, split_every=None, out=None
    ):
        """Returns the standard deviation of the array elements along given axis.

        Refer to :func:`dask.array.std` for full documentation.

        See Also
        --------
        dask.array.std : equivalent function
        """
        from dask_expr.array.reductions import std

        return std(
            self,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            ddof=ddof,
            split_every=split_every,
            out=out,
        )

    def var(
        self, axis=None, dtype=None, keepdims=False, ddof=0, split_every=None, out=None
    ):
        """Returns the variance of the array elements, along given axis.

        Refer to :func:`dask.array.var` for full documentation.

        See Also
        --------
        dask.array.var : equivalent function
        """
        from dask_expr.array.reductions import var

        return var(
            self,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            ddof=ddof,
            split_every=split_every,
            out=out,
        )

    def moment(
        self,
        order,
        axis=None,
        dtype=None,
        keepdims=False,
        ddof=0,
        split_every=None,
        out=None,
    ):
        """Calculate the nth centralized moment.

        Refer to :func:`dask.array.moment` for the full documentation.

        See Also
        --------
        dask.array.moment : equivalent function
        """
        from dask_expr.array.reductions import moment

        return moment(
            self,
            order,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            ddof=ddof,
            split_every=split_every,
            out=out,
        )

    def prod(self, axis=None, dtype=None, keepdims=False, split_every=None, out=None):
        """Return the product of the array elements over the given axis

        Refer to :func:`dask.array.prod` for full documentation.

        See Also
        --------
        dask.array.prod : equivalent function
        """
        from dask_expr.array.reductions import prod

        return prod(
            self,
            axis=axis,
            dtype=dtype,
            keepdims=keepdims,
            split_every=split_every,
            out=out,
        )


class IO(Array):
    pass


class FromArray(IO):
    _parameters = ["array", "chunks"]

    @property
    def chunks(self):
        return da.core.normalize_chunks(
            self.operand("chunks"), self.array.shape, dtype=self.array.dtype
        )

    @property
    def _meta(self):
        return self.array[tuple(slice(0, 0) for _ in range(self.array.ndim))]

    def _layer(self):
        dsk = da.core.graph_from_arraylike(
            self.array, chunks=self.chunks, shape=self.array.shape, name=self._name
        )
        return dict(dsk)  # this comes as a legacy HLG for now

    def __str__(self):
        return "FromArray(...)"


class FromGraph(Array):
    _parameters = ["layer", "_meta", "chunks", "_name"]

    @property
    def _meta(self):
        return self.operand("_meta")

    @property
    def chunks(self):
        return self.operand("chunks")

    @property
    def _name(self):
        return self.operand("_name")

    def _layer(self):
        return dict(self.operand("layer"))


def from_array(x, chunks="auto"):
    return FromArray(x, chunks)


from dask_expr.array.blockwise import Transpose, elemwise
