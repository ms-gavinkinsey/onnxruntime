# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import copy
import itertools

import onnx
import sympy
from onnx import GraphProto, ModelProto, NodeProto, TensorProto, helper

from ._common import SymbolicDSU, TensorInfo, TypeAndShapeInfer
from ._decompose import DecomposeDispatch
from ._op_config import is_elementwise_node
from ._sympy_utils import parse_shape
from ._utils import get_attribute, to_torch_tensor, topological_sort


class SortedGraph:
    """
    This class is used to
        1. decompose complex operators into preliminary operators,
        2. sort the operators in topological order,
        3. infer the type and shape of each node inputs and outputs.

    input args:
        model: the ONNX model.
        input_shapes: the shapes of the model inputs. Can be numeric values or symbolic values.
    """

    def __init__(self, model: ModelProto, input_shapes: list[list[sympy.Expr]]):
        self._model: ModelProto = model
        self._graph: GraphProto = model.graph
        self._input_shapes: list[list[sympy.Expr]] = input_shapes

        # For elementwise graph outputs, when we group nodes to different kernels, if the target shape is different
        # from other nodes' target shape, even it can be broadcasted, we still need to create a new kernel for it.
        self._elementwise_graph_outputs: set[str] = set()
        graph_output_names = [output.name for output in self._graph.output]
        for node in self._graph.node:
            if is_elementwise_node(node):
                self._elementwise_graph_outputs.update(
                    [output for output in node.output if output in graph_output_names]
                )

        # Topological sort the nodes in the graph.
        self._sorted_nodes: list[NodeProto] = topological_sort(
            [input.name for input in self._graph.input] + [initializer.name for initializer in self._graph.initializer],
            self._graph.node,
        )

        self._node_arg_infos: dict[str, TensorInfo] = {}
        for idx, input in enumerate(self._graph.input):
            self._node_arg_infos[input.name] = TensorInfo(input.type.tensor_type.elem_type, self._input_shapes[idx])
        for initializer in self._graph.initializer:
            self._node_arg_infos[initializer.name] = TensorInfo(
                initializer.data_type,
                parse_shape(list(initializer.dims)),
            )

        # Decompose complex operators.
        self._decompose()

        # Sort the initializers in reference order.
        # We try to reuse Triton module for different ONNX models with same graph structure,
        # even the node args names in the models are different.
        # Sorting the initializers can help to generate same model key for different ONNX models.
        initializers = {}
        for initializer in self._graph.initializer:
            initializers[initializer.name] = initializer
        self._sorted_initializers: list[TensorProto] = []
        for node in self._sorted_nodes:
            for input in node.input:
                if input in initializers:
                    self._sorted_initializers.append(initializers[input])
                    initializers.pop(input)

        # Split nodes to constant nodes and non-constant nodes.
        self._const_nodes: list[NodeProto] = [node for node in self._sorted_nodes if node.op_type == "Constant"]
        self._sorted_nodes: list[NodeProto] = [node for node in self._sorted_nodes if node.op_type != "Constant"]

    def __str__(self):
        """
        Generate a unique key for the model based on the graph structure, ignoring the node args names.
        We try to reuse Triton module for different ONNX models with same graph structure.
        """
        graph_inputs = []
        name_map = {}
        for idx, input in enumerate(self._graph.input):
            shape_str = str(self._input_shapes[idx]).replace(" ", "")
            graph_inputs.append(f"({input.type.tensor_type.elem_type!s},{shape_str})")
            name_map[input.name] = f"i{idx}"
        graph_inputs_str = ",".join(graph_inputs)

        constants = []
        for idx, initializer in enumerate(self._sorted_initializers):
            data_str = str(to_torch_tensor(initializer).tolist()).replace("\n", "").replace(" ", "")
            constants.append(f"({initializer.data_type},{data_str})")
            name_map[initializer.name] = f"c{idx}"

        for idx, node in enumerate(self._const_nodes):
            value_attr = get_attribute(node, "value")
            data_str = str(to_torch_tensor(value_attr).tolist()).replace("\n", "").replace(" ", "")
            constants.append(f"({value_attr.data_type},{data_str})")
            name_map[node.output[0]] = f"c{idx + len(self._sorted_initializers)}"
        constants_str = ",".join(constants)

        for idx, output in enumerate(self._graph.output):
            name_map[output.name] = f"o{idx}"

        nodes = []
        for node_idx, node in enumerate(self._sorted_nodes):
            inputs = []
            for input in node.input:
                inputs.append(name_map.get(input, input))
            inputs_str = ",".join(inputs)
            outputs = []
            for idx, output in enumerate(node.output):
                if output in name_map:
                    outputs.append(name_map[output])
                else:
                    name_map[output] = f"t{node_idx}_{idx}"
                    outputs.append(name_map[output])
            outputs_str = ",".join(outputs)
            attributes = []
            for attr in node.attribute:
                fields = [str(f[1]) for f in attr.ListFields()]
                attributes.append(f"{fields[0]}:{fields[2]}={fields[1]}")
            attributes_str = ",".join(attributes)
            nodes.append(f"{node.op_type}[{attributes_str}]({inputs_str})->({outputs_str})")
        nodes_str = ",".join(nodes)
        return f"{graph_inputs_str}|{len(self._graph.output)!s}|{constants_str}|{nodes_str}"

    def __hash__(self):
        return hash(str(self))

    def __eq__(self, other):
        return str(self) == str(other)

    @property
    def const_nodes(self) -> list[NodeProto]:
        return self._const_nodes

    @property
    def sorted_nodes(self) -> list[NodeProto]:
        return self._sorted_nodes

    @property
    def original_graph(self) -> GraphProto:
        return self._graph

    @property
    def node_arg_infos(self) -> dict[str, TensorInfo]:
        return self._node_arg_infos

    @property
    def elementwise_graph_outputs(self) -> set[str]:
        return self._elementwise_graph_outputs

    def _decompose(self):
        dispatch = DecomposeDispatch()
        symbolics: SymbolicDSU = SymbolicDSU()
        pos = 0
        # If a node is complex, decompose it and insert the decomposed nodes at the same position.
        # All complex Ops are defined in DecomposeDispatch.
        # It's possible that the decomposed nodes are also complex, so we need to do the decompose recursively.
        # For example, decomposed nodes for "Softmax" contains "ReduceMean",
        # which will be decomposed to "ReduceSum" and "Div" further.
        while pos < len(self._sorted_nodes):
            node = self._sorted_nodes[pos]
            if node in dispatch:
                new_nodes = dispatch(node, self._graph, node_arg_infos=self._node_arg_infos)
                if len(new_nodes) != 1 or new_nodes[0] != node:
                    new_nodes = topological_sort(node.input, new_nodes)
                    self._sorted_nodes[pos : pos + 1] = new_nodes
                    continue
            if node.op_type == "Constant":
                value_attr = get_attribute(node, "value")
                self._node_arg_infos[node.output[0]] = TensorInfo(
                    value_attr.data_type,
                    parse_shape(list(value_attr.dims)),
                )
            else:
                input_infos = []
                for input in node.input:
                    input_infos.append(self._node_arg_infos[input])
                output_infos = TypeAndShapeInfer.infer(node, input_infos, self._graph, symbolics)
                for idx, output in enumerate(node.output):
                    self._node_arg_infos[output] = output_infos[idx]
            pos += 1
        for tensor_info in self._node_arg_infos.values():
            tensor_info.update_shape(symbolics)

    # Save the ONNX graphs for debug purpose. The original ONNX graph is the subgraph from backend.
    # The processed ONNX graph is the subgraph after decompose, it also contains the concrete shapes for each arg.
    def save_onnx(self, file_path_prefix):
        onnx.save(self._model, file_path_prefix + "_original.onnx")
        processed_model = copy.deepcopy(self._model)
        processed_model.graph.ClearField("node")
        processed_model.graph.node.extend(self.const_nodes)
        processed_model.graph.node.extend(self.sorted_nodes)
        for node in itertools.chain(processed_model.graph.input, processed_model.graph.output):
            node.type.tensor_type.shape.Clear()
            for dim in self.node_arg_infos[node.name].shape:
                if dim.is_number:
                    node.type.tensor_type.shape.dim.add().dim_value = int(dim)
                else:
                    node.type.tensor_type.shape.dim.add().dim_param = str(dim)
        value_infos = []
        for node in itertools.chain(self.const_nodes, self.sorted_nodes):
            for output in node.output:
                tensor_info = self.node_arg_infos[output]
                value_infos.append(
                    helper.make_tensor_value_info(
                        output,
                        tensor_info.dtype,
                        [int(dim) if dim.is_number else str(dim) for dim in tensor_info.shape],
                    )
                )
        processed_model.graph.ClearField("value_info")
        processed_model.graph.value_info.extend(value_infos)
        onnx.save(processed_model, file_path_prefix + "_processed.onnx")
