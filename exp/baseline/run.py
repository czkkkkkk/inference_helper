import torch
import torch.nn as nn
import torch.nn.functional as F
import dgl
import numpy as np
import time
import tqdm
import argparse
from exp_model.gcn import StochasticTwoLayerGCN
from exp_model.sage import SAGE
from exp_model.gat import  GAT
from exp_model.jknet import JKNet
from dgl.data import CiteseerGraphDataset, RedditDataset
from inference_helper import InferenceHelper, EdgeControlInferenceHelper, AutoInferenceHelper

def load_reddit():
    from dgl.data import RedditDataset
    data = RedditDataset(self_loop=True)
    g = data[0]
    g.ndata['features'] = g.ndata['feat']
    return g, data.num_classes

def load_ogb(name):
    st = time.time()
    from ogb.nodeproppred import DglNodePropPredDataset
    data = DglNodePropPredDataset(name=name)
    splitted_idx = data.get_idx_split()
    graph, labels = data[0]
    graph = dgl.add_self_loop(graph)
    labels = labels[:, 0]

    print(graph)
    if name == "ogbn-papers100M":
        print(time.time()-st)
    graph.ndata['features'] = graph.ndata['feat']
    graph.ndata['label'] = labels
    in_feats = graph.ndata['features'].shape[1]
    num_labels = len(torch.unique(labels[torch.logical_not(torch.isnan(labels))]))

    # Find the node IDs in the training, validation, and test set.
    train_nid, val_nid, test_nid = splitted_idx['train'], splitted_idx['valid'], splitted_idx['test']
    train_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    train_mask[train_nid] = True
    val_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    val_mask[val_nid] = True
    test_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    test_mask[test_nid] = True
    graph.ndata['train_mask'] = train_mask
    graph.ndata['val_mask'] = val_mask
    graph.ndata['test_mask'] = test_mask
    return graph, num_labels

def train(args):
    if args.dataset == "reddit":
        dataset = load_reddit()
    else:
        dataset = load_ogb(args.dataset)
    # dataset = load_reddit()
    g : dgl.DGLHeteroGraph = dataset[0]
    train_mask = g.ndata['train_mask']
    val_mask = g.ndata['val_mask']
    test_mask = g.ndata['test_mask']
    feat = g.ndata['feat']
    labels = g.ndata['label']
    num_classes = dataset[1]
    in_feats = feat.shape[1]
    train_nid = torch.nonzero(train_mask, as_tuple=True)[0]
    hidden_feature = args.num_hidden

    sampler = dgl.dataloading.MultiLayerNeighborSampler([10, 25, 50])
    dataloader = dgl.dataloading.NodeDataLoader(
        g, train_nid, sampler,
        batch_size=2000,
        shuffle=True,
        drop_last=False,
        num_workers=4)

    if args.model == "GCN":
        model = StochasticTwoLayerGCN(args.num_layers, in_feats, hidden_feature, num_classes)
    elif args.model == "SAGE":
        model = SAGE(in_feats, hidden_feature, num_classes, args.num_layers, F.relu, 0.5)
    elif args.model == "GAT":
        model = GAT(args.num_layers, in_feats, hidden_feature, num_classes, [args.num_heads for _ in range(args.num_layers)], F.relu, 0.5, 0.5, 0.5, 0.5)
    elif args.model == "JKNET":
        model = JKNet(in_feats, hidden_feature, num_classes, args.num_layers)
    else:
        raise NotImplementedError()

    if args.gpu == -1:
        device = "cpu"
    else:
        device = "cuda:" + str(args.gpu)
    model = model.to(torch.device(device))
    opt = torch.optim.Adam(model.parameters())
    loss_fcn = nn.CrossEntropyLoss()

    for epoch in range(args.num_epochs):
        for input_nodes, output_nodes, blocks in dataloader:
            blocks = [b.to(torch.device(device)) for b in blocks]
            input_features = feat[input_nodes].to(torch.device(device))
            pred = model(blocks, input_features)
            output_labels = labels[output_nodes].to(torch.device(device))
            loss = loss_fcn(pred, output_labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            break

    with torch.no_grad():
        if args.gpufull:
            print(args.num_layers, args.model, "GPU FULL", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            pred = model.forward_full(g.to(device), feat.to(device))
            func_score = (torch.argmax(pred, dim=1) == labels.to(device)).float().sum() / len(pred)
            cost_time = time.time() - st
            print("CPU Inference: {}, inference time: {}".format(func_score, cost_time))

        elif args.cpufull:
            print(args.num_layers, args.model, "CPU FULL", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            model.to('cpu')
            pred = model.forward_full(g, feat)
            model.to(device)
            func_score = (torch.argmax(pred, dim=1) == labels).float().sum() / len(pred)
            cost_time = time.time() - st
            print("CPU Inference: {}, inference time: {}".format(func_score, cost_time))

        elif args.auto:
            print(args.num_layers, args.model, "auto", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            # helper = EdgeControlInferenceHelper(model, 2621440, torch.device('cuda'), debug = False)
            # helper = InferenceHelper(model, 2000, torch.device('cuda'), debug = False)
            helper = AutoInferenceHelper(model, torch.device(device), use_uva = args.use_uva, debug = args.debug)
            # import pdb
            # pdb.set_trace()
            helper_pred = helper.inference(g, feat)
            helper_score = (torch.argmax(helper_pred, dim=1) == labels).float().sum() / len(helper_pred)
            cost_time = time.time() - st
            print("Helper Inference: {}, inference time: {}".format(helper_score, cost_time))

        else:
            if args.gpu == -1:
                print(args.num_layers, args.model, "CPU", args.batch_size, args.dataset, args.num_heads, args.num_hidden)
            else:
                print(args.num_layers, args.model, "GPU", args.batch_size, args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            pred = model.inference(g, args.batch_size, torch.device(device), feat)
            func_score = (torch.argmax(pred, dim=1) == labels).float().sum() / len(pred)
            cost_time = time.time() - st
            if args.gpu != -1:
                print("max memory:", torch.cuda.max_memory_allocated() // 1024 ** 2)
            print("Origin Inference: {}, inference time: {}".format(func_score, cost_time))

if __name__ == '__main__':
    argparser = argparse.ArgumentParser()
    argparser.add_argument('--use-uva', action="store_true")
    argparser.add_argument('--cpufull', action="store_true")
    argparser.add_argument('--gpufull', action="store_true")
    argparser.add_argument('--gpu', type=int, default=0,
                           help="GPU device ID. Use -1 for CPU training")
    argparser.add_argument('--model', type=str, default='GCN')
    argparser.add_argument('--auto', action="store_true")
    argparser.add_argument('--debug', action="store_true")
    argparser.add_argument('--num-epochs', type=int, default=0)
    argparser.add_argument('--dataset', type=str, default='ogbn-products')
    argparser.add_argument('--num-hidden', type=int, default=128)
    argparser.add_argument('--num-heads', type=int, default=-1)
    argparser.add_argument('--num-layers', type=int, default=2)
    argparser.add_argument('--batch-size', type=int, default=2000)
    args = argparser.parse_args()

    train(args)