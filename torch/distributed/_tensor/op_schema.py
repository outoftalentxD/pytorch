from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

import torch
from torch.distributed._tensor.device_mesh import DeviceMesh
from torch.distributed._tensor.placement_types import DTensorSpec
from torch.utils._pytree import tree_map_only, TreeSpec


# Common type aliases
ArgsType = Tuple[object, ...]
KwargsType = Dict[str, object]
# ATen op schemas could have Tensor, Tuple[Tensor] and List[Tensor], so output type sould
# be the same set of possibilities.
OutputSpecType = Optional[Union[DTensorSpec, Sequence[Optional[DTensorSpec]]]]


def _rebuild_tensor_from_dtensor_meta(arg) -> object:
    """ "
    This is used to propagate tensor metadata, must be under fake mode
    """
    assert arg.tensor_meta is not None, "DTensorSpec does not contain tensor_meta."
    return torch.empty_strided(
        arg.tensor_meta.shape,
        arg.tensor_meta.stride,
        dtype=arg.tensor_meta.dtype,
    )


def _is_inplace_op(op: torch._ops.OpOverload):
    # simple analysis of function schema to determine
    # if this is an inplace variant, it might not
    # be entirely correct, but it's good enough for now.
    return op._schema.name[-1] == "_"


def _is_out_variant_op(op: torch._ops.OpOverload):
    # simple analysis of function schema to determine
    # if this is an out variant, it might not
    # be entirely correct, but it's good enough for now.
    return "out" in op._schema.overload_name


@dataclass
class PlacementStrategy:
    """
    A placement strategy describes an acceptable sharding placements of the output
    and the tensor arguments of an operation.
    """

    output_spec: DTensorSpec
    input_specs: Optional[Sequence[DTensorSpec]] = None

    def pretty_print_placements(self, placements):
        return "".join([str(p) for p in placements])

    def __str__(self) -> str:
        if self.input_specs is None:
            input_specs_str = ""
        else:
            input_specs_str = ", ".join(
                [
                    self.pretty_print_placements(spec.placements)
                    for spec in self.input_specs
                ]
            )
        output_spec_str = self.pretty_print_placements(self.output_spec.placements)
        return f"({input_specs_str}) -> ({output_spec_str}) @ mesh layout: {tuple(self.output_spec.mesh.mesh.shape)}"


class StrategyType:
    """
    Base class type for op strategy, We have two StrategyType:
        OpStrategy and TupleStrategy
    """

    pass


class OpStrategy(StrategyType):
    """
    OpStrategy that consists of a list of placement strategies associated with the op
    """

    def __init__(self, strategies: List[PlacementStrategy]) -> None:
        super().__init__()
        self.strategies: List[PlacementStrategy] = strategies

    def __str__(self) -> str:
        strategy_list_str = ", ".join([str(strategy) for strategy in self.strategies])
        return f"OpStrategy: [{strategy_list_str}]"

    def max_num_shards(self) -> int:
        """
        Returns the max number of shards across all placement strategies
        """
        return max([strategy.output_spec.num_shards for strategy in self.strategies])

    @property
    def output_shape(self):
        return self.strategies[0].output_spec.shape

    @property
    def output_ndim(self):
        return self.strategies[0].output_spec.ndim


class TupleStrategy(StrategyType):
    """
    TupleStrategy represents the output strategy of this op is a tuple
    of strategy, i.e. If the output of this op is a tuple of tensors, we should
    return a TupleStrategy that contains a tuple of OpStrategy.

    NOTE: if the output of the op is a List[Tensor], it's likely we should return
    OpStrategy directly in all cases.
    """

    def __init__(self, childs: Tuple[StrategyType, ...]) -> None:
        super().__init__()
        self.childs: Tuple[StrategyType, ...] = childs

    def __str__(self) -> str:
        tuple_strategies_str = "TupleStrategy: "
        child_strategies_str = "\n".join(
            [
                f" tuple idx: {idx}, strategy: {str(strat)}"
                for idx, strat in enumerate(self.childs)
            ]
        )
        return f"{tuple_strategies_str}\n{child_strategies_str}"


@dataclass
class RuntimeSchemaInfo:
    """
    RuntimeSchemaInfo stores the operator schema related information for runtime (eager)
    execution. This is mainly used for two ways: 1. to generate hash for args to determine
    whether to re-run sharding prop or not 2. to determine if we need pytree
    """

    # This static_argnum records static arg "starting index" for ops that have non-tensor
    # args/kwargs which would affect sharding propagation results. All args after this
    # index would be hashed to our sharding cache.
    # Note that only a few ops need this information, e.g. view, transpose, var.dim, etc.
    static_argnum: int = 100
    # This static_kwargkey records static kwarg names which would affect sharding prop
    static_kwargkey: Optional[List[str]] = None
    # TODO: make use of this field
    needs_pytree: bool = True


