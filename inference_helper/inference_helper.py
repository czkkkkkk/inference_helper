import dgl
import torch
import torch.nn as nn
import tqdm

from .dglfx import CostEvaluater
from .auto_turnner import AutoTunner
from .function_generator import FunctionGenerator
from .custom_dataloader import CustomDataset
from .utils import get_new_arg_input, update_ret_output


class InferenceHelperBase():
    def __init__(self, module: nn.Module, device, debug = False):
        # add a '_' in order not crash with the origin one.
        self._device = device
        self._function_generator = FunctionGenerator(module, debug)
        self._traced = self._function_generator.traced
        self._schema = self._function_generator.get_schema()
        self._funcs = self._function_generator.get_funcs()

    def _trace_output_shape(self, arg2val_map):
        ret_shapes = [[] for _ in range(self._schema.layers_count)]
        for layer, func in zip(self._schema.layers, self._funcs):
            fake_graph = dgl.graph((torch.tensor([0]), torch.tensor([0])))
            device = self._device if not isinstance(self._device, list) else self._device[0]
            new_args = get_new_arg_input(layer.inputs, arg2val_map, [0], fake_graph, device)
            output_vals = func(*new_args)
            if not isinstance(output_vals, tuple):
                output_vals = (output_vals,)
            if len(output_vals) != len(layer.outputs):
                raise Exception("output values not match with layer's output.")
            for val, arg_node in zip(output_vals, layer.outputs):
                if isinstance(val, torch.Tensor):
                    arg2val_map[arg_node] = val.cpu()
                    ret_shapes[layer.id].append((torch.Tensor, val.size()[1:]))
                else:
                    ret_shapes[layer.id].append((val.__class__, None))
        return ret_shapes

    def compute(self, inference_graph, rets, arg2val_map, layer, func):
        raise NotImplementedError()

    def before_inference(self, graph, *args):
        evaluater = CostEvaluater(self._traced)
        first_layer_inputs = (graph,) + tuple(args)
        evaluater.eval(*first_layer_inputs)
        pass

    def inference(self, inference_graph, *args):
        self.before_inference(inference_graph, *args)
        first_layer_inputs = (inference_graph,) + tuple(args)
        if len(first_layer_inputs) != len(self._schema.first_layer_input):
            raise Exception("layer's input not match with args.")
        arg2val_map = {}
        for val, arg_name in zip(first_layer_inputs, self._schema.first_layer_input):
            arg_node = self._schema.name2arg_map[arg_name]
            arg2val_map[arg_node] = val
        ret_shapes = self._trace_output_shape(arg2val_map)

        for layer, func in zip(self._schema.layers, self._funcs):

            rets = []
            for j, _ in enumerate(layer.outputs):
                cls, shape = ret_shapes[layer.id][j]
                if cls == torch.Tensor:
                    rets.append(
                        torch.zeros((inference_graph.number_of_nodes(),) + tuple(shape))
                    )
                else:
                    rets.append(None)

            rets = self.compute(inference_graph, rets, arg2val_map, layer, func)

            # delete intermediate val
            for arg_node in layer.inputs:
                if arg_node.input_layers[-1] == layer and arg_node.input_layers[0] != self._schema.get_layer(0):
                    del arg2val_map[arg_node]

            for ret, arg_node in zip(rets, layer.outputs):
                arg2val_map[arg_node] = ret

        outputs = ()
        for name in self._schema.last_layer_output:
            arg_node = self._schema.name2arg_map[name]
            outputs += (arg2val_map[arg_node],)
        if len(outputs) == 1:
            return outputs[0]
        return tuple(outputs)


class InferenceHelper(InferenceHelperBase):
    def __init__(self, module: nn.Module, batch_size, device, num_workers = 4, debug = False):
        super().__init__(module, device, debug)
        self._batch_size = batch_size
        self._num_workers = num_workers

    def compute(self, graph, rets, arg2val_map, layer, func):
        sampler = dgl.dataloading.MultiLayerFullNeighborSampler(1)
        dataloader = dgl.dataloading.NodeDataLoader(
            graph,
            torch.arange(graph.number_of_nodes()).to(graph.device),
            sampler,
            batch_size=self._batch_size,
            device=self._device if self._num_workers == 0 else 'cpu',
            shuffle=False,
            drop_last=False,
            num_workers=self._num_workers)

        for input_nodes, output_nodes, blocks in tqdm.tqdm(dataloader):
            new_args = get_new_arg_input(layer.inputs, arg2val_map, input_nodes, blocks[0], self._device)

            output_vals = func(*new_args)
            del new_args

            rets = update_ret_output(output_vals, rets, input_nodes, output_nodes, blocks)

        return rets


class EdgeControlInferenceHelper(InferenceHelperBase):
    def __init__(self, module: nn.Module, max_edge_in_batch, device, num_workers = 4, debug = False):
        super().__init__(module, device, debug)
        self._max_edge_in_batch = max_edge_in_batch
        self._num_workers = num_workers

    def compute(self, graph, rets, arg2val_map, layer, func):
        sampler = dgl.dataloading.MultiLayerFullNeighborSampler(1)
        nids = torch.arange(graph.number_of_nodes()).to(graph.device)
        custom_dataset = CustomDataset(self._max_edge_in_batch, graph, nids)
        dataloader = dgl.dataloading.NodeDataLoader(
            graph,
            custom_dataset,
            sampler,
            device=self._device if self._num_workers == 0 else 'cpu',
            shuffle=False,
            drop_last=False,
            num_workers=self._num_workers)

        for input_nodes, output_nodes, blocks in tqdm.tqdm(dataloader):
            new_args = get_new_arg_input(layer.inputs, arg2val_map, input_nodes, blocks[0], self._device)

            output_vals = func(*new_args)
            del new_args

            rets = update_ret_output(output_vals, rets, input_nodes, output_nodes, blocks)

        return rets


class AutoInferenceHelper(InferenceHelperBase):
    def __init__(self, module: nn.Module, device, num_workers = 4, debug = False):
        super().__init__(module, device, debug)
        self._num_workers = num_workers
        self.auto_tunner = AutoTunner(self._device)

    def compute(self, graph, rets, arg2val_map, layer, func):
        max_edge_in_batch = self.auto_tunner.search(graph, arg2val_map, layer, func)
        sampler = dgl.dataloading.MultiLayerFullNeighborSampler(1)
        nids = torch.arange(graph.number_of_nodes()).to(graph.device)
        custom_dataset = CustomDataset(max_edge_in_batch, graph, nids)
        dataloader = dgl.dataloading.NodeDataLoader(
            graph,
            custom_dataset,
            sampler,
            device=self._device if self._num_workers == 0 else 'cpu',
            shuffle=False,
            drop_last=False,
            num_workers=self._num_workers)

        for input_nodes, output_nodes, blocks in tqdm.tqdm(dataloader):
            new_args = get_new_arg_input(layer.inputs, arg2val_map, input_nodes, blocks[0], self._device)

            output_vals = func(*new_args)
            del new_args

            rets = update_ret_output(output_vals, rets, input_nodes, output_nodes, blocks)

        return rets
