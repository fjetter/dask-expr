import functools
import math
import operator

from dask.dataframe.dispatch import make_meta, meta_nonempty
from dask.dataframe.multi import (
    _concat_wrapper,
    _merge_chunk_wrapper,
    _split_partition,
    merge_chunk,
)
from dask.dataframe.shuffle import partitioning_index
from dask.utils import apply, get_default_shuffle_method
from toolz import merge_sorted, unique

from dask_expr._expr import (  # noqa: F401
    And,
    Binop,
    Blockwise,
    DropDuplicatesBlockwise,
    Elemwise,
    Expr,
    Filter,
    Index,
    Isin,
    PartitionsFiltered,
    Projection,
    Unaryop,
    _DelayedExpr,
    are_co_aligned,
    determine_column_projection,
    is_filter_pushdown_available,
)
from dask_expr._repartition import Repartition
from dask_expr._shuffle import (
    RearrangeByColumn,
    _contains_index_name,
    _select_columns_or_index,
)
from dask_expr._util import _convert_to_list, _tokenize_deterministic, is_scalar

_HASH_COLUMN_NAME = "__hash_partition"
_PARTITION_COLUMN = "_partitions"


class Merge(Expr):
    """Merge / join two dataframes

    This is an abstract class.  It will be transformed into a concrete
    implementation before graph construction.

    See Also
    --------
    BlockwiseMerge
    Repartition
    Shuffle
    """

    _parameters = [
        "left",
        "right",
        "how",
        "left_on",
        "right_on",
        "left_index",
        "right_index",
        "suffixes",
        "indicator",
        "shuffle_method",
        "_npartitions",
        "broadcast",
    ]
    _defaults = {
        "how": "inner",
        "left_on": None,
        "right_on": None,
        "left_index": False,
        "right_index": False,
        "suffixes": ("_x", "_y"),
        "indicator": False,
        "shuffle_method": None,
        "_npartitions": None,
        "broadcast": None,
    }

    @property
    def _filter_passthrough(self):
        raise NotImplementedError(
            "please use _filter_passthrough_available to make this decision"
        )

    def _filter_passthrough_available(self, parent, dependents):
        if is_filter_pushdown_available(self, parent, dependents):
            predicate = parent.predicate
            # This protects against recursion, no need to separate ands if the first
            # condition violates the join direction
            while isinstance(predicate, And):
                predicate = predicate.left
            predicate_columns = self._predicate_columns(predicate)
            if predicate_columns is None:
                return False
            if predicate_columns.issubset(self.left.columns):
                return self.how in ("left", "inner", "leftsemi")
            elif predicate_columns.issubset(self.right.columns):
                return self.how in ("right", "inner")
            elif len(predicate_columns) > 0:
                return False
            return True
        elif isinstance(parent.predicate, And):
            # If we can make that transformation then we should do it to further
            # align filters that sit on top of merges
            new = Filter(self, parent.predicate.left)
            return new._name in {
                x()._name for x in dependents[self._name] if x() is not None
            }
        return False

    def _predicate_columns(self, predicate):
        if isinstance(predicate, (Projection, Unaryop, Isin)):
            return self._get_original_predicate_columns(predicate)
        elif isinstance(predicate, Binop):
            if isinstance(predicate, And):
                return None

            if not isinstance(predicate.right, Expr):
                return self._get_original_predicate_columns(predicate.left)
            elif isinstance(predicate.right, Elemwise):
                return self._get_original_predicate_columns(predicate)
            else:
                return None
        else:
            # Unsupported predicate type
            return None

    def _get_original_predicate_columns(self, predicate):
        predicate_columns = set()
        stack = [predicate]
        seen = set()
        while stack:
            e = stack.pop()
            if self._name == e._name:
                continue

            if e._name in seen:
                continue
            seen.add(e._name)

            if isinstance(e, _DelayedExpr):
                continue

            dependencies = e.dependencies()
            stack.extend(dependencies)
            if any(d._name == self._name for d in dependencies):
                predicate_columns.update(e.columns)
        return predicate_columns

    def __str__(self):
        return f"Merge({self._name[-7:]})"

    @property
    def kwargs(self):
        return {
            k: self.operand(k)
            for k in [
                "how",
                "left_on",
                "right_on",
                "left_index",
                "right_index",
                "suffixes",
                "indicator",
            ]
        }

    @functools.cached_property
    def _meta(self):
        left = meta_nonempty(self.left._meta)
        right = meta_nonempty(self.right._meta)
        kwargs = self.kwargs.copy()
        if kwargs["how"] == "leftsemi":
            kwargs["how"] = "left"
        return make_meta(left.merge(right, **kwargs))

    @functools.cached_property
    def _npartitions(self):
        if self.operand("_npartitions") is not None:
            return self.operand("_npartitions")
        return max(self.left.npartitions, self.right.npartitions)

    @property
    def _bcast_left(self):
        if self.operand("_npartitions") is not None:
            if self.broadcast_side == "right":
                return Repartition(self.left, new_partitions=self._npartitions)
        return self.left

    @property
    def _bcast_right(self):
        if self.operand("_npartitions") is not None:
            if self.broadcast_side == "left":
                return Repartition(self.right, new_partitions=self._npartitions)
        return self.right

    def _divisions(self):
        if self.merge_indexed_left and self.merge_indexed_right:
            divisions = list(
                unique(merge_sorted(self.left.divisions, self.right.divisions))
            )
            if len(divisions) == 1:
                return (divisions[0], divisions[0])
            if self.left.npartitions == 1 and self.right.npartitions == 1:
                return (min(divisions), max(divisions))
            return divisions

        if self._is_single_partition_broadcast:
            use_left = self.right_index or _contains_index_name(
                self.right._meta, self.right_on
            )
            use_right = self.left_index or _contains_index_name(
                self.left._meta, self.left_on
            )
            if (
                use_right
                and self.left.npartitions == 1
                and self.how in ("right", "inner")
            ):
                return self.right.divisions
            elif (
                use_left
                and self.right.npartitions == 1
                and self.how in ("inner", "left", "leftsemi")
            ):
                return self.left.divisions
            else:
                _npartitions = max(self.left.npartitions, self.right.npartitions)

        elif self.is_broadcast_join:
            meta_index_names = set(self._meta.index.names)
            if (
                self.broadcast_side == "left"
                and set(self.right._meta.index.names) == meta_index_names
            ):
                return self._bcast_right._divisions()
            elif (
                self.broadcast_side == "right"
                and set(self.left._meta.index.names) == meta_index_names
            ):
                return self._bcast_left._divisions()
            _npartitions = max(self.left.npartitions, self.right.npartitions)

        else:
            _npartitions = self._npartitions

        return (None,) * (_npartitions + 1)

    @functools.cached_property
    def broadcast_side(self):
        return "left" if self.left.npartitions < self.right.npartitions else "right"

    @functools.cached_property
    def is_broadcast_join(self):
        broadcast_bias, broadcast = 0.5, None
        broadcast_side = self.broadcast_side
        if isinstance(self.broadcast, float):
            broadcast_bias = self.broadcast
        elif isinstance(self.broadcast, bool):
            broadcast = self.broadcast

        s_method = self.shuffle_method or get_default_shuffle_method()
        if (
            s_method in ("tasks", "p2p")
            and self.how in ("inner", "left", "right", "leftsemi")
            and self.how != broadcast_side
            and broadcast is not False
        ):
            n_low = min(self.left.npartitions, self.right.npartitions)
            n_high = max(self.left.npartitions, self.right.npartitions)
            if broadcast or (n_low < math.log2(n_high) * broadcast_bias):
                return True
        return False

    @functools.cached_property
    def _is_single_partition_broadcast(self):
        _npartitions = max(self.left.npartitions, self.right.npartitions)
        return (
            _npartitions == 1
            or self.left.npartitions == 1
            and self.how in ("right", "inner")
            or self.right.npartitions == 1
            and self.how in ("left", "inner", "leftsemi")
        )

    @functools.cached_property
    def merge_indexed_left(self):
        return (
            self.left_index or _contains_index_name(self.left, self.left_on)
        ) and self.left.known_divisions

    @functools.cached_property
    def merge_indexed_right(self):
        return (
            self.right_index or _contains_index_name(self.right, self.right_on)
        ) and self.right.known_divisions

    @functools.cached_property
    def merge_indexed_left(self):
        return (
            self.left_index or _contains_index_name(self.left, self.left_on)
        ) and self.left.known_divisions

    @functools.cached_property
    def merge_indexed_right(self):
        return (
            self.right_index or _contains_index_name(self.right, self.right_on)
        ) and self.right.known_divisions

    def _lower(self):
        # Lower from an abstract expression
        left = self.left
        right = self.right
        left_on = self.left_on
        right_on = self.right_on
        left_index = self.left_index
        right_index = self.right_index
        shuffle_method = self.shuffle_method

        # TODO:
        #  1. Add/leverage partition statistics

        # Check for "trivial" broadcast (single partition)
        if self._is_single_partition_broadcast:
            return BlockwiseMerge(left, right, **self.kwargs)

        # NOTE: Merging on an index is fragile. Pandas behavior
        # depends on the actual data, and so we cannot use `meta`
        # to accurately predict the output columns. Once general
        # partition statistics are available, it may make sense
        # to drop support for left_index and right_index.

        shuffle_left_on = left_on
        shuffle_right_on = right_on
        if self.merge_indexed_left and self.merge_indexed_right:
            # fully-indexed merge
            divisions = list(unique(merge_sorted(left.divisions, right.divisions)))
            if len(divisions) == 1:
                divisions = (divisions[0], divisions[0])
            right = Repartition(right, new_divisions=divisions, force=True)
            left = Repartition(left, new_divisions=divisions, force=True)
            shuffle_left_on = shuffle_right_on = None

        # TODO:
        #   - Need 'rearrange_by_divisions' equivalent
        #     to avoid shuffle when we are merging on known
        #     divisions on one side only.
        else:
            if left_index:
                shuffle_left_on = left.index._meta.name
                if shuffle_left_on is None:
                    # placeholder for unnamed index merge
                    shuffle_left_on = "_index"
            if right_index:
                shuffle_right_on = right.index._meta.name
                if shuffle_right_on is None:
                    shuffle_right_on = "_index"
            if self.is_broadcast_join:
                left, right = self._bcast_left, self._bcast_right

                if self.how != "inner":
                    if self.broadcast_side == "left":
                        left = RearrangeByColumn(
                            left,
                            shuffle_left_on,
                            npartitions_out=left.npartitions,
                        )
                    else:
                        right = RearrangeByColumn(
                            right,
                            shuffle_right_on,
                            npartitions_out=right.npartitions,
                        )

                return BroadcastJoin(
                    left,
                    right,
                    self.how,
                    left_on,
                    right_on,
                    left_index,
                    right_index,
                    self.suffixes,
                    self.indicator,
                )

        if (shuffle_left_on or shuffle_right_on) and (
            shuffle_method == "p2p"
            or shuffle_method is None
            and get_default_shuffle_method() == "p2p"
        ):
            return HashJoinP2P(
                left,
                right,
                how=self.how,
                left_on=left_on,
                right_on=right_on,
                suffixes=self.suffixes,
                indicator=self.indicator,
                left_index=left_index,
                right_index=right_index,
                shuffle_left_on=shuffle_left_on,
                shuffle_right_on=shuffle_right_on,
                _npartitions=self.operand("_npartitions"),
            )

        if shuffle_left_on:
            # Shuffle left
            left = RearrangeByColumn(
                left,
                shuffle_left_on,
                npartitions_out=self._npartitions,
                method=shuffle_method,
                index_shuffle=left_index,
            )

        if shuffle_right_on:
            # Shuffle right
            right = RearrangeByColumn(
                right,
                shuffle_right_on,
                npartitions_out=self._npartitions,
                method=shuffle_method,
                index_shuffle=right_index,
            )

        # Blockwise merge
        return BlockwiseMerge(left, right, **self.kwargs)

    def _simplify_up(self, parent, dependents):
        if isinstance(parent, Filter):
            if not self._filter_passthrough_available(parent, dependents):
                return
            predicate = parent.predicate

            if isinstance(predicate, And):
                new = Filter(self, predicate.left)
                new_pred = predicate.right.substitute(self, new)
                return Filter(new, new_pred)

            predicate_cols = self._predicate_columns(parent.predicate)
            new_left, new_right = self.left, self.right
            left_suffix, right_suffix = self.suffixes[0], self.suffixes[1]
            if predicate_cols and predicate_cols.issubset(self.left.columns):
                if left_suffix != "" and any(
                    f"{col}{left_suffix}" in self.columns and col in self.right.columns
                    for col in predicate_cols
                ):
                    # column was renamed so the predicate must go into the other side
                    pass
                else:
                    left_filter = predicate.substitute(self, self.left)
                    new_left = self.left[left_filter]
            if predicate_cols and predicate_cols.issubset(self.right.columns):
                if right_suffix != "" and any(
                    f"{col}{right_suffix}" in self.columns and col in self.left.columns
                    for col in predicate_cols
                ):
                    # column was renamed so the predicate must go into the other side
                    pass
                else:
                    right_filter = predicate.substitute(self, self.right)
                    new_right = self.right[right_filter]
            if new_right is self.right and new_left is self.left:
                # don't drop the filter
                return
            return type(self)(new_left, new_right, *self.operands[2:])
        if isinstance(parent, (Projection, Index)):
            # Reorder the column projection to
            # occur before the Merge
            columns = determine_column_projection(self, parent, dependents)
            columns = _convert_to_list(columns)
            if isinstance(parent, Index):
                # Index creates an empty column projection
                projection, parent_columns = columns, None
            else:
                projection, parent_columns = columns, parent.operand("columns")
            if is_scalar(projection):
                projection = [projection]

            left, right = self.left, self.right
            left_on = _convert_to_list(self.left_on)
            if left_on is None:
                left_on = []

            right_on = _convert_to_list(self.right_on)
            if right_on is None:
                right_on = []

            left_suffix, right_suffix = self.suffixes[0], self.suffixes[1]
            project_left, project_right = [], []

            # Find columns to project on the left
            for col in left.columns:
                if col in left_on or col in projection:
                    project_left.append(col)
                elif f"{col}{left_suffix}" in projection:
                    project_left.append(col)
                    if col in right.columns:
                        # Right column must be present
                        # for the suffix to be applied
                        project_right.append(col)

            # Find columns to project on the right
            for col in right.columns:
                if col in right_on or col in projection:
                    project_right.append(col)
                elif f"{col}{right_suffix}" in projection:
                    project_right.append(col)
                    if col in left.columns and col not in project_left:
                        # Left column must be present
                        # for the suffix to be applied
                        project_left.append(col)

            if set(project_left) < set(left.columns) or set(project_right) < set(
                right.columns
            ):
                result = type(self)(
                    left[project_left], right[project_right], *self.operands[2:]
                )
                if parent_columns is None:
                    return type(parent)(result)
                return result[parent_columns]


