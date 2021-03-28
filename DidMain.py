import json
import sys
from datetime import datetime

import torch
import torch.nn.functional as F
import torch.optim as optim

import wandb
from DidDataset import DidDataset
from DidModel import DidModel
from DidModelRunner import DidModelRunner
from parallel import DataParallelModel, DataParallelCriterion

if __name__ == "__main__":
    config_path = sys.argv[1]
    with open(config_path) as f:
        did_config = json.load(f)

    # get device on which training should run
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Using more than one GPU
    if torch.cuda.device_count() > 1:
        device_count = torch.cuda.device_count()
        print("Using:", device_count, "GPUs!")
        # config_defaults["batch_size"] = config_defaults["batch_size"] * device_count
        # print("Multiplying batch * GPUs new batch_size=", config_defaults["batch_size"])

    # Initialize a new wandb run
    wandb.init(project='w2v_did', config=did_config, entity='ba-reisedomfiviapas',
               name=datetime.now().strftime("w2v_did " + "_%Y%m%d-%H%M%S"))
    # Config is a variable that holds and saves hyperparameters and inputs
    config = wandb.config

    print('data:')
    print("  train_data: " + config.data['train_dataset'])
    print("  test_data: " + config.data['test_dataset'])
    print("  batch_size: " + str(config.data['batch_size']) + ", shuffle: " + str(config.data['shuffle']))

    print('model:')
    print("  location: " + config.model['model_location'])
    print("  num_classes: " + str(config.model['num_classes']) + ", freeze_fairseq: " + str(
        config.model['freeze_fairseq']))

    print('general:')
    print("  num_workers: " + str(config.general['num_workers']))
    print("  epochs: " + str(config.general['epochs']))
    print("  optimizer: " + config.general['optimizer'])
    print("  loss_function: " + config.general['loss_function'])
    print("  log_interval: " + str(config.general['log_interval']) + ", model_save_interval: " + str(
        config.general['model_save_interval']))

    # define params for data loaders
    kwargs = {'num_workers': config.general['num_workers'],
              'pin_memory': True} if device == 'cuda' else {}  # needed for using datasets on gpu

    # build train data
    csv_path_train = config.data['train_dataset'] + 'metadata.csv'  # file_path_train = './data/dev/segmented/'
    train_set = DidDataset(csv_path_train, config.data['train_dataset'])
    print("Train set size: " + str(len(train_set)))

    # build test data
    csv_path_test = config.data['test_dataset'] + 'metadata.csv'  # file_path_test = './data/dev/segmented/'
    test_set = DidDataset(csv_path_test, config.data['test_dataset'])
    print("Test set size: " + str(len(test_set)))

    # create our own model with classifier on top of fairseq's xlsr_53_56k.pt
    model = DidModel(model_path=config.model['model_location'],
                     num_classes=config.model['num_classes'],
                     freeze_fairseq=config.model['freeze_fairseq'])

    # Using more than one GPU
    if torch.cuda.device_count() > 1:
        print("Wrapping model with DataParallel")
        model = DataParallelModel(model)

    # build data loaders
    train_loader = torch.utils.data.DataLoader(train_set,
                                               batch_size=config.data['batch_size'],
                                               shuffle=config.data['shuffle'],
                                               **kwargs)
    test_loader = torch.utils.data.DataLoader(test_set,
                                              batch_size=config.data['batch_size'],
                                              shuffle=config.data['shuffle'],
                                              **kwargs)

    # Optimizer
    print('optimizer_params:')
    if config.general['optimizer'] == 'adam':
        print('  lr: ' + str(config.optimizers[config.general['optimizer']]['lr']) + ', weight_decay: ' + str(
            config.optimizers[config.general['optimizer']]['weight_decay']))
        optimizer = optim.Adam(model.parameters(),
                               lr=config.optimizers[config.general['optimizer']]['lr'],
                               weight_decay=config.optimizers[config.general['optimizer']]['weight_decay'])
    else:
        raise SystemExit("you must specify optimizer for " + config.general['optimizer'])

    # Scheduler
    print('scheduler_params:')
    print('  step_size: ' + str(config.scheduler['step_size']) + ', gamma: ' + str(config.scheduler['gamma']))
    scheduler = optim.lr_scheduler.StepLR(optimizer,
                                          step_size=config.scheduler['step_size'],
                                          gamma=config.scheduler['gamma'])

    # Loss Function
    if config.general['loss_function'] == 'nllLoss':
        loss_function = F.nll_loss
    else:
        raise SystemExit("you must specify loss_function for " + config.general['loss_function'])

    # Using more than one GPU
    if torch.cuda.device_count() > 1:
        print("Wrapping loss_function with DataParallelCriterion")
        loss_function = DataParallelCriterion(loss_function)

    output_for_loss = config.loss_functions[config.general['loss_function']]['output']

    # create runner for training and testing
    runner = DidModelRunner(device=device,
                            model=model,
                            optimizer=optimizer,
                            scheduler=scheduler,
                            wandb=wandb,
                            loss_function=loss_function,
                            output_for_loss=output_for_loss)

    wandb.watch(model)

    for epoch in range(config.general['epochs']):
        closs = runner.train(train_loader=train_loader,
                             epoch=epoch,
                             log_interval=config.general['log_interval'])
        wandb.log({"loss": closs / (len(train_loader.dataset) / config.batch_size)})

        if epoch % config.general['model_save_interval'] == 0:  # test and save model every n epochs
            accuracy = runner.test(test_loader=test_loader)
            wandb.log({"accuracy": accuracy})
        #     model_path = './models/did_model_epoch_' + str(epoch) + '.pt'
        #     print("Saving model to " + model_path)
        #     torch.save(model.state_dict(), model_path)

        scheduler.step()

    print('Finished Training')
