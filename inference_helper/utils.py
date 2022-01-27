from collections import Iterable
from torch.fx import Node

import torch
from dgl import DGLHeteroGraph


def inference_helper_getattr(obj, name: str):
    if name.isnumeric():
        return obj[int(name)]
    return getattr(obj, name)

def arg_transform(env, args):
    new_args = ()
    for arg in args:
        if isinstance(arg, Node):
            new_arg = env[arg.name]
        elif isinstance(arg, slice):
            new_arg = slice(
                arg.start if not isinstance(arg.start, Node) else env[arg.start.name],
                arg.step if not isinstance(arg.step, Node) else env[arg.step.name],
                arg.stop if not isinstance(arg.stop, Node) else env[arg.stop.name]
            )
        elif isinstance(arg, Iterable):
            new_arg = arg_transform(env, arg)
        else:
            new_arg = arg
        new_args += (new_arg,)
    return new_args

def arg_trace(args):
    ret = set()
    for arg in args:
        if isinstance(arg, Node):
            ret.add(arg.name)
        if isinstance(arg, slice):
            ret = ret.union(arg_trace((arg.start, arg.step, arg.stop)))
        if isinstance(arg, Iterable):
            ret = ret.union(arg_trace(arg))
    return ret

def get_new_arg_input(inputs, arg2val_map, input_nodes, inference_graph, device):
    new_args = ()
    for arg_node in inputs:
        if isinstance(arg2val_map[arg_node], torch.Tensor):
            new_args += (arg2val_map[arg_node][input_nodes].to(device),)
        elif isinstance(arg2val_map[arg_node], DGLHeteroGraph):
            new_args += (inference_graph.to(device),)
        elif hasattr(arg2val_map[arg_node], "to"):
            new_args += (arg2val_map[arg_node].to(device),)
        else:
            new_args += (arg2val_map[arg_node],)
    return new_args