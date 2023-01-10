import os, sys
import torch
from torch import nn
from torch.utils.data import DataLoader
from sklearn.model_selection import ParameterGrid
import numpy as np
from pickle import dump, load
from time import time
import json 

from utils.db6 import DB6MultiSession
from utils.utils import SuperSet
from utils.model import ViT_quantized as ViT
from utils.model import TEMPONet as TEMPONet
from utils.download_DB6 import download_file
from utils.utils import get_loss_preds
from utils.train import train 
from utils.configs import configs_pretrain, configs_finetune, configs_finetune_nopretrain
import argparse

PROCESSES = 1
save_model_every_n = 50
name_prefix = f"None"

def extend_results(results, result):
    if results is None:
        return result
    for i in range(len(results)): # n campi di results (config, ecc)
        results[i].extend(result[i])
    return results

def main_quantize(chunk_idx, args):
    configs = configs_chunks_finetune[chunk_idx]
    configs[0]["depth"] = args.depth
    configs[0]["heads"] = args.heads
    configs[0]["dim_head"] = args.dim_head
    configs[0]["ch_1"] = args.ch_1
    configs[0]["ch_2"] = args.ch_2
    configs[0]["ch_3"] = args.ch_3
    configs[0]["patch_size1"] = (1, args.patch_size1)
    configs[0]["patch_size2"] = (1, args.patch_size2)
    configs[0]["patch_size3"] = (1, args.patch_size3)
    configs[0]["tcn_layers"] = args.tcn_layers
    subject = configs[0]['subjects']
    n_sessions = configs[0]['sessions']
    train_sessions = list(range(configs[0]['sessions']))
    test_sessions = list(range(configs[0]['sessions'], 10))
    steady=True
    n_classes='7+1'
    bootstrap='no'
    print("Training subject", subject)
    print("Steady", steady)
    print("Bootstrap", bootstrap)
    minmax = True
    if configs[0]["pretrained"] is not None:
        subs = ','.join([str(a) for a in range(1, 11) if a != subject])
        minmax_picklename = f'artifacts/ds_minmax_sessions={n_sessions}subjects={subs}.pickle'
        print("Loading minmax from", minmax_picklename)
        minmax = load(open(minmax_picklename, 'rb'))

    ds = DB6MultiSession(folder=os.path.expanduser(dataset_folder), subjects=[subject], sessions=train_sessions, steady=steady, n_classes=n_classes, minmax=minmax, image_like_shape=True).to(device)
    test_datasets_steady = [DB6MultiSession(folder=os.path.expanduser(dataset_folder), subjects=[subject], sessions=[i], 
                                            steady=True, n_classes=n_classes, minmax=(ds.X_min, ds.X_max), image_like_shape=True) \
                            for i in test_sessions]
    results_ = []
    for i, config in enumerate(configs, start=1):
        config['pretrained'] = f"../{name_prefix}_{subject - 1}_epoch100.pth"
        results = {}
        results['subject'] = subject
        results['steady'] = steady
        results['n_classes'] = n_classes
        results['bootstrap'] = bootstrap
        results['train_sessions'] = train_sessions
        results['test_sessions'] = test_sessions
        result = {}

        if args.network == "TEMPONet":
            net_fp32 = TEMPONet()
        elif args.network == "ViT":
            net_fp32 = ViT(**config)
        config['pretrained'] = f"{name_prefix}_{9 - (subject - 1)}_epoch100.pth"
        if config['pretrained'] is not None:
            net_fp32.load_state_dict(torch.load(config['pretrained'], map_location=torch.device('cpu')))
            print("Loaded checkpoint", config['pretrained'])
        
        net_fp32.eval()
        net_fp32.qconfig = torch.quantization.get_default_qconfig('fbgemm')

        model_fp32_prepared = torch.quantization.prepare(net_fp32)

        # calibrate the prepared model to determine quantization parameters for activations
        # in a real world setting, the calibration would be done with a representative dataset
        torch.manual_seed(2023)
        loader = DataLoader(ds, batch_size=1024, shuffle=True, pin_memory=False, drop_last=False)
        i = 0
        for X_batch, Y_batch in loader:
            if i == 0:
                input_fp32 = X_batch.to("cpu")
            else:
                input_fp32 = torch.concat((input_fp32, X_batch.to("cpu")))
            i = 1

        # Convert the observed model to a quantized model. This does several things:
        # quantizes the weights, computes and stores the scale and bias value to be
        # used with each activation tensor, and replaces key operators with quantized
        # implementations.
        model_int8 = torch.quantization.convert(model_fp32_prepared)

        criterion = nn.CrossEntropyLoss()
        test_losses, y_preds, y_trues, outs = [], [], [], []

        torch.manual_seed(0)
        for test_ds in test_datasets_steady:
            test_loader = DataLoader(test_ds, batch_size=1024, shuffle=False, pin_memory=False, drop_last=False)
            test_loss, (y_pred, y_true, out) = get_loss_preds(model_int8, criterion, test_loader, device = device)
            test_losses.append(test_loss)
            y_preds.append(y_pred.cpu())
            y_trues.append(y_true.cpu())
            outs.append(out)
        result['test_losses_steady'] = test_losses
        result['y_preds_steady'] = y_preds
        result['y_trues_steady'] = y_trues
        result['outs_steady'] = outs
        results[f'val-fold'] = result
        correct = 0
        total = 0
        for i in np.arange(5):
            correct+= sum(y_preds[i]==y_trues[i])
            total+= len(y_preds[i])
        acc = correct/total*100
        print(f"Accuracy of subject Quantized {subject}: {acc}")


        criterion = nn.CrossEntropyLoss()
        test_losses, y_preds, y_trues, outs = [], [], [], []
        torch.manual_seed(0)
        for test_ds in test_datasets_steady:
            test_loader = DataLoader(test_ds, batch_size=1024, shuffle=False, pin_memory=False, drop_last=False)
            test_loss, (y_pred, y_true, out) = get_loss_preds(net_fp32, criterion, test_loader, device = device)
            test_losses.append(test_loss)
            y_preds.append(y_pred.cpu())
            y_trues.append(y_true.cpu())
            outs.append(out)
        result['test_losses_steady'] = test_losses
        result['y_preds_steady'] = y_preds
        result['y_trues_steady'] = y_trues
        result['outs_steady'] = outs
        results[f'val-fold'] = result
        correct = 0
        total = 0
        for i in np.arange(5):
            correct+= sum(y_preds[i]==y_trues[i])
            total+= len(y_preds[i])
        acc = correct/total*100
        print(f"Accuracy of subject {subject}: {acc}")

    results_.append(results)
    return [configs, results_]


