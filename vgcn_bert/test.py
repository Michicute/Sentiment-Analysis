# -*- coding: utf-8 -*-

# @author Zhibin.LU
# @website: https://github.com/Louis-udm

"""Train the vanilla vgcn_bert model"""


import argparse
import gc
import os
import pickle as pkl
import random
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

# use pytorch_pretrained_bert.modeling for huggingface transformers 0.6.2
from pytorch_pretrained_bert.optimization import BertAdam  # , warmup_linear
from pytorch_pretrained_bert.tokenization import BertTokenizer

# from tqdm import tqdm, trange
from sklearn.metrics import classification_report, f1_score
from torch.utils.data import DataLoader

from vgcn_bert.env_config import env_config
from vgcn_bert.models.vanilla_vgcn_bert import VanillaVGCNBert
from vgcn_bert.utils import *

# from transformers import BertTokenizer,AdamW


random.seed(env_config.GLOBAL_SEED)
np.random.seed(env_config.GLOBAL_SEED)
torch.manual_seed(env_config.GLOBAL_SEED)


cuda_yes = torch.cuda.is_available()
if cuda_yes:
    torch.cuda.manual_seed_all(44)
device = torch.device("cuda:0" if cuda_yes else "cpu")


"""
Configuration
"""

parser = argparse.ArgumentParser()
parser.add_argument("--ds", type=str, default="cola")
parser.add_argument("--load", type=int, default=0)
parser.add_argument("--sw", type=int, default="0")
parser.add_argument("--lr", type=float, default=1e-5)
parser.add_argument("--l2", type=float, default=0.01)
parser.add_argument("--model", type=str, default="Vanilla_VGCN_BERT")
parser.add_argument("--validate_program", action="store_true")
parser.add_argument("--number_test_case", type=int, default="1")

args = parser.parse_args()
cfg_model_type = args.model
cfg_stop_words = True if args.sw == 1 else False
will_train_mode_from_checkpoint = True if args.load == 1 else False
learning_rate0 = args.lr
l2_decay = args.l2

dataset_list = {"sst", "cola"}
# hate: 10k, mr: 6753, sst: 7792, r8: 5211

total_train_epochs = 9
dropout_rate = 0.2  # 0.5 # Dropout rate (1 - keep probability).
if args.ds == "sst":
    batch_size = 16  # 12
    learning_rate0 = 1e-5  # 2e-5
    # l2_decay = 0.001
    l2_decay = 0.01  # default
elif args.ds == "cola":
    batch_size = 16  # 12
    learning_rate0 = 8e-6  # 2e-5
    l2_decay = 0.01

MAX_SEQ_LENGTH = 200
gcn_embedding_dim = 1
gradient_accumulation_steps = 1
bert_model_scale = "bert-base-uncased"
if env_config.TRANSFORMERS_OFFLINE == 1:
    bert_model_scale = os.path.join(
        env_config.HUGGING_LOCAL_MODEL_FILES_PATH,
        f"hf-maintainers_{bert_model_scale}",
    )

do_lower_case = True
warmup_proportion = 0.1

data_dir = f"data/preprocessed/{args.ds}"
output_dir = "output/"
if not os.path.exists(output_dir):
    os.mkdir(output_dir)

perform_metrics_str = ["weighted avg", "f1-score"]

# cfg_add_linear_mapping_term=False
cfg_vocab_adj = "pmi"
# cfg_vocab_adj='all'
# cfg_vocab_adj='tf'
cfg_adj_npmi_threshold = 0.2
cfg_adj_tf_threshold = 0
classifier_act_func = nn.ReLU()

resample_train_set = False  # if mse and resample, then do resample
do_softmax_before_mse = True
cfg_loss_criterion = "cle"
model_file_4save = (
    cfg_model_type
    + str(gcn_embedding_dim)
    + "_model_"
    + args.ds
    + "_"
    + cfg_loss_criterion
    + "_"
    + "sw"
    + str(int(cfg_stop_words))
    + ".pt"
)

