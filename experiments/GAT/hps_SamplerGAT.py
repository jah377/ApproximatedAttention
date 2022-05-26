import wandb
import copy
from ogb.nodeproppred import Evaluator

import torch
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch_geometric.loader import NeighborLoader

from model_SamplerGAT import GAT
from steps_SamplerGAT import train_epoch, test_epoch
from general.utils import set_seeds, standardize_data

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

hyperparameter_defaults = dict(
    dataset='cora',
    seed=42,
    optimizer_type='Adam',
    optimizer_lr=1e-3,
    optimizer_decay=1e-3,
    epochs=5,
    hidden_channel=256,
    dropout=0.6,
    nlayers=3,
    batch_size=256,
)

wandb.init(config=hyperparameter_defaults)
config = wandb.config

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def main(config):
    set_seeds(config.seed)

    # IMPORT & STANDARDIZE DATA
    path = f'data/{config.dataset}_sign_k0.pth'
    data = torch.load(path)
    data = standardize_data(data, config.dataset)

    # CREATE TRAINING AND SUBGRAPH LOADERS
    # [n_neighbors] = hyperparameter
    train_loader = NeighborLoader(
        data,
        input_nodes=data.train_mask,  # can be bool or n_id indices
        num_neighbors=[config.n_neighbors]*config.nlayers,
        shuffle=True,
        batch_size=config.batch_size,
        drop_last=True,  # remove final batch if incomplete
        num_workers=config.num_workers,
    )

    subgraph_loader = NeighborLoader(
        copy.copy(data),
        input_nodes=None,
        num_neighbors=[-1]*config.nlayers,      # sample all neighbors
        shuffle=False,                          # :batch_size in sequential order
        batch_size=config.batch_size,
        drop_last=False,
        num_workers=config.num_workers,
    )
    subgraph_loader.data.num_nodes = data.num_nodes
    del subgraph_loader.data.x, subgraph_loader.data.y  # only need indices

    # BUILD MODEL
    model = GAT(
        data.num_features,  # in_channel
        data.num_classes,  # out_channel
        config.hidden_channel,
        config.dropout,
        config.nlayers,
        config.heads_in,
        config.heads_out,
    ).to(device)

    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    wandb.log({'trainable_params': n_params})  # size of model

    # BUILD OPTIMIZER
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=config.optimizer_lr,
        weight_decay=config.optimizer_decay,
    )

    # BUILD SCHEDULER (modulates learning rate)
    scheduler = ReduceLROnPlateau(
        optimizer,
        mode='min',     # nll_loss expected to decrease over epochs
        factor=0.1,     # lr reduction factor
        patience=5,     # reduce lr after _ epochs of no improvement
        min_lr=1e-6,    # min learning rate
        verbose=False,  # do not monitor lr updates
    )

    # EVALUATOR
    if data.dataset_name in ['products', 'arxiv']:
        evaluator = Evaluator(name=f'ogbn-{data.dataset_name}')
    else:
        evaluator = None

    # RUN THROUGH EPOCHS
    # params for early termination
    previous_loss = 1e10
    patience = 5
    trigger_times = 0

    for epoch in range(config.epochs):

        training_out, training_time = train_epoch(
            model,
            optimizer,
            train_loader
        )

        eval_out, inf_time = test_epoch(model, data, subgraph_loader, evaluator)

        val_loss = eval_out['val_loss']
        scheduler.step(val_loss)

        # log results
        log_dict = {
            'epoch': epoch,
            'epoch-training-train_time': training_time,
            'epoch-eval-inf_time': inf_time,
        }

        log_dict.update(
            {'epoch-train-train_'+k: v for k, v in training_out.items()}
        )

        log_dict.update({f'epoch-eval-'+k: v for k,
                        v in eval_out.items()})
        wandb.log(log_dict)

        # early stopping
        current_loss = val_loss
        if current_loss > previous_loss:
            trigger_times += 1
            if trigger_times >= patience:
                print('~~~ early stop triggered ~~~')
                break
        else:
            trigger_times = 0
        previous_loss = current_loss


if __name__ == "__main__":
    main(config)