def main_inference(chunk_idx, args):
    configs = configs_chunks_finetune[chunk_idx]
    configs[0]["depth"] = args.depth
    configs[0]["heads"] = args.heads
    configs[0]["dim_head"] = args.dim_head
    configs[0]["ch_1"] = args.ch_1
    configs[0]["ch_2"] = args.ch_2
    configs[0]["ch_3"] = args.ch_3
    configs[0]["patch_size1"] = (1, args.patch_size1)
    configs[0]["patch_size2"] = (1, args.patch_size2)
    configs[0]["patch_size3"] = (1, args.patch_size3)
    configs[0]["tcn_layers"] = args.tcn_layers
    subject = configs[0]['subjects']
    n_sessions = configs[0]['sessions']
    train_sessions = list(range(configs[0]['sessions']))
    test_sessions = list(range(configs[0]['sessions'], 10))
    steady=True
    n_classes='7+1'
    bootstrap='no'
    print("Training subject", subject)
    print("Steady", steady)
    print("Bootstrap", bootstrap)
    minmax = True
    if configs[0]["pretrained"] is not None:
        subs = ','.join([str(a) for a in range(1, 11) if a != subject])
        minmax_picklename = f'artifacts/ds_minmax_sessions={n_sessions}subjects={subs}.pickle'
        print("Loading minmax from", minmax_picklename)
        minmax = load(open(minmax_picklename, 'rb'))

    test_datasets_steady = [DB6MultiSession(folder=os.path.expanduser(dataset_folder), subjects=[subject], sessions=[i], 
                                            steady=True, n_classes=n_classes, minmax=minmax, image_like_shape=True) \
                            for i in test_sessions]
    results_ = []
    for i, config in enumerate(configs, start=1):
        config['pretrained'] = f"../{name_prefix}_{subject - 1}_epoch100.pth"
        results = {}
        results['subject'] = subject
        results['steady'] = steady
        results['n_classes'] = n_classes
        results['bootstrap'] = bootstrap
        results['train_sessions'] = train_sessions
        results['test_sessions'] = test_sessions
        result = {}
        if args.network == "TEMPONet":
            net_fp32 = TEMPONet()
        elif args.network == "ViT":
            net_fp32 = ViT(**config)
        config['pretrained'] = f"{name_prefix}_{9 - (subject - 1)}_epoch100.pth"
        if config['pretrained'] is not None:
            net_fp32.load_state_dict(torch.load(config['pretrained'], map_location=torch.device('cpu')))
            print("Loaded checkpoint", config['pretrained'])
        
        net_fp32.eval()
        net_fp32.qconfig = torch.quantization.get_default_qconfig('fbgemm')
        criterion = nn.CrossEntropyLoss()
        test_losses, y_preds, y_trues, outs = [], [], [], []

        torch.manual_seed(0)
        for test_ds in test_datasets_steady:
            test_loader = DataLoader(test_ds, batch_size=1024, shuffle=False, pin_memory=False, drop_last=False)
            test_loss, (y_pred, y_true, out) = get_loss_preds(net_fp32, criterion, test_loader, device = device)
            test_losses.append(test_loss)
            y_preds.append(y_pred.cpu())
            y_trues.append(y_true.cpu())
            outs.append(out)
        result['test_losses_steady'] = test_losses
        result['y_preds_steady'] = y_preds
        result['y_trues_steady'] = y_trues
        result['outs_steady'] = outs
        results[f'val-fold'] = result
        correct = 0
        total = 0
        for i in np.arange(5):
            correct+= sum(y_preds[i]==y_trues[i])
            total+= len(y_preds[i])
        acc = correct/total*100
        print(f"Accuracy of subject {subject}: {acc}")
    results_.append(results)
    return [configs, results_]


