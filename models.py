import torch.nn.functional as F
import torch.nn as nn
import torch
import math
import pdb
import torch
import bisect
import numpy as np
from torch.autograd import Variable
import pandas as pd
from opacus.layers.dp_rnn import DPGRU

def attention(q, k, v, mask = None, dropout = None):
    scores = q.matmul(k.transpose(-2, -1))
    scores /= math.sqrt(q.shape[-1])
    

    scores = scores if mask is None else scores.masked_fill(mask == 0, -1e3)
    
    scores = F.softmax(scores, dim = -1)
    scores = dropout(scores) if dropout is not None else scores
    output = scores.matmul(v)
    return output

class MultiHeadAttention(nn.Module):
    def __init__(self, n_heads, out_dim, dropout=0.1):
        super().__init__()
        
        self.linear = nn.Linear(out_dim, out_dim*3)

        self.n_heads = n_heads
        self.out_dim = out_dim
        self.out_dim_per_head = out_dim // n_heads
        self.out = nn.Linear(out_dim, out_dim)
        self.dropout = nn.Dropout(dropout)
    
    def split_heads(self, t):
        return t.reshape(t.shape[0], -1, self.n_heads, self.out_dim_per_head)
    
    def forward(self, x, y=None, mask=None):
        #in decoder, y comes from encoder. In encoder, y=x
        y = x if y is None else y
        
        qkv = self.linear(x) # BS * SEQ_LEN * (3*EMBED_SIZE_L)
        q = qkv[:, :, :self.out_dim] # BS * SEQ_LEN * EMBED_SIZE_L
        k = qkv[:, :, self.out_dim:self.out_dim*2] # BS * SEQ_LEN * EMBED_SIZE_L
        v = qkv[:, :, self.out_dim*2:] # BS * SEQ_LEN * EMBED_SIZE_L
        
        #break into n_heads
        q, k, v = [self.split_heads(t) for t in (q,k,v)]  # BS * SEQ_LEN * HEAD * EMBED_SIZE_P_HEAD
        q, k, v = [t.transpose(1,2) for t in (q,k,v)]  # BS * HEAD * SEQ_LEN * EMBED_SIZE_P_HEAD
        
        #n_heads => attention => merge the heads => mix information
        scores = attention(q, k, v, mask, self.dropout) # BS * HEAD * SEQ_LEN * EMBED_SIZE_P_HEAD
        scores = scores.transpose(1,2).contiguous().view(scores.shape[0], -1, self.out_dim) # BS * SEQ_LEN * EMBED_SIZE_L
        out = self.out(scores)  # BS * SEQ_LEN * EMBED_SIZE
        
        return out

class FeedForward(nn.Module):
    def __init__(self, inp_dim, inner_dim, dropout=0.1):
        super().__init__()
        self.linear1 = nn.Linear(inp_dim, inner_dim)
        self.linear2 = nn.Linear(inner_dim, inp_dim)
        self.dropout = nn.Dropout(dropout)
    
    def forward(self, x):
        #inp => inner => relu => dropout => inner => inp
        return self.linear2(self.dropout(F.relu(self.linear1(x)))) 

class EncoderLayer(nn.Module):
    def __init__(self, n_heads, inner_transformer_size, inner_ff_size, dropout=0.1):
        super().__init__()
        self.mha = MultiHeadAttention(n_heads, inner_transformer_size, dropout)
        self.ff = FeedForward(inner_transformer_size, inner_ff_size, dropout)
        self.norm1 = nn.LayerNorm(inner_transformer_size)
        self.norm2 = nn.LayerNorm(inner_transformer_size)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
    
    def forward(self, x, mask=None):
        x2 = self.norm1(x)
        x = x + self.dropout1(self.mha(x2, mask=mask))
        x2 = self.norm2(x)
        x = x + self.dropout2(self.ff(x2))
        return x