class HashJoinP2P(Merge, PartitionsFiltered):
    _parameters = [
        "left",
        "right",
        "how",
        "left_on",
        "right_on",
        "left_index",
        "right_index",
        "suffixes",
        "indicator",
        "_partitions",
        "shuffle_left_on",
        "shuffle_right_on",
        "_npartitions",
    ]
    _defaults = {
        "how": "inner",
        "left_on": None,
        "right_on": None,
        "left_index": None,
        "right_index": None,
        "suffixes": ("_x", "_y"),
        "indicator": False,
        "_partitions": None,
        "shuffle_left_on": None,
        "shuffle_right_on": None,
        "_npartitions": None,
    }
    is_broadcast_join = False

    def _lower(self):
        return None

    def _layer(self) -> dict:
        from distributed.shuffle._core import ShuffleId, barrier_key
        from distributed.shuffle._merge import merge_unpack
        from distributed.shuffle._shuffle import shuffle_barrier

        dsk = {}
        token_left = _tokenize_deterministic(
            # Include self._name to ensure that shuffle IDs are unique for individual
            # merge operations. Reusing shuffles between merges is dangerous because of
            # required coordination and complexity introduced through dynamic clusters.
            self._name,
            self.left._name,
            self.shuffle_left_on,
            self.left_index,
        )
        token_right = _tokenize_deterministic(
            # Include self._name to ensure that shuffle IDs are unique for individual
            # merge operations. Reusing shuffles between merges is dangerous because of
            # required coordination and complexity introduced through dynamic clusters.
            self._name,
            self.right._name,
            self.shuffle_right_on,
            self.right_index,
        )
        _barrier_key_left = barrier_key(ShuffleId(token_left))
        _barrier_key_right = barrier_key(ShuffleId(token_right))

        transfer_name_left = "hash-join-transfer-" + token_left
        transfer_name_right = "hash-join-transfer-" + token_right
        transfer_keys_left = list()
        transfer_keys_right = list()
        func = create_assign_index_merge_transfer()
        for i in range(self.left.npartitions):
            transfer_keys_left.append((transfer_name_left, i))
            dsk[(transfer_name_left, i)] = (
                func,
                (self.left._name, i),
                self.shuffle_left_on,
                _HASH_COLUMN_NAME,
                self.npartitions,
                token_left,
                i,
                self.left._meta,
                self._partitions,
                self.left_index,
            )
        for i in range(self.right.npartitions):
            transfer_keys_right.append((transfer_name_right, i))
            dsk[(transfer_name_right, i)] = (
                func,
                (self.right._name, i),
                self.shuffle_right_on,
                _HASH_COLUMN_NAME,
                self.npartitions,
                token_right,
                i,
                self.right._meta,
                self._partitions,
                self.right_index,
            )

        dsk[_barrier_key_left] = (shuffle_barrier, token_left, transfer_keys_left)
        dsk[_barrier_key_right] = (
            shuffle_barrier,
            token_right,
            transfer_keys_right,
        )

        for part_out in self._partitions:
            dsk[(self._name, part_out)] = (
                merge_unpack,
                token_left,
                token_right,
                part_out,
                _barrier_key_left,
                _barrier_key_right,
                self.how,
                self.left_on,
                self.right_on,
                self._meta,
                self.suffixes,
                self.left_index,
                self.right_index,
                self.indicator,
            )
        return dsk

    def _simplify_up(self, parent, dependents):
        return