configs = list(ParameterGrid({k: (v if isinstance(v, list) else [v]) for k, v in configs_pretrain.items()}))
configs = sorted(configs, key=lambda x: (x["subjects"], x["sessions"]) )
configs_chunks_pretrain = []
dataset_combinations = set(map(lambda x: (x["subjects"], x["sessions"]), configs))
if len(dataset_combinations) == 1:
    for indices in np.array_split(np.arange(len(configs)), PROCESSES):
        if len(indices) > 0: # 
            configs_chunks_pretrain.append(configs[indices[0]:indices[-1]+1])
else:
    prev_dataset_combination, new_chunk = None, None
    for config in configs:
        current_dataset_combination = (config["subjects"], config["sessions"])
        if current_dataset_combination != prev_dataset_combination:
            prev_dataset_combination = current_dataset_combination
            if new_chunk is not None:
                configs_chunks_pretrain.append(new_chunk)
            new_chunk = []
        new_chunk.append(config)
    configs_chunks_pretrain.append(new_chunk)
configs_chunks_idx_pretrain = list(range(len(configs_chunks_pretrain)))


configs = list(ParameterGrid({k: (v if isinstance(v, list) else [v]) for k, v in configs_finetune_nopretrain.items()}))
configs = sorted(configs, key=lambda x: (x["subjects"], x["sessions"]) )
configs_chunks_finetune = []
dataset_combinations = set(map(lambda x: (x["subjects"], x["sessions"]), configs))
if len(dataset_combinations) == 1:
    for indices in np.array_split(np.arange(len(configs)), PROCESSES):
        if len(indices) > 0: # 
            configs_chunks_finetune.append(configs[indices[0]:indices[-1]+1])