class Transformer(nn.Module):
    def __init__(self, n_code, n_heads, embed_size, inner_ff_size, n_embeddings, n_locations, seq_len, cls_index, dropout=.1):
        super().__init__()

        #model input
        self.embeddings = nn.Embedding(n_embeddings, embed_size)
        self.pe = PositionalEmbedding(embed_size, seq_len)
        self.n_embeddings = n_embeddings
        self.n_locations = n_locations
        
        #backbone
        encoders = []
        for i in range(n_code):
            encoders += [EncoderLayer(n_heads, embed_size, inner_ff_size, dropout)]
        self.encoders = nn.ModuleList(encoders)
        
        #language model
        self.norm = nn.LayerNorm(embed_size)
        self.linear = nn.Linear(embed_size, n_locations, bias=False)
        self.cls_index = cls_index
                
            
    def forward_without_softmax(self, x, mask=None):
        cls_array = torch.tensor([self.cls_index]*(len(x))).reshape(-1,1).to(next(self.parameters()).device)
        x = torch.cat([x, cls_array], dim=1)
        x = self.embeddings(x)
        x = x + self.pe(x)
        for encoder in self.encoders:
            x = encoder(x, mask=mask)
        x = self.norm(x)
        x = self.linear(x)
        return x

            
    def forward(self, x, mask=None):
        x = self.forward_without_softmax(x)
        x = F.log_softmax(x, dim=-1)[:,-1,:]
        return x
    
class TimeTransformer(nn.Module):
    def __init__(self, n_code, n_heads, embed_size, inner_ff_size, n_embeddings, n_locations, seq_len, start_index, dropout=.1):
        super().__init__()
        
        #model input
        tim_embedding_dim = 16
        self.time_embeddings = nn.Embedding(num_embeddings=seq_len+1, embedding_dim=tim_embedding_dim) 
        self.embeddings = nn.Embedding(n_embeddings, embed_size-tim_embedding_dim)

        #backbone
        encoders = []
        for i in range(n_code):
            encoders += [EncoderLayer(n_heads, embed_size, inner_ff_size, dropout)]
        self.encoders = nn.ModuleList(encoders)
        
        #language model
        self.norm = nn.LayerNorm(embed_size)
        self.linear = nn.Linear(embed_size, n_locations, bias=False)
        
        self.start_index = start_index
        self.seq_len = seq_len
                
            
    def make_time_data(self, x):
        x = x.detach().cpu()
        # print(x)
        start_numbers = [sum(v) for v in x==self.start_index]
    #         print(start_numbers)
        # times = [torch.tensor(range(self.seq_len-v)) for v in start_numbers]
        times = [torch.tensor(range(self.seq_len-v-1)) for v in start_numbers]
        times = torch.cat(times).long()
        x[x!=self.start_index] = times
        x[x==self.start_index] = self.seq_len
        return x
    
    def forward_without_softmax(self, x, mask=None):
        t = self.make_time_data(x).to(next(self.parameters()).device)
        x = self.embeddings(x)
        t = self.time_embeddings(t)
        x = torch.cat([x, t], dim=-1)
        for encoder in self.encoders:
            x = encoder(x, mask=mask)
        x = self.norm(x)
        x = self.linear(x)
        return x

    def forward(self, x, mask=None):
        x = self.forward_without_softmax(x)
        x = F.log_softmax(x, dim=-1)[:,-1,:]
        return x

# Positional Embedding
class PositionalEmbedding(nn.Module):
    def __init__(self, d_model, max_seq_len = 80):
        super().__init__()
        self.d_model = d_model
        pe = torch.zeros(max_seq_len, d_model)
        pe.requires_grad = False
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = math.sin(pos / (10000 ** ((2 * i)/d_model)))
                pe[pos, i + 1] = math.cos(pos / (10000 ** ((2 * (i + 1))/d_model)))
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
    
    def forward(self, x):
        return self.pe[:,:x.size(1)] #x.size(1) = seq_len
    
    
