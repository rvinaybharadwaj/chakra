#!/usr/bin/env python3

import bisect
import copy
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from chakra.third_party.utils.protolib import encodeMessage as encode_message
from chakra.et_converter.pytorch_node import PyTorchNodeType, PyTorchNode
from chakra.et_def.et_def_pb2 import (
    GlobalMetadata,
    Node as ChakraNode,
    AttributeProto as ChakraAttr,
    INVALID_NODE,
    COMP_NODE,
    COMM_COLL_NODE,
    ALL_REDUCE,
    ALL_GATHER,
    BROADCAST,
    ALL_TO_ALL,
    REDUCE_SCATTER,
)


class UniqueIdAssigner:
    """
    Class for assigning unique IDs. Generates a new unique ID for each call,
    even with the same original ID, and keeps track of all assigned IDs.

    Attributes:
        next_id (int): The next available unique ID.
        original_to_assigned_ids (Dict[int, List[int]]): Mapping from original
            IDs to lists of assigned unique IDs.
    """

    def __init__(self) -> None:
        self.next_id = 0
        self.original_to_assigned_ids: Dict[int, List[int]] = {}

    def set_next_id(self, next_id: int) -> None:
        """
        Sets the starting next unique ID.

        Args:
            next_id (int): The starting next unique ID to set.
        """
        self.next_id = next_id

    def assign_unique_id(self, original_id: int) -> int:
        """
        Generates and tracks a new unique ID for each call for a given original ID.

        Args:
            original_id (int): The original ID to generate a unique ID for.

        Returns:
            int: A new unique ID for the original ID.
        """
        unique_id = self.next_id
        self.next_id += 1

        assigned_ids = self.original_to_assigned_ids.setdefault(original_id, [])
        assigned_ids.append(unique_id)

        return unique_id

    def get_assigned_ids(self, original_id: int) -> List[int]:
        """
        Retrieves all unique IDs assigned to a given original ID.

        Args:
            original_id (int): The original ID to retrieve unique IDs for.

        Returns:
            List[int]: List of unique IDs assigned to the original ID.
        """
        return self.original_to_assigned_ids.get(original_id, [])