class BroadcastJoin(Merge, PartitionsFiltered):
    _parameters = [
        "left",
        "right",
        "how",
        "left_on",
        "right_on",
        "left_index",
        "right_index",
        "suffixes",
        "indicator",
        "_partitions",
    ]
    _defaults = {
        "how": "inner",
        "left_on": None,
        "right_on": None,
        "left_index": None,
        "right_index": None,
        "suffixes": ("_x", "_y"),
        "indicator": False,
        "_partitions": None,
    }

    def _divisions(self):
        if self.broadcast_side == "left":
            return self.right._divisions()
        return self.left._divisions()

    def _simplify_up(self, parent, dependents):
        return

    def _lower(self):
        return None

    def _layer(self) -> dict:
        if self.broadcast_side == "left":
            bcast_name = self.left._name
            bcast_size = self.left.npartitions
            other = self.right._name
            other_on = self.right_on
        else:
            bcast_name = self.right._name
            bcast_size = self.right.npartitions
            other = self.left._name
            other_on = self.left_on

        split_name = "split-" + self._name
        inter_name = "inter-" + self._name
        kwargs = {
            "how": self.how,
            "indicator": self.indicator,
            "left_index": self.left_index,
            "right_index": self.right_index,
            "suffixes": self.suffixes,
            "result_meta": self._meta,
            "left_on": self.left_on,
            "right_on": self.right_on,
        }
        dsk = {}
        for part_out in self._partitions:
            if self.how != "inner":
                dsk[(split_name, part_out)] = (
                    _split_partition,
                    (other, part_out),
                    other_on,
                    bcast_size,
                )

            _concat_list = []
            for j in range(bcast_size):
                # Specify arg list for `merge_chunk`
                _merge_args = [
                    (
                        (
                            operator.getitem,
                            (split_name, part_out),
                            j,
                        )
                        if self.how != "inner"
                        else (other, part_out)
                    ),
                    (bcast_name, j),
                ]
                if self.broadcast_side in ("left", "leftsemi"):
                    _merge_args.reverse()

                inter_key = (inter_name, part_out, j)
                dsk[(inter_name, part_out, j)] = (
                    apply,
                    _merge_chunk_wrapper,
                    _merge_args,
                    kwargs,
                )
                _concat_list.append(inter_key)
            dsk[(self._name, part_out)] = (_concat_wrapper, _concat_list)
        return dsk