class GRUNet(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, n_layers, drop_prob=0.1):
        super(GRUNet, self).__init__()
        embed_size = 128
        self.embeddings = nn.Embedding(input_dim, embed_size)
        self.hidden_dim = hidden_dim
        self.n_layers = n_layers
        
        self.gru = nn.GRU(embed_size, hidden_dim, n_layers, batch_first=True, dropout=drop_prob)
        self.fc = nn.Linear(hidden_dim, output_dim)
        self.relu = nn.ReLU()
        
#     def forward(self, x, h):
    def forward(self, x):
        h = self.init_hidden(x.shape[0])
        x = self.embeddings(x)
        out, _ = self.gru(x, h)
        out = self.fc(self.relu(out[:,-1]))
        return F.log_softmax(out, dim=-1)
    
    def init_hidden(self, batch_size):
        weight = next(self.parameters()).data
        hidden = weight.new(self.n_layers, batch_size, self.hidden_dim).zero_().to(next(self.parameters()).device)
        return hidden
    
class DPGRUNet(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, n_layers, drop_prob=0.1):
        super(DPGRUNet, self).__init__()
        embed_size = 128
        self.embeddings = nn.Embedding(input_dim, embed_size)
        self.hidden_dim = hidden_dim
        self.n_layers = n_layers
        
        self.gru = DPGRU(embed_size, hidden_dim, n_layers, batch_first=True, dropout=drop_prob)
        self.fc = nn.Linear(hidden_dim, output_dim)
        self.relu = nn.ReLU()
        
#     def forward(self, x, h):
    def forward(self, x):
        h = self.init_hidden(x.shape[0])
        x = self.embeddings(x)
        out, _ = self.gru(x, h)
        out = self.fc(self.relu(out[:,-1]))
        return F.log_softmax(out, dim=-1)
    
    def init_hidden(self, batch_size):
        weight = next(self.parameters()).data
        hidden = weight.new(self.n_layers, batch_size, self.hidden_dim).zero_().to(next(self.parameters()).device)
        return hidden
    
    

class Discriminator(nn.Module):
    """Basic discriminator.
    """

    def __init__(
            self,traj_length,
            total_locations=8606,
            embedding_net=None,
            embedding_dim=64,
            dropout=0.6,):
        super(Discriminator, self).__init__()
        num_filters = [100, 200, 200, 200, 200, 100, 100, 100, 100, 100, 160, 160][:traj_length]
        filter_sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20][:traj_length]
        self.embedding = nn.Embedding(num_embeddings=total_locations, embedding_dim=embedding_dim)
        self.convs = nn.ModuleList([nn.Conv2d(1, n, (f, embedding_dim)) for (n, f) in zip(num_filters, filter_sizes)])
        self.highway = nn.Linear(sum(num_filters), sum(num_filters))
        self.dropout = nn.Dropout(p=dropout)
        self.linear = nn.Linear(sum(num_filters), 2)
        self.init_parameters()

    def forward(self, x):
        """
        Args:
            x: (batch_size * seq_len)
        """
        emb = self.embedding(x).unsqueeze(1)  
        convs = [F.relu(conv(emb)).squeeze(3) for conv in self.convs]
        pools = [F.max_pool1d(conv, conv.size(2)).squeeze(2)
                 for conv in convs]  # [batch_size * num_filter]
        pred = torch.cat(pools, 1)  # batch_size * num_filters_sum
        highway = self.highway(pred)
        pred = torch.sigmoid(highway) * F.relu(highway) + \
            (1. - torch.sigmoid(highway)) * pred
        pred = F.log_softmax(self.linear(self.dropout(pred)), dim=-1)
        return pred

    def init_parameters(self):
        for param in self.parameters():
            param.data.uniform_(-0.05, 0.05)




def step(generator, i, window_size, sample, n_locations, start_index):