else:
    prev_dataset_combination, new_chunk = None, None
    for config in configs:
        current_dataset_combination = (config["subjects"], config["sessions"])
        if current_dataset_combination != prev_dataset_combination:
            prev_dataset_combination = current_dataset_combination
            if new_chunk is not None:
                configs_chunks_finetune.append(new_chunk)
            new_chunk = []
        new_chunk.append(config)
    configs_chunks_finetune.append(new_chunk)
configs_chunks_idx_finetune = list(range(len(configs_chunks_finetune)))

if __name__ == '__main__':
    print("Python Script starting")
    
    parser = argparse.ArgumentParser()
    # Add an argument to the parser
    parser.add_argument('--network', choices=['TEMPONet', 'ViT'], default = "ViT", type=str)
    parser.add_argument('--tcn_layers', choices = [1, 2], default = 1, type=int)
    parser.add_argument('--blocks', choices = [1, 2, 3], default = 1, type=int)
    parser.add_argument('--dim_head', choices = [8, 16, 32, 64], default = 32, type=int)
    parser.add_argument('--heads', choices = [1, 2, 4, 8], default = 8, type=int)
    parser.add_argument('--depth', choices = [1, 2, 4], default = 1, type=int)
    parser.add_argument('--patch_size1', choices = [1, 3, 5, 10, 30, 60], default = 10, type=int)
    parser.add_argument('--patch_size2', choices = [0, 1, 3, 5, 10, 30, 60], default = 0, type=int)
    parser.add_argument('--patch_size3', choices = [0, 1, 3, 5, 10, 30, 60], default = 0, type=int)
    parser.add_argument('--ch_1', default = 14, type=int)
    parser.add_argument('--ch_2', default = 'None', type=int)
    parser.add_argument('--ch_3', default = 'None', type=int)
    parser.add_argument('--subjects', default = 1, type = int)
    parser.add_argument('--pretrain', default = 'True')
    parser.add_argument('--finetune', default = 'False')
    # Parse the command-line arguments
    args = parser.parse_args()
    if args.network == "TEMPONet":
        name_prefix = f"artifacts/temponet"
    else:
        name_prefix = f"artifacts/ViT_{args.tcn_layers}_{args.blocks}_{args.dim_head}_{args.heads}_{args.depth}_{args.patch_size1}_{args.patch_size2}_{args.patch_size3}_{args.ch_1}_{args.ch_2}_{args.ch_3}"
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print("Missing configuration file: Add config.json file")
        exit(0)
    device = config['device']
    dataset_folder = config['dataset_dir']
    
    #if device == "cuda":
    #    os.environ["CUDA_VISIBLE_DEVICES"]=config["gpu_number"]
    if len(os.listdir(config['dataset_dir'])) == 0:
        for subject in np.arange(1,11):
            for part in ['a', 'b']:
                download_file(subject, part, download_dir = config['dataset_dir'], keep_zip = 'no')
    else:
        print('Dataset already in {} directory'.format(config['dataset_dir']))

    if args.pretrain == 'True':
        pretrain = True
    else:
        pretrain = False

    if args.finetune == 'True':
        finetune = True
    else:
        finetune = False

    if args.subjects == 1:
        i_begin = 0
        i_end = 5
    else:
        i_begin = 5
        i_end = 10

    if False:
        results = None
        for i in configs_chunks_idx_pretrain[i_begin:i_end]:
            result = main_inference(i, args)
            results = extend_results(results, result)
        pickle_name = f'{name_prefix}_results_pretrain_{i_begin}_{i_end}_{time():.0f}.pickle'
        dump(results, open(pickle_name, 'wb'))
        print("Saved", pickle_name)
    
    if True:
        results = None
        for i in configs_chunks_idx_finetune[i_begin:i_end]:
            # result = main_finetune(i, args)
            result = main_quantize(i, args)
            results = extend_results(results, result)
        pickle_name = f'{name_prefix}_results_finetune_nopretraining_{i_begin}_{i_end}_{time():.0f}.pickle'
        dump(results, open(pickle_name, 'wb'))
        print("Saved", pickle_name)
