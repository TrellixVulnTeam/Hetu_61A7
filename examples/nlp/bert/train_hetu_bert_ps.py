from tqdm import tqdm
import logging
import math
import os
import hetu as ht
from hetu_bert import BertForPreTraining
from bert_config import BertConfig
from load_data import DataLoaderForBertPretraining
import numpy as np
import time
import argparse


def pretrain(args):
    num_epochs = args.epochs
    lr = args.lr

    config = BertConfig(vocab_size=args.vocab_size,
                        hidden_size=args.hidden_size,
                        num_hidden_layers=args.num_hidden_layers,
                        num_attention_heads=args.num_attention_heads,
                        intermediate_size=args.hidden_size*4,
                        max_position_embeddings=args.seq_length,
                        attention_probs_dropout_prob=args.dropout_prob,
                        hidden_dropout_prob=args.dropout_prob,
                        batch_size=args.train_batch_size,
                        hidden_act=args.hidden_act)

    # Input data file names definition
    dict_seqlen2predlen = {128: 20, 512: 80}
    pred_len = dict_seqlen2predlen[config.max_position_embeddings]
    dataset = args.dataset
    if dataset not in ['wikicorpus_en', 'wiki_books']:
        raise (NotImplementedError)
    file_dir = './data/hdf5_lower_case_1_seq_len_128_max_pred_20_masked_lm_prob_0.15_random_seed_12345_dupe_factor_5/%s/' % dataset
    file_name_format = dataset + '_training_%d.hdf5'
    train_file_num = 2
    train_files = [file_dir + file_name_format %
                   file_id for file_id in range(train_file_num)]
    input_ids = np.zeros((0, 128))
    attention_mask = np.zeros((0, 128))
    token_type_ids = np.zeros((0, 128))
    masked_lm_labels = np.zeros((0, 128))
    next_sentence_label = np.zeros((0))
    loss_position_sum = np.zeros((0))

    for train_file in train_files:
        dataloader = DataLoaderForBertPretraining(
            train_file, config.batch_size, pred_len)
        batch_num = min(dataloader.batch_num, 100)
        for i in range(batch_num):
            start_time = time.time()
            batch_data = dataloader.get_batch(i)
            if i % 100 == 0:
                print(f'{train_file} processing {i}')
            input_ids = np.concatenate(
                (input_ids, batch_data['input_ids']), axis=0)
            token_type_ids = np.concatenate(
                (token_type_ids, batch_data['token_type_ids']), axis=0)
            attention_mask = np.concatenate(
                (attention_mask, batch_data['attention_mask']), axis=0)
            masked_lm_labels = np.concatenate(
                (masked_lm_labels, batch_data['masked_lm_labels']), axis=0)
            next_sentence_label = np.concatenate(
                (next_sentence_label, batch_data['next_sentence_label']), axis=0)
            loss_position_sum = np.concatenate((loss_position_sum,
                                                np.array([np.where(batch_data['masked_lm_labels'].reshape(-1) != -1)[0].shape[0]]).repeat(config.batch_size, axis=0)), axis=0)

    # Hetu model definition
    model = BertForPreTraining(config=config)
    input_ids = ht.dataloader_op([[input_ids, config.batch_size, 'train']])
    attention_mask = ht.dataloader_op(
        [[attention_mask, config.batch_size, 'train']])
    token_type_ids = ht.dataloader_op(
        [[token_type_ids, config.batch_size, 'train']])
    masked_lm_labels = ht.dataloader_op(
        [[masked_lm_labels, config.batch_size, 'train']])
    next_sentence_label = ht.dataloader_op(
        [[next_sentence_label, config.batch_size, 'train']])
    loss_position_sum = ht.dataloader_op(
        [[loss_position_sum, config.batch_size, 'train']])

    _, _, masked_lm_loss, next_sentence_loss = model(
        input_ids, token_type_ids, attention_mask, masked_lm_labels, next_sentence_label)
    loss_position_sum = ht.slice_op(loss_position_sum, [0], [1])
    masked_lm_loss_mean = ht.div_op(ht.reduce_sum_op(
        masked_lm_loss, [0, 1]), loss_position_sum)
    next_sentence_loss_mean = ht.reduce_mean_op(next_sentence_loss, [0])

    loss = masked_lm_loss_mean + next_sentence_loss_mean
    # opt = ht.optim.AdamOptimizer(learning_rate=lr, beta1=0.9, beta2=0.999, epsilon=1e-8, l2reg = args.adam_weight_decay)
    # opt = ht.optim.AdamOptimizer(learning_rate=lr, beta1=0.9, beta2=0.999, epsilon=1e-8)
    opt = ht.optim.SGDOptimizer(learning_rate=lr)
    train_op = opt.minimize(loss)

    eval_nodes = {'train': [masked_lm_loss_mean,
                            next_sentence_loss_mean, loss, train_op]}
    strategy = ht.dist.DataParallel(aggregate='PS')
    executor = ht.Executor(eval_nodes, dist_strategy=strategy, prefetch=False, dynamic_memory=True)

    global_step_num = 0
    for ep in range(num_epochs):
        step_num = 0
        batch_num = executor.get_batch_num('train')
        for i in range(batch_num):
            start_time = time.time()
            results = executor.run('train')

            masked_lm_loss_mean_out = results[0].asnumpy()
            next_sentence_loss_mean_out = results[1].asnumpy()
            loss_out = results[2].asnumpy()

            end_time = time.time()
            print('[Epoch %d] (Iteration %d): Loss = %.3f, MLM_loss = %.3f, NSP_loss = %.6f, Time = %.3f' % (
                ep, step_num, loss_out, masked_lm_loss_mean_out, next_sentence_loss_mean_out, end_time-start_time))
            step_num += 1
            global_step_num += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--train_batch_size", type=int, default=64, help="Training batch size"
    )
    parser.add_argument(
        "--dataset", type=str, default='wikicorpus_en', help="Dataset used to train."
    )
    parser.add_argument(
        "--vocab_size", type=int, default=30522, help="Total number of vocab"
    )
    parser.add_argument(
        "--hidden_size", type=int, default=768, help="Hidden size of transformer model",
    )
    parser.add_argument(
        "--num_hidden_layers", type=int, default=12, help="Number of layers"
    )
    parser.add_argument(
        "-a",
        "--num_attention_heads",
        type=int,
        default=12,
        help="Number of attention heads",
    )
    parser.add_argument(
        "-s", "--seq_length", type=int, default=128, help="Maximum sequence len"
    )
    parser.add_argument("-e", "--epochs", type=int,
                        default=10, help="Number of epochs")
    parser.add_argument("--lr", type=float, default=1e-5,
                        help="Learning rate of adam")
    parser.add_argument(
        "--adam_weight_decay", type=float, default=0.01, help="Weight_decay of adam"
    )
    parser.add_argument(
        "--hidden_act", type=str, default='gelu', help="Hidden activation to use."
    )
    parser.add_argument(
        "--dropout_prob", type=float, default=0.1, help="Dropout rate."
    )
    args = parser.parse_args()

    pretrain(args)