#     if self.real == True:
#         self.step_from_real(i, sample)
#         return
    pad_num = window_size - i
    if pad_num > 0:
        padding_data = torch.tensor([[start_index]*pad_num]*sample.shape[0])
        input = torch.cat([padding_data, sample], dim=1).to(next(generator.parameters()).device)[:, :window_size].long()
    else:
        input = sample.to(next(generator.parameters()).device)[:, i-window_size:i].long()
        
#     print(input)
    probs = torch.exp(generator(input)).detach().cpu().numpy()

    for j, prob in enumerate(probs):
        prob = prob / prob.sum()
        sample[j, i] = np.random.choice(n_locations, p=prob)
    return sample


# def step_from_real(self, i, sample):
#     input = sample.to(next(self.parameters()).device)[:, i:self.window_size+i]
#     probs = torch.exp(self(input)).detach().cpu().numpy()
#     real = self.data[:, i]
#     multiplier = np.zeros(self.n_locations)
#     for v in real:
#         if v >= self.n_locations:
#             continue
#         multiplier[v] = 1

#     for j, prob in enumerate(probs):
#         prob *= multiplier
#         prob = prob / prob.sum()
#         sample[j, self.window_size+i] = np.random.choice(self.n_locations, p=prob)

def make_frame_data(n_sample, seq_len, start_index):
    frame_data = torch.zeros((n_sample, seq_len))
    frame_data.fill_(start_index)
    return frame_data

def recurrent_step(generator, seq_len, window_size, n_locations, start_time, data, start_index):
    for i in range(start_time, seq_len):
        # print(data)
        step(generator, i, window_size, data, n_locations, start_index)
    return data


def make_sample(batch_size, generator, n_sample, dataset, real_start=True):

    frame_data = make_frame_data(n_sample, dataset.seq_len, dataset.START_IDX)
    start_time = 0
    
    if real_start:
        start_time = 1
        indice = np.random.choice(range(len(dataset)), n_sample, replace=False)
        real_data = dataset.data[indice]
        frame_data[:,0] = torch.tensor(real_data[:,0])
    
    samples = []
    for i in range(int(n_sample / batch_size)):
        sample = recurrent_step(generator, dataset.seq_len, dataset.window_size, dataset.n_locations, start_time, frame_data[i*batch_size:(i+1)*batch_size], dataset.START_IDX).cpu().detach().long().numpy()
        samples.extend(sample)
    return samples

def make_input_for_predict_next_location_on_all_stages(self, x, start_time=0):
    input = []
    for traj in x:
        for i in range(self.window_size):
            input.append([self.start_index]*(self.window_size-start_time-i) + [state.item() for state in traj[max(start_time-self.window_size+i,0):start_time+i]])

    return torch.tensor(input).long()

def predict_next_location_on_all_stages(self, x, start_time=0):
    input = self.make_input_for_predict_next_location_on_all_stages(x, start_time).to(next(self.parameters()).device)
    probs = []
    for i in range(int(len(input)/self.window_size)):
        windowed_input = input[i*self.window_size:(i+1)*self.window_size].to(next(self.parameters()).device)
        prob = self(windowed_input)
        probs.append(prob)
    return torch.cat(probs).reshape(x.shape[0]*self.window_size, -1)

    
# def make_generator(class_name):
    
#     class TransGenerator(class_name):

#         def __init__(self, n_vocabs, window_size, seq_len, start_index, mask_index, cls_index, generator_embedding_dim):
#             embed_size = generator_embedding_dim
#             inner_ff_size = embed_size * 4
#             n_heads = 8
#             n_code = 8
#             super().__init__(n_code, n_heads, embed_size, inner_ff_size, n_vocabs, window_size+1, 0.1)
#             self.start_index = start_index 
#             self.mask_index = mask_index
#             self.cls_index = cls_index
#             self.seq_len = seq_len
#             self.n_vocabs = n_vocabs
#             self.n_locations = n_vocabs - 5
#             self.window_size = window_size

#         def make_initial_data(self, n_sample, data=[]):

#             data_len = len(data[0]) if data != [] else 0
#             samples = torch.tensor([[self.start_index]*(self.seq_len+self.window_size)]*n_sample).long()
#             samples[:,0] = self.start_index