if args.validate_program:
    total_train_epochs = 1

print(cfg_model_type + " Start at:", time.asctime())
print(
    "\n----- Configure -----",
    f"\n  args.ds: {args.ds}",
    f"\n  stop_words: {cfg_stop_words}",
    f"\n  Learning_rate0: {learning_rate0}" f"\n  weight_decay: {l2_decay}",
    f"\n  Loss_criterion {cfg_loss_criterion}"
    f"\n  softmax_before_mse: {do_softmax_before_mse}",
    f"\n  Dropout: {dropout_rate}"
    f"\n  Run_adj: {cfg_vocab_adj}"
    f"\n  gcn_act_func: Relu",
    f"\n  MAX_SEQ_LENGTH: {MAX_SEQ_LENGTH}",  #'valid_data_taux',valid_data_taux
    f"\n  perform_metrics_str: {perform_metrics_str}",
    f"\n  model_file_4save: {model_file_4save}",
    f"\n  validate_program: {args.validate_program}",
)


"""
Prepare data set
Load vocabulary adjacent matrix
"""
print("\n----- Prepare data set -----")
print(
    "  Load/shuffle/seperate",
    args.ds,
    "dataset, and vocabulary graph adjacent matrix",
)

objects = []
names = [
    "labels",
    "train_y",
    "train_y_prob",
    "valid_y",
    "valid_y_prob",
    "test_y",
    "test_y_prob",
    "shuffled_clean_docs",
    "vocab_adj_tf",
    "vocab_adj_pmi",
    "vocab_map",
]
for i in range(len(names)):
    datafile = "./" + data_dir + "/data_%s.%s" % (args.ds, names[i])
    with open(datafile, "rb") as f:
        objects.append(pkl.load(f, encoding="latin1"))
(
    lables_list,
    train_y,
    train_y_prob,
    valid_y,
    valid_y_prob,
    test_y,
    test_y_prob,
    shuffled_clean_docs,
    gcn_vocab_adj_tf,
    gcn_vocab_adj,
    gcn_vocab_map,
) = tuple(objects)

label2idx = lables_list[0]
idx2label = lables_list[1]

y = np.hstack((train_y, valid_y, test_y))
y_prob = np.vstack((train_y_prob, valid_y_prob, test_y_prob))
print('\nInput comment: ', shuffled_clean_docs[0][0], '\n\n')
examples = []
for i, ts in enumerate(shuffled_clean_docs):
    ex = InputExample(i, ts.strip(), confidence=y_prob[i], label=y[i])
    examples.append(ex)

num_classes = len(label2idx)
gcn_vocab_size = len(gcn_vocab_map)
train_size = len(train_y)
valid_size = len(valid_y)
test_size = len(test_y)

indexs = np.arange(0, len(examples))
train_examples = [examples[i] for i in indexs[:train_size]]

valid_examples = [
    examples[i] for i in indexs[train_size : train_size + valid_size]
]

test_examples = [
    examples[i]
    for i in indexs[
        train_size + valid_size : train_size + valid_size + test_size
    ]
]
print(shuffled_clean_docs[train_size + valid_size : train_size + valid_size + test_size])

if cfg_adj_tf_threshold > 0:
    gcn_vocab_adj_tf.data *= gcn_vocab_adj_tf.data > cfg_adj_tf_threshold
    gcn_vocab_adj_tf.eliminate_zeros()
if cfg_adj_npmi_threshold > 0:
    gcn_vocab_adj.data *= gcn_vocab_adj.data > cfg_adj_npmi_threshold
    gcn_vocab_adj.eliminate_zeros()

if cfg_vocab_adj == "pmi":
    gcn_vocab_adj_list = [gcn_vocab_adj]
elif cfg_vocab_adj == "tf":
    gcn_vocab_adj_list = [gcn_vocab_adj_tf]
