
import os.path as osp
from pathlib import Path
import glob
import time
import argparse
import numpy as np
from distutils.util import strtobool

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torch_geometric.transforms as T
from torch_sparse import SparseTensor

from ogb.nodeproppred import Evaluator

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class FeedForwardNet(nn.Module):
    """
    https://github.com/THUDM/CRGNN/blob/main/layer.py
    https://github.com/dmlc/dgl/blob/master/examples/pytorch/ogb/sign/sign.py
    """

    def __init__(
        self,
        in_channel: int,
        out_channel: int,
        hidden_channel: int,
        dropout: float,
        n_fflayers: int,
        batch_norm: bool = True
    ):

        super(FeedForwardNet, self).__init__()
        self.dropout = dropout
        self.n_fflayers = max(1, n_fflayers)
        self.batch_norm = batch_norm
        self.lins = nn.ModuleList()
        self.bns = nn.ModuleList()

        if n_fflayers == 1:
            self.lins.append(nn.Linear(in_channel, out_channel))
        else:
            self.lins.append(nn.Linear(in_channel, hidden_channel))
            self.bns.append(nn.BatchNorm1d(hidden_channel))

            for _ in range(n_fflayers-2):
                self.lins.append(nn.Linear(hidden_channel, hidden_channel))
                self.bns.append(nn.BatchNorm1d(hidden_channel))

            self.lins.append(nn.Linear(hidden_channel, out_channel))

        if self.n_fflayers > 1:
            self.prelu = nn.PReLU()
            self.dropout = nn.Dropout(dropout)

        self.reset_parameters()

    def reset_parameters(self):
        gain = nn.init.calculate_gain('relu')
        for lin in self.lins:
            nn.init.xavier_uniform_(lin.weight, gain=gain)
            nn.init.zeros_(lin.bias)
        for bns in self.bns:
            bns.reset_parameters()

    def forward(self, x):
        for i, layer in enumerate(self.lins):
            x = layer(x)
            if i < self.n_fflayers-1:
                if self.batch_norm == True:
                    x = self.dropout(self.prelu(self.bns[i](x)))
                else:
                    x = self.dropout(self.prelu(x))
        return x


class SIGN(torch.nn.Module):
    def __init__(
        self,
        in_channel: int,
        out_channel: int,
        hidden_channel: int,
        dropout: float,
        input_dropout: float,
        K: int,
        n_fflayers: int,
        batch_norm: bool = True
    ):
        super(SIGN, self).__init__()
        self.K = K
        self.n_fflayers = n_fflayers
        self.batch_norm = batch_norm
        self.dropout = nn.Dropout(dropout)
        self.input_dropout = nn.Dropout(input_dropout)
        self.prelu = nn.PReLU()
        self.inception_ffs = nn.ModuleList()

        # inception feedforward layers
        for _ in range(self.K + 1):
            self.inception_ffs.append(
                FeedForwardNet(
                    in_channel, hidden_channel, hidden_channel,
                    dropout, n_fflayers, batch_norm
                )
            )

        # feedforward layer for concatenated outputs
        self.concat_ff = FeedForwardNet(
            (self.K+1)*hidden_channel, out_channel,
            hidden_channel, dropout, n_fflayers, batch_norm
        )

    def reset_parameters(self):
        for layer in self.inception_ffs:
            layer.reset_parameters()
        self.concat_ff.reset_parameters()

    def forward(self, xs):
        """ xs = [AX^0, AX^1, ..., AX^K] """

        xs = [self.input_dropout(x) for x in xs]  # input dropout
        hs = []  # store forward pass of each AX^K

        for i, layer in enumerate(self.inception_ffs):
            hs.append(layer(xs[i]))

        return self.concat_ff(self.dropout(self.prelu(torch.cat(hs, dim=-1)))).log_softmax(dim=-1)


def time_wrapper(func):
    """ wrapper for recording time
    Args:
        func:   function to evaluate

    Return:
        output:         output of func
        delta_time:     seconds, time to exec func
    """
    def wrapper(*args, **kwargs):

        time_initial = time.time()
        output = func(*args, **kwargs)
        time_end = time.time()-time_initial

        # unpack tuple if func returns multiple outputs
        if isinstance(output, tuple):
            return *output, time_end

        return output, time_end

    return wrapper


def create_evaluator_fn(dataset):
    """
    Get evaluator from Open Graph Benchmark based on dataset
    """
    evaluator = Evaluator(name=f'ogbn-{dataset}')
    return lambda preds, labels: evaluator.eval({
        'y_true': labels.view(-1, 1),
        'y_pred': preds.view(-1, 1),
    })['acc']


def load_data(dataset):
    """ load data from dataset name """
    file_name = f'{dataset}.pth'
    path = glob.glob(f'./**/{file_name}', recursive=True)[0][2:]
    return torch.load(path)