class PyTorch2ChakraConverter:
    """
    Converter class for transforming PyTorch execution traces into Chakra format.

    This class is responsible for converting the execution traces collected
    from PyTorch into a format that is compatible with Chakra, a performance
    analysis tool. It handles the intricate mappings and transformations
    required to accurately represent the execution in a different format.

    Attributes:
        input_filename (str): Input file name containing PyTorch execution trace.
        output_filename (str): Output file name for the converted Chakra trace.
        num_dims (int): Number of dimensions involved in the conversion process.
        logger (logging.Logger): Logger for logging information during conversion.
        id_assigner (UniqueIdAssigner): Object to manage unique ID assignments.
        pytorch_schema (Optional[str]): Schema info of the PyTorch trace.
        pytorch_pid (Optional[int]): Process ID associated with the PyTorch trace.
        pytorch_time (Optional[str]): Time info of the PyTorch trace.
        pytorch_start_ts (Optional[int]): Start timestamp of the PyTorch trace.
        pytorch_finish_ts (Optional[int]): Finish timestamp of the PyTorch trace.
        pytorch_nodes (Dict[int, Any]): Map of PyTorch node IDs to nodes.
        pytorch_root_nids (List[int]): List of root node IDs in the PyTorch trace.
        pytorch_cpu_node_id_gpu_node_map (Dict[int, List[int]]): Map of PyTorch
            CPU node IDs to GPU node IDs.
        chakra_nodes (Dict[int, Any]): Map of Chakra node IDs to nodes.
    """

    def __init__(
        self,
        input_filename: str,
        output_filename: str,
        num_dims: int,
        logger: logging.Logger
    ) -> None:
        """
        Initializes the PyTorch to Chakra converter. It sets up necessary
        attributes and prepares the environment for the conversion process.

        Args:
            input_filename (str): Name of the input file containing PyTorch execution trace.
            output_filename (str): Name of the output file for the converted Chakra trace.
            num_dims (int): Number of dimensions involved in the conversion process.
            logger (logging.Logger): Logger for logging information during the conversion.
        """
        self.input_filename = input_filename
        self.output_filename = output_filename
        self.num_dims = num_dims
        self.logger = logger
        self.id_assigner = UniqueIdAssigner()
        self.initialize_attributes()

    def initialize_attributes(self) -> None:
        # Initialize file and trace-related attributes
        self.pytorch_schema = None
        self.pytorch_pid = None
        self.pytorch_time = None
        self.pytorch_start_ts = None
        self.pytorch_finish_ts = None
        self.pytorch_nodes = None
        self.pytorch_root_nids = []

        # Initialize node mapping dictionaries
        self.pytorch_cpu_node_id_gpu_node_map = {}
        self.chakra_nodes = {}

    def convert(self) -> None:
        """
        Converts PyTorch execution traces into the Chakra format. Orchestrates
        the conversion process including trace loading, trace opening, phase
        end node construction, node splitting, and node conversion.
        """
        self.load_pytorch_execution_traces()

        self.open_chakra_execution_trace()

        self.split_cpu_nodes_with_gpu_child()

        for pytorch_nid, pytorch_node in self.pytorch_nodes.items():
            if (pytorch_node.get_op_type() == PyTorchNodeType.CPU_OP)\
            or (pytorch_node.get_op_type() == PyTorchNodeType.LABEL):
                chakra_node = self.convert_to_chakra_node(pytorch_node)
                self.chakra_nodes[chakra_node.id] = chakra_node

                if pytorch_node.child_gpu:
                    pytorch_gpu_node = pytorch_node.child_gpu
                    chakra_gpu_node = self.convert_to_chakra_node(pytorch_gpu_node)

                    if chakra_node.type == COMM_COLL_NODE:
                        chakra_gpu_node.attr.extend([
                            ChakraAttr(name="comm_type",
                                       int64_val=pytorch_gpu_node.collective_comm_type),
                            ChakraAttr(name="comm_size",
                                       int64_val=pytorch_gpu_node.comm_size),
                            ChakraAttr(name="involved_dim",
                                       bool_list={"values": [True]*self.num_dims})
                        ])

                    self.chakra_nodes[chakra_gpu_node.id] = chakra_gpu_node

        root_nodes = [node for node in self.chakra_nodes.values() if self.is_root_node(node)]
        for root_node in root_nodes:
            self.convert_ctrl_dep_to_data_dep(root_node)

        self.remove_dangling_nodes()

        self.identify_cyclic_dependencies()

        self.write_chakra_et()

        self.close_chakra_execution_trace()

    def load_pytorch_execution_traces(self) -> None:
        """
        Loads PyTorch execution traces from a file.

        Reads and parses the PyTorch execution trace data from a file, creating
        PyTorchNode objects and establishing node relationships.

        Raises:
            Exception: If there is an IOError in opening the file.
        """
        self.logger.info("Loading PyTorch execution traces from file.")
        try:
            with open(self.input_filename, "r") as pytorch_et:
                pytorch_et_data = json.load(pytorch_et)
            self._parse_and_instantiate_nodes(pytorch_et_data)
            self.id_assigner.set_next_id(max(self.pytorch_nodes.keys()) + 1)
        except IOError as e:
            self.logger.error(f"Error opening file {self.input_filename}: {e}")
            raise Exception(f"Could not open file {self.input_filename}")

    def _parse_and_instantiate_nodes(self, pytorch_et_data: Dict) -> None:
        """
        Parses and instantiates PyTorch nodes from execution trace data.

        Args:
            pytorch_et_data (Dict): The execution trace data.

        Extracts node information, sorts nodes by timestamp, and establishes
        parent-child relationships among them.
        """
        self.logger.info("Extracting and processing node data from execution trace.")
        self.pytorch_schema = pytorch_et_data["schema"]
        self.pytorch_pid = pytorch_et_data["pid"]
        self.pytorch_time = pytorch_et_data["time"]
        self.pytorch_start_ts = pytorch_et_data["start_ts"]
        self.pytorch_finish_ts = pytorch_et_data["finish_ts"]

        pytorch_nodes = pytorch_et_data["nodes"]
        pytorch_node_objects = {
            node_data["id"]: PyTorchNode(node_data) for node_data in pytorch_nodes
        }
        self._establish_parent_child_relationships(pytorch_node_objects)

    def _establish_parent_child_relationships(
        self, pytorch_node_objects: Dict[int, PyTorchNode]
    ) -> None:
        """
        Establishes parent-child relationships among PyTorch nodes and counts
        the node types.

        Args:
            pytorch_node_objects (Dict[int, PyTorchNode]): Dictionary of PyTorch
            node objects.
        """
        # Initialize counters for different types of nodes
        node_type_counts = {
            "total_op": 0,
            "cpu_op": 0,
            "gpu_op": 0,
            "record_param_comms_op": 0,
            "nccl_op": 0,
            "root_op": 0
        }

        # Establish parent-child relationships
        for pytorch_node in pytorch_node_objects.values():
            parent_id = pytorch_node.parent
            if parent_id in pytorch_node_objects:
                parent_node = pytorch_node_objects[parent_id]
                parent_node.add_child(pytorch_node)

                if pytorch_node.is_gpu_op():
                    parent_node.set_child_gpu(pytorch_node)

                if pytorch_node.is_record_param_comms_op():
                    parent_node.record_param_comms_node = pytorch_node

                if pytorch_node.is_nccl_op():
                    parent_node.nccl_node = pytorch_node

            if pytorch_node.name in ["[pytorch|profiler|execution_graph|thread]",
                                     "[pytorch|profiler|execution_trace|thread]"]:
                self.pytorch_root_nids.append(pytorch_node.id)
                node_type_counts["root_op"] += 1

            # Collect statistics
            node_type_counts["total_op"] += 1
            if pytorch_node.is_cpu_op():
                node_type_counts["cpu_op"] += 1
            if pytorch_node.is_gpu_op():
                node_type_counts["gpu_op"] += 1
            if pytorch_node.is_record_param_comms_op():
                node_type_counts["record_param_comms_op"] += 1
            if pytorch_node.is_nccl_op():
                node_type_counts["nccl_op"] += 1

        # Log the counts of each node type
        for node_type, count in node_type_counts.items():
            self.logger.info(f"{node_type}: {count}")

        self.pytorch_nodes = pytorch_node_objects

    def open_chakra_execution_trace(self) -> None:
        """
        Opens the Chakra execution trace file for writing.

        Raises:
            Exception: If there is an IOError in opening the file.
        """
        self.logger.info(f"Opening Chakra execution trace file: {self.output_filename}")
        try:
            self.chakra_et = open(self.output_filename, "wb")
        except IOError as e:
            err_msg = f"Error opening file {self.output_filename}: {e}"
            self.logger.error(err_msg)
            raise Exception(err_msg)

    def split_cpu_nodes_with_gpu_child(self) -> None:
        """
        Decomposes CPU nodes with GPU child nodes to model execution overlap
        accurately. This method addresses scenarios where a CPU node has a GPU
        child node, with an overlap in their execution ending at the same time.
        The method splits the CPU node into:
        1. Non-Overlapping Part: Segment before the GPU node starts.
        2. Overlapping Part: Segment overlapping with the GPU node.

        Timeline Stages:
        Stage 1 - Original Scenario:
            |------------ CPU Node ------------|
                              |--- GPU Node ---|

        Stage 2 - After Split:
            |-- Non-Overlap --|--- Overlap ----|
                              |--- GPU Node ---|

        Raises:
            ValueError: If timestamps of GPU and CPU nodes are inconsistent.
        """
        self.logger.info("Decomposing CPU nodes with GPU child nodes.")
        updated_pytorch_nodes: Dict[int, PyTorchNode] = {}
        for cpu_node in self.pytorch_nodes.values():
            if cpu_node.child_gpu is None:
                new_cpu_node_id = self.id_assigner.assign_unique_id(cpu_node.id)
                cpu_node.id = new_cpu_node_id
                for child_node in cpu_node.children:
                    child_node.parent = cpu_node.id
                updated_pytorch_nodes[new_cpu_node_id] = cpu_node
            else:
                gpu_node = cpu_node.child_gpu
                cpu_node_first, cpu_node_second, updated_gpu_node =\
                        self._split_cpu_node(cpu_node, gpu_node, updated_pytorch_nodes)
                updated_pytorch_nodes[cpu_node_first.id] = copy.deepcopy(cpu_node_first)
                updated_pytorch_nodes[cpu_node_second.id] = copy.deepcopy(cpu_node_second)
                updated_pytorch_nodes[updated_gpu_node.id] = copy.deepcopy(updated_gpu_node)

        self.pytorch_nodes = updated_pytorch_nodes

    def _split_cpu_node(
        self, cpu_node: PyTorchNode, gpu_node: PyTorchNode,
        updated_pytorch_nodes: Dict[int, PyTorchNode]
    ) -> Tuple[PyTorchNode, PyTorchNode, PyTorchNode]:
        """
        Splits a CPU node based on the GPU node's timestamp.

        Args:
            cpu_node (PyTorchNode): Original CPU node to be split.
            gpu_node (PyTorchNode): GPU node dictating the split.
            updated_pytorch_nodes (Dict[int, PyTorchNode]): Updated PyTorch nodes.

        Returns:
            Tuple[PyTorchNode, PyTorchNode, PyTorchNode]: Two split nodes and
            the updated GPU node.

        Raises:
            ValueError: For inconsistencies in the timestamps of the nodes.
        """
        original_cpu_info = f"Original CPU Node ID {cpu_node.id} ({cpu_node.name}), " \
                            f"Duration: {cpu_node.dur}."
        self.logger.debug(original_cpu_info)
        self.logger.debug(f"GPU Node ID {gpu_node.id} ({gpu_node.name}), "
                          f"Duration: {gpu_node.dur}.")

        cpu_node_first = copy.deepcopy(cpu_node)
        cpu_node_first.id = self.id_assigner.assign_unique_id(cpu_node.id)
        cpu_node_first.ts = cpu_node.ts
        cpu_node_first.dur = gpu_node.ts - cpu_node.ts
        cpu_node_first.set_child_gpu(gpu_node)
        if cpu_node_first.ts >= gpu_node.ts or cpu_node_first.dur <= 0:
            err_msg = (f"Invalid timestamps for the first split CPU node derived from {original_cpu_info}\n"
                       f"\tFirst Split CPU Node Timestamp: {cpu_node_first.ts}, \n"
                       f"\tGPU Node Timestamp: {gpu_node.ts}, \n"
                       f"\tFirst Split CPU Node Duration: {cpu_node_first.dur}.")
            self.logger.error(err_msg)
            raise ValueError(err_msg)

        if cpu_node.parent in self.pytorch_nodes:
            self._update_parent_node_children(self.pytorch_nodes, cpu_node, cpu_node_first)
        elif cpu_node.parent in updated_pytorch_nodes:
            self._update_parent_node_children(updated_pytorch_nodes, cpu_node, cpu_node_first)

        self.logger.debug(f"First Split CPU Node ID {cpu_node_first.id} ({cpu_node_first.name}), "
                          f"Duration: {cpu_node_first.dur}")

        gpu_node_id = self.id_assigner.assign_unique_id(gpu_node.id)
        gpu_node.id = gpu_node_id
        gpu_node.parent = cpu_node_first.id

        cpu_node_second = copy.deepcopy(cpu_node)
        cpu_node_second.id = self.id_assigner.assign_unique_id(cpu_node.id)
        cpu_node_second.ts = gpu_node.ts
        cpu_node_second.dur = cpu_node.dur - (gpu_node.ts - cpu_node.ts)
        cpu_node_second.set_child_gpu(None)
        cpu_node_second.parent = cpu_node_first.id
        for child_node in cpu_node.children:
            child_node.parent = cpu_node_second.id
            cpu_node_second.add_child(child_node)
        if cpu_node_second.ts <= cpu_node_first.ts or cpu_node_second.dur <= 0:
            err_msg = (f"Invalid timestamps for the second split CPU node derived from {original_cpu_info}\n"
                       f"\tFirst Split Timestamp: {cpu_node_first.ts}, \n"
                       f"\tSecond Split Timestamp: {cpu_node_second.ts}, \n"
                       f"\tSecond Split Duration: {cpu_node_second.dur}.")
            self.logger.error(err_msg)
            raise ValueError(err_msg)

        self.logger.debug(f"Second Split CPU Node ID {cpu_node_second.id} ({cpu_node_second.name}), "
                          f"Duration: {cpu_node_second.dur}.")

        cpu_node_first.add_child(cpu_node_second)
        cpu_node_first.add_child(gpu_node)

        return cpu_node_first, cpu_node_second, gpu_node

    def _update_parent_node_children(self, parent_node_dict: Dict[int, PyTorchNode],
                                     cpu_node: PyTorchNode,
                                     cpu_node_first: PyTorchNode) -> None:
        """
        Updates the children of the parent node in the given dictionary.

        This method removes the original CPU node from the parent's children list
        and adds the first split node.

        Args:
            parent_node_dict (Dict[int, PyTorchNode]): Dictionary containing the
            parent node.
            cpu_node (PyTorchNode): Original CPU node being split.
            cpu_node_first (PyTorchNode): First split node to add to the parent's
            children.
        """
        parent_node = parent_node_dict[cpu_node.parent]
        parent_node.children = [child for child in parent_node.children
                                if child.id != cpu_node.id]
        parent_node.children.extend([cpu_node_first])

    def convert_to_chakra_node(self, pytorch_node: PyTorchNode) -> ChakraNode:
        """
        Converts a PyTorchNode to a ChakraNode.

        Args:
            pytorch_node (PyTorchNode): The PyTorch node to convert.

        Returns:
            ChakraNode: The converted Chakra node.
        """
        self.logger.debug(f"Converting PyTorch node ID {pytorch_node.id} to Chakra node.")

        chakra_node = ChakraNode()
        chakra_node.id = pytorch_node.id
        chakra_node.name = pytorch_node.name
        chakra_node.type = self.get_chakra_node_type_from_pytorch_node(pytorch_node)
        if pytorch_node.parent in self.chakra_nodes:
            chakra_node.ctrl_deps.append(pytorch_node.parent)
        chakra_node.duration_micros = pytorch_node.dur if pytorch_node.has_dur() else 0
        chakra_node.inputs.values = str(pytorch_node.inputs)
        chakra_node.inputs.shapes = str(pytorch_node.input_shapes)
        chakra_node.inputs.types = str(pytorch_node.input_types)
        chakra_node.outputs.values = str(pytorch_node.outputs)
        chakra_node.outputs.shapes = str(pytorch_node.output_shapes)
        chakra_node.outputs.types = str(pytorch_node.output_types)
        chakra_node.attr.extend([
            ChakraAttr(name="rf_id", int64_val=pytorch_node.rf_id),
            ChakraAttr(name="fw_parent", int64_val=pytorch_node.fw_parent),
            ChakraAttr(name="seq_id", int64_val=pytorch_node.seq_id),
            ChakraAttr(name="scope", int64_val=pytorch_node.scope),
            ChakraAttr(name="tid", int64_val=pytorch_node.tid),
            ChakraAttr(name="fw_tid", int64_val=pytorch_node.fw_tid),
            ChakraAttr(name="op_schema", string_val=pytorch_node.op_schema),
            ChakraAttr(name="is_cpu_op", int32_val=not pytorch_node.is_gpu_op()),
            ChakraAttr(name="ts", int64_val=pytorch_node.ts)
        ])
        return chakra_node

    def get_chakra_node_type_from_pytorch_node(self, pytorch_node: PyTorchNode) -> int:
        """
        Determines the Chakra node type from a PyTorch node.

        Args:
            pytorch_node (PyTorchNode): The PyTorch node to determine the type of.

        Returns:
            int: The corresponding Chakra node type.
        """
        if pytorch_node.is_gpu_op() and (
            "ncclKernel" in pytorch_node.name or "ncclDevKernel" in pytorch_node.name
        ):
            return COMM_COLL_NODE
        elif pytorch_node.is_gpu_op():
            return COMP_NODE
        elif ("c10d::" in pytorch_node.name) or ("nccl:" in pytorch_node.name):
            return COMM_COLL_NODE
        elif (pytorch_node.op_schema != "") or pytorch_node.outputs:
            return COMP_NODE
        return INVALID_NODE

    def is_root_node(self, node):
        """
        Determines whether a given node is a root node in the execution trace.

        In the context of PyTorch execution traces, root nodes are the starting
        points of execution graphs or execution traces. These nodes typically do
        not have parent nodes and act as the original sources of execution flow.
        This method identifies such root nodes based on their names. Specifically,
        nodes with names indicating they are part of the PyTorch execution graph or
        execution trace threads are considered root nodes.

        Args:
            node (ChakraNode): The node to be evaluated.

        Returns:
            bool: True if the node is a root node, False otherwise.
        """
        if node.name in ["[pytorch|profiler|execution_graph|thread]",
                         "[pytorch|profiler|execution_trace|thread]"]:
            return True

    def convert_ctrl_dep_to_data_dep(self, chakra_node: ChakraNode) -> None:
        """
        Traverses nodes based on control dependencies (parent nodes) and encodes
        data dependencies appropriately. This method is crucial for converting the
        dependency structure from PyTorch execution traces to Chakra execution
        traces. In PyTorch traces, control dependencies are represented by a
        parent field in each node, denoting the parent node ID. This structure
        indicates which functions (operators) are called by a particular operator.

        In contrast, Chakra execution traces, while retaining control dependencies
        for compatibility, primarily rely on data dependencies to represent
        relationships between nodes. Data dependencies in Chakra are more broadly
        defined compared to those in PyTorch, where they are implicitly encoded in
        tensor input-output relationships. In Chakra, data dependencies are explicit
        and represent a general dependency between nodes.

        To convert PyTorch's control dependencies to Chakra's data dependencies, a
        Depth-First Search (DFS) is performed. The DFS traversal starts from a given
        Chakra node, traversing through its children (based on control
        dependencies). During traversal, data dependencies are encoded by linking
        nodes that have been visited in sequence. These dependencies form a chain,
        mirroring the function call order from the PyTorch trace.

        Special attention is given to the types of nodes involved. CPU and label
        nodes (non-GPU) in PyTorch can only depend on other CPU or label nodes.
        However, GPU nodes can depend on any type of node. Thus, while traversing,
        if a GPU node is encountered, it can establish a data dependency with the
        last visited node of any type. For CPU and label nodes, the dependency is
        only established with the last visited non-GPU node. This distinction
        ensures that the converted dependencies accurately reflect the execution
        dynamics of the original PyTorch trace within the Chakra framework.

        Args:
            chakra_node (ChakraNode): The starting node for the traversal and
            dependency processing.
        """
        visited = set()
        stack = [chakra_node]
        last_visited_non_gpu = None
        last_visited_any = None

        while stack:
            current_node = stack.pop()
            if current_node.id in visited:
                continue

            visited.add(current_node.id)

            # Determine the operator type of the current node
            pytorch_node = self.pytorch_nodes.get(current_node.id)
            if pytorch_node:
                node_op_type = pytorch_node.get_op_type()

                if node_op_type == PyTorchNodeType.GPU_OP:
                    # GPU operators can depend on any type of operator
                    if last_visited_any:
                        if last_visited_any.id not in current_node.data_deps:
                            current_node.data_deps.append(last_visited_any.id)
                            self.logger.debug(
                                f"GPU Node ID {current_node.id} now has a data "
                                f"dependency on Node ID {last_visited_any.id}"
                            )
                    last_visited_any = current_node
                else:
                    # CPU operators depend on non-GPU operators
                    if last_visited_non_gpu:
                        if last_visited_non_gpu.id not in current_node.data_deps:
                            current_node.data_deps.append(last_visited_non_gpu.id)
                            self.logger.debug(
                                f"CPU Node ID {current_node.id} now has a data "
                                f"dependency on non-GPU Node ID "
                                f"{last_visited_non_gpu.id}"
                            )
                    last_visited_non_gpu = current_node
                    last_visited_any = current_node

                # Add children to the stack
                children_chakra_ids = [child.id for child in pytorch_node.children]
                for child_chakra_id in sorted(children_chakra_ids, reverse=True):
                    child_chakra_node = self.chakra_nodes.get(child_chakra_id)
                    if child_chakra_node and child_chakra_node.id not in visited:
                        stack.append(child_chakra_node)

    def remove_dangling_nodes(self) -> None:
        """
        Removes any dangling nodes from the chakra_nodes dictionary.
        A node is considered dangling if it has no parents and no children.
        """
        parent_ids = set()
        for node in self.chakra_nodes.values():
            parent_ids.update(node.data_deps)

        dangling_nodes = []
        for node_id, node in list(self.chakra_nodes.items()):
            if node_id not in parent_ids and not node.data_deps:
                dangling_nodes.append(node)
                del self.chakra_nodes[node_id]
                del self.pytorch_nodes[node_id]

        if dangling_nodes:
            self.logger.info(f"Identified and removed {len(dangling_nodes)} dangling nodes:")
            for node in dangling_nodes:
                self.logger.info(f" - Node ID {node.id}: {node.name}")

    def identify_cyclic_dependencies(self) -> None:
        """
        Identifies if there are any cyclic dependencies among Chakra nodes.

        This method checks for cycles in the graph of Chakra nodes using a
        depth-first search (DFS) algorithm. It logs an error message and raises
        an exception if a cycle is detected, ensuring the graph is a Directed
        Acyclic Graph (DAG).

        Raises:
            Exception: If a cyclic dependency is detected among the Chakra nodes.
        """
        visited = set()
        stack = set()

        def dfs(node_id: int, path: List[int]) -> bool:
            """
            Depth-first search to detect cycles.

            Args:
                node_id (int): The node ID to start the DFS from.
                path (List[int]): The path traversed so far, for tracing the cycle.

            Returns:
                bool: True if a cycle is detected, False otherwise.
            """
            if node_id in stack:
                cycle_nodes = " -> ".join(
                    [self.chakra_nodes[n].name for n in path + [node_id]]
                )
                self.logger.error(f"Cyclic dependency detected: {cycle_nodes}")
                return True
            if node_id in visited:
                return False

            visited.add(node_id)
            stack.add(node_id)
            path.append(node_id)
            for child_id in self.chakra_nodes[node_id].data_deps:
                if dfs(child_id, path.copy()):
                    return True
            stack.remove(node_id)
            path.pop()
            return False

        for node_id in self.chakra_nodes:
            if dfs(node_id, []):
                raise Exception(
                    f"Cyclic dependency detected starting from node "
                    f"{self.chakra_nodes[node_id].name}"
                )

    def write_chakra_et(self) -> None:
        """
        Writes the Chakra execution trace by encoding global metadata and nodes.

        Encodes and writes both the metadata and individual nodes to create a
        complete execution trace.
        """
        self.logger.info("Writing Chakra execution trace.")
        self._write_global_metadata()
        self._encode_and_write_nodes()
        self.logger.info("Chakra execution trace writing completed.")

    def _write_global_metadata(self) -> None:
        """
        Encodes and writes global metadata for the Chakra execution trace.

        This process includes encoding metadata like schema, process ID, timestamps,
        and other relevant information for the Chakra execution trace.
        """
        self.logger.info("Encoding global metadata for Chakra execution trace.")
        global_metadata = GlobalMetadata(
            attr=[
                ChakraAttr(name="schema", string_val=self.pytorch_schema),
                ChakraAttr(name="pid", uint64_val=self.pytorch_pid),
                ChakraAttr(name="time", string_val=self.pytorch_time),
                ChakraAttr(name="start_ts", uint64_val=self.pytorch_start_ts),
                ChakraAttr(name="finish_ts", uint64_val=self.pytorch_finish_ts)
            ]
        )
        encode_message(self.chakra_et, global_metadata)

    def _encode_and_write_nodes(self) -> None:
        """
        Encodes and writes nodes for the Chakra execution trace.

        Each node from the PyTorch execution trace is encoded and written into the
        Chakra format. This includes node IDs, names, types, dependencies, and
        other attributes.
        """
        self.logger.info("Encoding and writing nodes for Chakra execution trace.")
        seen_nids = set()
        for nid in sorted(self.chakra_nodes.keys()):
            if nid in seen_nids:
                err_msg = f"Duplicate NID {nid} detected in Chakra nodes."
                self.logger.error(err_msg)
                raise ValueError(err_msg)
            seen_nids.add(nid)
            chakra_node = self.chakra_nodes[nid]
            encode_message(self.chakra_et, chakra_node)

    def close_chakra_execution_trace(self) -> None:
        """
        Closes the Chakra execution trace file if it is open.

        Ensures proper closure of the trace file to preserve data integrity.
        """
        self.logger.info("Closing Chakra execution trace file.")
        if self.chakra_et and not self.chakra_et.closed:
            self.chakra_et.close()