elif cfg_vocab_adj == "all":
    gcn_vocab_adj_list = [gcn_vocab_adj_tf, gcn_vocab_adj]

norm_gcn_vocab_adj_list = []
for i in range(len(gcn_vocab_adj_list)):
    adj = gcn_vocab_adj_list[i]  # .tocsr() #(lr是用非norm时的1/10)
    print(
        "  Zero ratio(?>66%%) for vocab adj %dth: %.8f"
        % (i, 100 * (1 - adj.count_nonzero() / (adj.shape[0] * adj.shape[1])))
    )
    adj = normalize_adj(adj)
    norm_gcn_vocab_adj_list.append(sparse_scipy2torch(adj.tocoo()).to(device))
gcn_adj_list = norm_gcn_vocab_adj_list


del gcn_vocab_adj_tf, gcn_vocab_adj, gcn_vocab_adj_list
gc.collect()

train_classes_num, train_classes_weight = get_class_count_and_weight(
    train_y, len(label2idx)
)
loss_weight = torch.tensor(train_classes_weight, dtype=torch.float).to(device)

tokenizer = BertTokenizer.from_pretrained(
    bert_model_scale, do_lower_case=do_lower_case
)


def get_pytorch_dataloader(
    examples,
    tokenizer,
    batch_size,
    shuffle_choice,
    classes_weight=None,
    total_resample_size=-1,
):
    ds = CorpusDataset(
        examples, tokenizer, gcn_vocab_map, MAX_SEQ_LENGTH, gcn_embedding_dim
    )
    if shuffle_choice == 0:  # shuffle==False
        return DataLoader(
            dataset=ds,
            batch_size=batch_size,
            shuffle=False,
            num_workers=4,
            collate_fn=ds.pad,
        )
    elif shuffle_choice == 1:  # shuffle==True
        return DataLoader(
            dataset=ds,
            batch_size=batch_size,
            shuffle=True,
            num_workers=4,
            collate_fn=ds.pad,
        )
    elif shuffle_choice == 2:  # weighted resampled
        assert classes_weight is not None
        assert total_resample_size > 0
        weights = [
            classes_weight[0]
            if label == 0
            else classes_weight[1]
            if label == 1
            else classes_weight[2]
            for _, _, _, _, label in dataset
        ]
        sampler = WeightedRandomSampler(
            weights, num_samples=total_resample_size, replacement=True
        )
        return DataLoader(
            dataset=ds,
            batch_size=batch_size,
            sampler=sampler,
            num_workers=4,
            collate_fn=ds.pad,
        )


# ds size=1 for validating the program
if args.validate_program:
    train_examples = [train_examples[0]]
    valid_examples = [valid_examples[0]]
    test_examples = [test_examples[0]]

train_dataloader = get_pytorch_dataloader(
    train_examples, tokenizer, batch_size, shuffle_choice=0
)
valid_dataloader = get_pytorch_dataloader(
    valid_examples, tokenizer, batch_size, shuffle_choice=0
)
test_dataloader = get_pytorch_dataloader(
    test_examples, tokenizer, batch_size, shuffle_choice=0
)


# total_train_steps = int(len(train_examples) / batch_size / gradient_accumulation_steps * total_train_epochs)
total_train_steps = int(
    len(train_dataloader) / gradient_accumulation_steps * total_train_epochs
)

print("  Train_classes count:", train_classes_num)
print(
    f"  Num examples for train = {len(train_examples)}",
    f", after weight sample: {len(train_dataloader) * batch_size}",
)
print("  Num examples for validate = %d" % len(valid_examples))
print("  Batch size = %d" % batch_size)
print("  Num steps = %d" % total_train_steps)


"""
Train vanilla_vgcn_bert model
"""