@dataclass
class OpSchema:
    """
    OpSchema is a data class that describes an operator input schemas, it
    includes DTensor DTensorSpecs and non-tensor args/kwargs (positional order
    preserved). It is mainly used by the dispatching logic below to run things like
    sharding propagation.

    NOTE: this should be used as a read only data class
    TODO: make this a frozen dataclass

    Args:
        op: the operator overload we are intercepting
        args_schema: contains args except that the DTensor args have been replaced
            with its DTensorSpec
        kwargs_schema: contains kwargs except that the DTensor kwargs have been replaced
            with its DTensorSpec
    """

    op: torch._ops.OpOverload
    args_schema: ArgsType
    kwargs_schema: KwargsType

    schema_info: Optional[RuntimeSchemaInfo] = None

    @property
    def args_spec(self) -> Tuple[DTensorSpec, ...]:
        """
        args_spec: Tuple[DTensorSpec, ...]: contains a clean list of args spec list
            with NO non-DTensor positional arguments (i.e. int/float/tuple, etc)
            mainly used by sharding propagation to propagate the output spec
        """
        # filter out non-relevant values from args schema to get a clean spec list
        # this would mainly be used by sharding propagation rules
        return tuple(item for item in self.args_schema if isinstance(item, DTensorSpec))

    def __repr__(self) -> str:
        return (
            f"OpSchema(op={self.op},"
            f" args_schema={self.args_schema},"
            f" kwargs_schema={self.kwargs_schema})"
        )

    def arg_type_tensor_or_tensor_list_like(self, arg_idx: int) -> bool:
        arg = self.args_schema[arg_idx]
        is_tensor = isinstance(arg, DTensorSpec)
        if is_tensor:
            return True

        if not isinstance(arg, list):
            return False

        return all(isinstance(e, DTensorSpec) or e is None for e in arg)

    def return_type_tuple_tensors(self) -> bool:
        return_types = self.op._schema.returns
        # all dispatch ops only return Tensor or Tuple[Tensor], so this check if enough
        return len(return_types) > 1 and isinstance(
            return_types[0].type, torch.TensorType
        )

    def __hash__(self) -> int:
        # Only hash args and kwargs that op indicates to hash
        if not self.schema_info:
            static_argnum = len(self.args_schema)
            static_kwargkey = None
        else:
            static_argnum = self.schema_info.static_argnum
            static_kwargkey = self.schema_info.static_kwargkey

        args_to_hash = tuple(
            tuple(e) if isinstance(e, list) else e
            for i, e in enumerate(self.args_schema)
            if self.arg_type_tensor_or_tensor_list_like(i) or i >= static_argnum
        )
        if static_kwargkey is not None:
            kwargs_to_hash = tuple(
                self.kwargs_schema.get(k, None) for k in static_kwargkey
            )
            return hash((self.op, args_to_hash, kwargs_to_hash))
        else:
            return hash((self.op, args_to_hash))

    def __eq__(self, other: object) -> bool:
        # early return checks
        if not isinstance(other, OpSchema):
            return False

        if self.op != other.op:
            return False

        if len(self.args_schema) != len(other.args_schema):
            return False

        # compare each element and early return if any of them is different
        if not self.schema_info:
            static_argnum = len(self.args_schema)
            static_kwargkey = None
        else:
            static_argnum = self.schema_info.static_argnum
            static_kwargkey = self.schema_info.static_kwargkey

        for i, (self_arg, other_arg) in enumerate(
            zip(self.args_schema, other.args_schema)
        ):
            if isinstance(self_arg, DTensorSpec) and self_arg != other_arg:
                return False
            elif i >= static_argnum and self_arg != other_arg:
                return False

        # check kwarg equality when there's a static kwarg key
        if static_kwargkey:
            for key in static_kwargkey:
                if self.kwargs_schema.get(key, None) != other.kwargs_schema.get(
                    key, None
                ):
                    return False

        return True

    def gen_fake_args(self) -> ArgsType:
        """
        gen_fake_args: generate fake args for the operator, this is mainly used
            by sharding propagation rules to generate fake args for the operator
            to run the local tensor operator and get the output spec.
        """
        return tree_map_only(
            DTensorSpec, _rebuild_tensor_from_dtensor_meta, self.args_schema
        )

    def gen_fake_kwargs(self) -> KwargsType:
        """
        gen_fake_kwargs: generate fake kwargs for the operator, this is mainly used
            by sharding propagation rules to generate fake kwargs for the operator
            to run the local tensor operator and get the output spec.
        """
        return tree_map_only(
            DTensorSpec, _rebuild_tensor_from_dtensor_meta, self.kwargs_schema
        )

    def _inplace_rewrap_schema_suggestion(self, origin_schema: "OpSchema") -> None:
        suggestion_args_spec = self.args_spec
        new_arg_schema: List[object] = []
        idx_of_args_spec = 0
        for arg in origin_schema.args_schema:
            if isinstance(arg, DTensorSpec):
                new_arg_schema.append(suggestion_args_spec[idx_of_args_spec])
                idx_of_args_spec += 1
            else:
                new_arg_schema.append(arg)
        self.args_schema = tuple(new_arg_schema)
        self.kwargs_schema = origin_schema.kwargs_schema


@dataclass
class OutputSharding:
    """
    OutputSharding is a data class that is used by the sharding propagation
    rules, it could set the output_spec upon successful propagation, and if
    it failed, output_spec would become None and sharding propagation rules
    could give a list of suggestions for inputs to reshard.

    NOTE: the schema_suggestion generated by sharding propagation should be
    exactly the same as the operator OpSchema, except the DTensor DTensorSpecs
    """

    output_spec: OutputSpecType
    schema_suggestions: Optional[List[OpSchema]] = None
    failed_reason: Optional[str] = None
    needs_redistribute: bool = False


@dataclass
class OpInfo:
    """
    All Runtime Op execution info are packed here
    """

    mesh: DeviceMesh
    schema: OpSchema
    flat_args_schema: List[object]
    flat_kwargs_schema: List[object]
    flat_local_args: List[object]
    flat_local_kwargs: List[object]
    args_tree_spec: TreeSpec
    kwargs_tree_spec: TreeSpec

    # the output sharding info
    output_sharding: Optional[OutputSharding] = None
