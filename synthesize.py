import json
import pandas as pd
import argparse
import torch
import numpy as np

from models import Discriminator, TransGeneratorWithAux, TimeTransGeneratorWithAux, Transformer, make_sample, GRUNet
from my_utils import load_M1, load_M2, load_dataset, get_datadir
import pathlib
from evaluation import evaluation


def run(generator, dataset, result_name, save_path, real_start, batch_size):
    
    print(real_start)

    for i in range(n_iter):
        (save_path / result_name).mkdir(exist_ok=True)
        generated_data_path = save_path / result_name / f"temp.csv"
#         generate_samples(generator, batch_size, dataset.seq_len, generated_num, generated_data_path, real_start)
        samples = make_sample(batch_size, generator, len(dataset), dataset, real_start)
        df = pd.DataFrame(samples).to_csv(generated_data_path, header=None, index=None)

        (save_path / result_name).mkdir(exist_ok=True)
        evaluation(str(dataset), args.save_name, f"{result_name}/temp.csv", f"{result_name}/{i}")


def next_location_top10(generator, query_data, batch_size):
    query = pd.read_csv(get_datadir() / f"next_location_query" / query_data, header=None).values
    real_start = generator.make_initial_data(len(query))
    real_start[:,generator.window_size-1] = torch.tensor(query).reshape(-1)
    
#     print(real_start)
    
    results = []
    for i in range(int(len(query) / batch_size)+1):
        batch = real_start[i*batch_size:(i+1)*batch_size][:,:generator.window_size].to(next(generator.parameters()).device)
        if len(real_start[i*batch_size:(i+1)*batch_size]) == 0:
            continue
        
#         print(batch)
        prob = -torch.exp(generator(batch)).cpu().detach().numpy()
#         print(prob)
        top_10 = np.argsort(prob)[:, :10]
        results.append(top_10)
    return pd.DataFrame(np.concatenate(results))
    
        
    
    
        
if __name__ == "__main__":
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--cuda_number',  default=0, type=int)
    parser.add_argument('--n_iter', default=1, type=int)
    parser.add_argument('--dataset', default='test', type=str)
    parser.add_argument('--data_name', default='test_1', type=str)
    parser.add_argument('--save_name', default="", type=str)
    parser.add_argument('--result_name', default="result", type=str)
    parser.add_argument('--model_name', default="trained_gen_10.pth", type=str)
    parser.add_argument('--real_start', action="store_true")
    parser.add_argument('--transformer', action="store_true")
    parser.add_argument('--gru', action="store_true")
    args = parser.parse_args()
    
    if args.save_name == "":
        args.save_name = args.data_name
    save_path = get_datadir() / "results" / args.dataset / args.save_name
    
    with open(save_path / "param.json", "r") as f:
        args_ = json.load(f)
    with open(get_datadir() / f"{args.dataset}/{args.data_name}/params.json", "r") as f:
        params = json.load(f)
    df = pd.read_csv(get_datadir() / f"{args.dataset}/{args.data_name}/training_data.csv", header=None)
    result_name = args.result_name
    model_name = args.model_name
    n_iter = args.n_iter
    real_start = args.real_start
    transformer = args.transformer
    gru = args.gru

    args = argparse.Namespace

    args.window_size = args_["window_size"]
    args.data_name = args_["data_name"]
    args.dataset = args_["dataset"]
    args.generator_embedding_dim = args_["generator_embedding_dim"]
    args.discriminator_embedding_dim = args_["discriminator_embedding_dim"]
    args.save_name = args_["save_name"]
    args.cuda_number = 0
    args.batch_size = 100

    dataset = load_dataset(args.dataset, args.data_name, args.window_size)
    generated_num = len(dataset)
    args.window_size = dataset.window_size
    n_vocabs = len(dataset.vocab)

    M1 = load_M1(dataset)
    M2 = load_M2(dataset)

    generator = TimeTransGeneratorWithAux(n_vocabs, args.window_size, dataset.seq_len, dataset.START_IDX, dataset.MASK_IDX, dataset.CLS_IDX, args.generator_embedding_dim, M1, M2).cuda(args.cuda_number)
    
    if transformer:
        embed_size = 128
        inner_ff_size = embed_size*4
        n_heads = 8
        n_code = 8
        generator = Transformer(n_code, n_heads, embed_size, inner_ff_size, n_vocabs, dataset.n_locations, dataset.seq_len, dataset.CLS_IDX, 0.1).cuda(args.cuda_number)
        
    if gru:
        input_dim = dataset.n_locations+1
        output_dim = dataset.n_locations
        hidden_dim = 256
        n_layers = 2
        generator = GRUNet(input_dim, hidden_dim, output_dim, n_layers).cuda(args.cuda_number)
        
        
    generator.data = dataset.data
    generator.eval()
    generator.real = False
    print("load generator from", save_path / model_name)
    generator.load_state_dict(torch.load(save_path / model_name))

#     if args.without_real_start:
#         real_start = None
#     else:
#         real_start = generator.make_initial_data(len(dataset))
#         real_start[:,generator.window_size] = torch.tensor(dataset.data[:, 0])

    run(generator, dataset, result_name, save_path, real_start, args.batch_size)
    results = next_location_top10(generator, f"{args.data_name}.csv", args.batch_size)
    results.to_csv(save_path / "next_locations.csv", header=None, index=None)