def create_assign_index_merge_transfer():
    import pandas as pd
    from distributed.shuffle._core import ShuffleId
    from distributed.shuffle._merge import merge_transfer

    def assign_index_merge_transfer(
        df,
        index,
        name,
        npartitions,
        id: ShuffleId,
        input_partition: int,
        meta: pd.DataFrame,
        parts_out: set[int],
        index_merge,
    ):
        if index_merge:
            index = df[[]]
            index["_index"] = df.index
        else:
            index = _select_columns_or_index(df, index)
        if isinstance(index, (str, list, tuple)):
            # Assume column selection from df
            index = [index] if isinstance(index, str) else list(index)
            index = partitioning_index(df[index], npartitions)
        else:
            index = partitioning_index(index, npartitions)
        df = df.assign(**{name: index})
        meta = meta.assign(**{name: 0})
        return merge_transfer(
            df, id, input_partition, npartitions, meta, parts_out, True
        )

    return assign_index_merge_transfer


class SemiMerge(Merge):
    def _lower(self):
        # This is cheap and avoids shuffling unnecessary data
        right = DropDuplicatesBlockwise(self.right)
        return Merge(self.left, right, *self.operands[2:])


class BlockwiseMerge(Merge, Blockwise):
    """Merge two dataframes with aligned partitions

    This operation will directly merge partition i of the
    left dataframe with partition i of the right dataframe.
    The two dataframes must be shuffled or partitioned
    by the merge key(s) before this operation is performed.
    Single-partition dataframes will always be broadcasted.

    See Also
    --------
    Merge
    """

    is_broadcast_join = False

    def _divisions(self):
        if self.left.npartitions == self.right.npartitions:
            return super()._divisions()
        is_unknown = any(d is None for d in super()._divisions())
        frame = (
            self.left if self.left.npartitions > self.right.npartitions else self.right
        )
        if is_unknown:
            return (None,) * (frame.npartitions + 1)
        return frame.divisions

    def _lower(self):
        return None

    def _broadcast_dep(self, dep: Expr):
        return dep.npartitions == 1

    def _task(self, index: int):
        kwargs = self.kwargs.copy()
        kwargs["result_meta"] = self._meta
        return (
            apply,
            merge_chunk,
            [
                self._blockwise_arg(self.left, index),
                self._blockwise_arg(self.right, index),
            ],
            kwargs,
        )


class JoinRecursive(Expr):
    _parameters = ["frames", "how"]
    _defaults = {"right_index": True, "how": "outer"}

    @functools.cached_property
    def _meta(self):
        if len(self.frames) == 1:
            return self.frames[0]._meta
        else:
            return self.frames[0]._meta.join(
                [op._meta for op in self.frames[1:]],
            )

    def _divisions(self):
        return self.lower_completely().divisions

    def _lower(self):
        if self.how == "left":
            right = self._recursive_join(self.frames[1:])
            return Merge(
                self.frames[0],
                right,
                how=self.how,
                left_index=True,
                right_index=True,
            )

        return self._recursive_join(self.frames)

    def _recursive_join(self, frames):
        if len(frames) == 1:
            return frames[0]

        if len(frames) == 2:
            return Merge(
                frames[0],
                frames[1],
                how="outer",
                left_index=True,
                right_index=True,
            )

        midx = len(frames) // 2

        return self._recursive_join(
            [
                self._recursive_join(frames[:midx]),
                self._recursive_join(frames[midx:]),
            ],
        )