def evaluate(
    model, gcn_adj_list, predict_dataloader, batch_size, epoch_th, dataset_name
):
    # print("***** Running prediction *****")
    model.eval()
    predict_out = []
    all_label_ids = []
    ev_loss = 0
    total = 0
    correct = 0
    start = time.time()
    with torch.no_grad():
        for batch in predict_dataloader:
            batch = tuple(t.to(device) for t in batch)
            (
                input_ids,
                input_mask,
                segment_ids,
                y_prob,
                label_ids,
                gcn_swop_eye,
            ) = batch
            # the parameter label_ids is None, model return the prediction score
            logits = model(
                gcn_adj_list, gcn_swop_eye, input_ids, segment_ids, input_mask
            )

            if cfg_loss_criterion == "mse":
                if do_softmax_before_mse:
                    logits = F.softmax(logits, -1)
                loss = F.mse_loss(logits, y_prob)
            else:
                if loss_weight is None:
                    loss = F.cross_entropy(
                        logits.view(-1, num_classes), label_ids
                    )
                else:
                    loss = F.cross_entropy(
                        logits.view(-1, num_classes), label_ids
                    )
            # print(num_classes)
            _, predicted = torch.max(logits, -1)
            # print(predicted)
            predict_out.extend(predicted.tolist())
            all_label_ids.extend(label_ids.tolist())
            eval_accuracy = predicted.eq(label_ids)
            numpy_array = predicted.cpu().numpy()
            #print(eval_accuracy)
            break 
    return 0, 0, 0, numpy_array


print("\n----- Running testing -----")
if True:
    checkpoint = torch.load(
        "/content/drive/MyDrive/NLP/VGCN-BERT/VGCN-BERT/output/my_Vanilla_VGCN_BERT1_model_sst_cle_sw0.pt", map_location="cpu"
    )
    if "step" in checkpoint:
        prev_save_step = checkpoint["step"]
        start_epoch = checkpoint["epoch"]
    else:
        prev_save_step = -1
        start_epoch = checkpoint["epoch"] + 1
    start_epoch = checkpoint["epoch"] + 1
    valid_acc_prev = checkpoint["valid_acc"]
    perform_metrics_prev = checkpoint["perform_metrics"]
    model = VanillaVGCNBert.from_pretrained(
        bert_model_scale,
        state_dict=checkpoint["model_state"],
        gcn_adj_dim=gcn_vocab_size,
        gcn_adj_num=len(gcn_adj_list),
        gcn_embedding_dim=gcn_embedding_dim,
        num_labels=len(label2idx),
    )
    print(
        "Loaded the pretrain model:",
        "/content/drive/MyDrive/NLP/VGCN-BERT/VGCN-BERT/output/my_Vanilla_VGCN_BERT1_model_sst_cle_sw0.pt",
        ", epoch:",
        checkpoint["epoch"],
        "valid acc:",
        checkpoint["valid_acc"],
        " ".join(perform_metrics_str) + "_valid:",
        checkpoint["perform_metrics"],
    )

else:
    start_epoch = 0
    valid_acc_prev = 0
    perform_metrics_prev = 0
    model = VanillaVGCNBert.from_pretrained(
        bert_model_scale,
        gcn_adj_dim=gcn_vocab_size,
        gcn_adj_num=len(gcn_adj_list),
        gcn_embedding_dim=gcn_embedding_dim,
        num_labels=len(label2idx),
    )
    prev_save_step = -1

model.to(device)

optimizer = BertAdam(
    model.parameters(),
    lr=learning_rate0,
    warmup=warmup_proportion,
    t_total=total_train_steps,
    weight_decay=l2_decay,
)

train_start = time.time()
global_step_th = int(
    len(train_examples)
    / batch_size
    / gradient_accumulation_steps
    * start_epoch
)

if True:
    print(test_dataloader)
    _, _, _, arr = evaluate(
        model, gcn_adj_list, test_dataloader, batch_size, 0, "Test_set"
    )
    print('=======================RESULT======================')
    if arr[:args.number_test_case][0] == 1:
      print('Positive sentiment')
    elif arr[:args.number_test_case][0] == 0:
      print('Negative sentiment')
    print('===================================================')