#             samples[:, self.window_size:self.window_size+data_len] = torch.tensor(data)

#             return samples


#         def make_input_for_predict_next_location_on_all_stages(self, x, start_time=0):
#             input = []
#             for traj in x:
#                 for i in range(self.window_size):
#                     input.append([self.start_index]*(self.window_size-start_time-i) + [state.item() for state in traj[max(start_time-self.window_size+i,0):start_time+i]])

#             return torch.tensor(input).long()
        
#         def predict_next_location_on_all_stages(self, x, start_time=0):
#             input = self.make_input_for_predict_next_location_on_all_stages(x, start_time).to(next(self.parameters()).device)
#             probs = []
#             for i in range(int(len(input)/self.window_size)):
#                 windowed_input = input[i*self.window_size:(i+1)*self.window_size].to(next(self.parameters()).device)
#                 prob = self(windowed_input)
#                 probs.append(prob)
#             return torch.cat(probs).reshape(x.shape[0]*self.window_size, -1)


#         def forward_without_softmax(self, x):
#             cls_array = torch.tensor([self.cls_index]*(len(x))).reshape(-1,1).to(next(self.parameters()).device)
#             x = torch.cat([x, cls_array], dim=1)
#             x = super().forward_without_softmax(x)[:,-1,:self.n_locations]
#             return x
    
    return TransGenerator

def add_aux(class_name):
    
    class TransGeneratorWithAux(class_name):

        def __init__(self, n_vocabs, window_size, seq_len, start_index, mask_index, cls_index, generator_embedding_dim, M1=None, M2=None):
            super().__init__(n_vocabs, window_size, seq_len, start_index, mask_index, cls_index, generator_embedding_dim)

            self.linear_dim = 128
            
            if M1 is not None:
                self.M1 = M1
                self.M1 = np.concatenate([M1, np.zeros((1,M1.shape[1]))], axis=0)
                self.linear_M1 = nn.Linear(self.n_locations, self.linear_dim)
                self.linear_M1_2 = nn.Linear(self.linear_dim, self.n_locations)
            else:
                print('M1 is not used')
                self.M1 = None

            if M2 is not None:
                self.M2 = M2
                self.M2 = np.concatenate([M2, np.zeros((1,M2.shape[1]))], axis=0)
                self.linear_M2 = nn.Linear(self.n_locations, self.linear_dim)
                self.linear_M2_2 = nn.Linear(self.linear_dim, self.n_locations)
            else:
                print('M2 is not used')
                self.M2 = None


        def forward(self, x):
            device = next(self.parameters()).device
            batch_size = len(x)
            last_locs = x[:, -1].cpu().detach().numpy()
            
            x = super().forward_without_softmax(x)
            
            if self.M1 is not None:

                mat1 = self.M1[last_locs]
                mat1 = torch.Tensor(mat1).to(device)

                mat1 = F.relu(self.linear_M1(mat1))
                mat1 = torch.sigmoid(self.linear_M1_2(mat1))
                mat1 = F.normalize(mat1)
            
            else:
                mat1 = torch.zeros(x.shape).to(device)
            
            if self.M2 is not None:
                mat2 = self.M2[last_locs]
                mat2 = torch.Tensor(mat2).to(device)

                mat2 = F.relu(self.linear_M2(mat2))
                mat2 = torch.sigmoid(self.linear_M2_2(mat2))
                mat2 = F.normalize(mat2)
            else:
                mat2 = torch.zeros(x.shape).to(device)

            x = x + torch.mul(x,mat1) + torch.mul(x,mat2)
            x = F.log_softmax(x, dim=-1)
            return x
        
    return TransGeneratorWithAux


# TransGenerator = make_generator(Transformer)
# TimeTransGenerator = make_generator(TimeTransformer)
TransformerWithAux = add_aux(Transformer)
TimeTransformerWithAux = add_aux(TimeTransformer)