@time_wrapper
def transform_data(data, K):

    # calculate adj matrix
    row, col = data.edge_index
    adj_t = SparseTensor(
        row=col,
        col=row,
        sparse_sizes=(data.num_nodes, data.num_nodes)
    )

    # setup degree normalization tensors
    deg = adj_t.sum(dim=1).to(torch.float)
    deg_inv_sqrt = deg.pow(-0.5)
    deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
    adj_t = deg_inv_sqrt.view(-1, 1) * adj_t * deg_inv_sqrt.view(1, -1)

    assert data.x is not None
    xs = [data.x]

    for i in range(1, K + 1):

        xs += [adj_t @ xs[-1]]
        data[f'x{i}'] = xs[-1]

    # sanity check
    if K > 0:
        assert hasattr(data, f'x{K}')

    return data


@time_wrapper
def train(data, model, optimizer, train_loader):
    model.train()

    for batch in train_loader:
        xs = [data.x[batch].to(device)]
        xs += [data[f'x{i}'][batch].to(device)
               for i in range(1, model.K + 1)]
        labels = data.y[batch].to(device)
        loss = F.nll_loss(model(xs), labels)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()


@torch.no_grad()
def eval(data, model, eval_loader, evaluator):

    @time_wrapper
    def inference(model, xs):
        return model(xs)
    model.eval()

    inf_time = 0
    preds, labels = [], []
    for batch in eval_loader:
        xs = [data.x[batch].to(device)]
        xs += [data[f'x{i}'][batch].to(device)
               for i in range(1, model.K + 1)]

        out, batch_time = inference(model, xs)
        preds.append(out.argmax(dim=1).cpu())
        # labels.append(data.y[batch])
        inf_time += batch_time

    # labels = torch.cat(labels, dim=0)
    preds = torch.cat(preds, dim=0)

    train_f1 = evaluator(preds[data.train_mask], data.y[data.train_mask])
    val_f1 = evaluator(preds[data.val_mask], data.y[data.val_mask])
    test_f1 = evaluator(preds[data.test_mask], data.y[data.test_mask])

    return [train_f1, val_f1, test_f1], inf_time


def main(args):
    # data
    data = load_data(args.DATASET)
    data, transform_time = transform_data(data, args.K)

    print('Transformation Time (s): {:.4f}'.format(transform_time))

    train_loader = DataLoader(
        data.train_mask,
        batch_size=args.TRAIN_BATCH_SIZE,
        shuffle=True,
        drop_last=False
    )

    eval_loader = DataLoader(
        data.n_id,
        batch_size=args.EVAL_BATCH_SIZE,
        shuffle=False,
        drop_last=False
    )

    # model
    model = SIGN(
        data.num_features,       # in_channel
        data.num_classes,        # out_channel
        args.HIDDEN_CHANNEL,
        args.DROPOUT,
        args.INPUT_DROPOUT,
        args.K,
        args.N_FFLAYERS,
        args.BATCH_NORM
    ).to(device)

    print('# Model Params:', sum(p.numel() for p in model.parameters()))

    # prep
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=args.LR,
        weight_decay=args.WEIGHT_DECAY
    )
    evaluator = create_evaluator_fn(args.DATASET)

    # per run
    model.reset_parameters()

    epoch_train_time = []
    epoch_inf_time = []
    best_epoch, best_val, best_test = 0, 0, 0

    for epoch in range(1, args.EPOCHS+1):
        start = time.time()
        _, train_time = train(data, model, optimizer, train_loader)
        epoch_train_time.append([train_time])

        if epoch % 100 == 0:
            accs, inf_time = eval(data, model, eval_loader, evaluator)
            epoch_inf_time.append([inf_time])
            end = time.time()
            log = 'Epoch {}, Time(s): {:.4f}, '.format(epoch, end - start)
            log += 'Acc: Train {:.4f}, Val {:.4f}, Test {:.4f}'.format(*accs)
            print(log)

            if accs[1] > best_val:
                best_epoch = epoch
                best_train, best_val, best_test = accs

    print('Best Epoch {}, Train {:.4f}, Val {:.4f}, Test {:.4f}'.format(
        best_epoch, best_train, best_val, best_test))
    print('Avg. Training Time (s): {:.4f} +/- {:.4f}'.format(
        np.mean(epoch_train_time), np.std(epoch_train_time)))
    print('Avg. Inference Time (s): {:.4f} +/- {:.4f}'.format(
        np.mean(epoch_inf_time), np.std(epoch_inf_time)))


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='SIGN')
    parser.add_argument('--SEED', type=int, default=42)
    parser.add_argument('--EPOCHS', type=int, default=1000)
    parser.add_argument('--HIDDEN_CHANNEL', type=int, default=512)
    parser.add_argument('--K', type=int, default=5)
    parser.add_argument('--LR', type=float, default=0.001)
    parser.add_argument('--DATASET', type=str, default='arxiv')
    parser.add_argument('--DROPOUT', type=float, default=0.5)
    parser.add_argument('--WEIGHT_DECAY', type=float, default=0)
    parser.add_argument('--TRAIN_BATCH_SIZE', type=int, default=50000)
    parser.add_argument('--EVAL_BATCH_SIZE', type=int, default=100000)
    parser.add_argument('--N_FFLAYERS', type=int, default=2)
    parser.add_argument('--INPUT_DROPOUT', type=float, default=0)
    parser.add_argument('--BATCH_NORM', type=strtobool, default=True)
    args = parser.parse_args()

    print(args)
    main(args)