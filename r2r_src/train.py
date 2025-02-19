import torch

import os
import sys
import time
import json
import random
import numpy as np
from collections import defaultdict

from utils import read_vocab, write_vocab, build_vocab, padding_idx, timeSince, read_img_features, print_progress
import utils
from env import R2RBatch
from agent import Seq2SeqAgent
from eval import Evaluation, format_results
from param import args

import warnings
warnings.filterwarnings("ignore")
from tensorboardX import SummaryWriter

from vlnbert.vlnbert_init import get_tokenizer

log_dir = 'snap/%s' % args.name
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

IMAGENET_FEATURES = 'img_features/ResNet-152-imagenet.tsv'
PLACE365_FEATURES = 'img_features/ResNet-152-places365.tsv'
CLIP_ResNet504_FEATURES = 'img_features/CLIP-ResNet-50x4-views.tsv'

if args.features == 'imagenet':
    features = IMAGENET_FEATURES
elif args.features == 'places365':
    features = PLACE365_FEATURES
elif args.features == "clip_resnet504":
    features = CLIP_ResNet504_FEATURES

feedback_method = args.feedback  # teacher or sample

print(args); print('')


''' train the listener '''
def train(train_env, tok, n_iters, log_every=2000, val_envs={}, val_env_names=['val_seen','val_unseen'], aug_env=None):
    writer = SummaryWriter(log_dir=log_dir)
    listner = Seq2SeqAgent(train_env, "", tok, args.maxAction, seed=args.seed)

    record_file = open('./logs/' + args.name + '.txt', 'a')
    record_file.write(str(args) + '\n\n')
    record_file.close()

    start_iter = 0
    if args.load is not None:
        if args.aug is None:
            start_iter = listner.load(os.path.join(args.load))
            print("\nLOAD the model from {}, iteration ".format(args.load, start_iter))
        else:
            load_iter = listner.load(os.path.join(args.load))
            print("\nLOAD the model from {}, iteration ".format(args.load, load_iter))

    start = time.time()
    print('\nListener training starts, start iteration: %s' % str(start_iter))

    # best_val = {'val_unseen': {"spl": 0., "sr": 0., "state":"", 'update':False}, 'val_seen': {"spl": 0., "sr": 0., "state":"", 'update':False}}
    best_val = {val_name: {"spl": 0., "sr": 0., "state": "", 'update': False} for val_name in val_env_names}
    # print("best_val: ", best_val)

    for idx in range(start_iter, start_iter+n_iters, log_every):
        listner.logs = defaultdict(list)
        interval = min(log_every, n_iters-idx)
        iter = idx + interval

        # Train for log_every interval
        if aug_env is None:
            listner.env = train_env
            listner.train(interval, feedback=feedback_method)  # Train interval iters
        else:
            jdx_length = len(range(interval // 2))
            for jdx in range(interval // 2):
                # Train with GT data
                listner.env = train_env
                args.ml_weight = 0.2
                listner.train(1, feedback=feedback_method)

                # Train with Augmented data
                listner.env = aug_env
                args.ml_weight = 0.2
                listner.train(1, feedback=feedback_method)

                print_progress(jdx, jdx_length, prefix='Progress:', suffix='Complete', bar_length=50)

        # Log the training stats to tensorboard
        total = max(sum(listner.logs['total']), 1)
        length = max(len(listner.logs['critic_loss']), 1)
        critic_loss = sum(listner.logs['critic_loss']) / total
        RL_loss = sum(listner.logs['RL_loss']) / max(len(listner.logs['RL_loss']), 1)
        IL_loss = sum(listner.logs['IL_loss']) / max(len(listner.logs['IL_loss']), 1)
        entropy = sum(listner.logs['entropy']) / total
        writer.add_scalar("loss/critic", critic_loss, idx)
        writer.add_scalar("policy_entropy", entropy, idx)
        writer.add_scalar("loss/RL_loss", RL_loss, idx)
        writer.add_scalar("loss/IL_loss", IL_loss, idx)
        writer.add_scalar("total_actions", total, idx)
        writer.add_scalar("max_length", length, idx)
        # print("total_actions", total, ", max_length", length)

        # Run validation
        loss_str = "iter {}".format(iter)
        for env_name, (env, evaluator) in val_envs.items():
            listner.env = env

            # Get validation distance from goal under test evaluation conditions
            listner.test(use_dropout=False, feedback='argmax', iters=None)
            result = listner.get_results()
            score_summary, _ = evaluator.score(result)
            loss_str += ", %s " % env_name
            val = score_summary['spl']
            writer.add_scalar("spl/%s" % env_name, val, idx)
            if env_name in best_val:
                if val > best_val[env_name]['spl']:
                    best_val[env_name]['spl'] = val
                    best_val[env_name]['update'] = True
                elif (val == best_val[env_name]['spl']) and (score_summary['success_rate'] > best_val[env_name]['sr']):
                    best_val[env_name]['spl'] = val
                    best_val[env_name]['update'] = True
            loss_str += format_results(score_summary)

        record_file = open('./logs/' + args.name + '.txt', 'a')
        record_file.write(loss_str + '\n')
        record_file.close()

        for env_name in best_val:
            if best_val[env_name]['update']:
                best_val[env_name]['state'] = 'Iter %d %s' % (iter, loss_str)
                best_val[env_name]['update'] = False
                listner.save(idx, os.path.join("snap", args.name, "state_dict", "best_%s" % (env_name)))
            else:
                listner.save(idx, os.path.join("snap", args.name, "state_dict", "latest_dict"))

        print(('%s (%d %d%%) %s' % (timeSince(start, float(iter)/n_iters),
                                             iter, float(iter)/n_iters*100, loss_str)))

        if iter % log_every == 0:  # 1000
            print("BEST RESULT TILL NOW")
            for env_name in best_val:
                print(env_name, best_val[env_name]['state'])

                record_file = open('./logs/' + args.name + '.txt', 'a')
                record_file.write('BEST RESULT TILL NOW: ' + env_name + ' | ' + best_val[env_name]['state'] + '\n')
                record_file.close()

    listner.save(idx, os.path.join("snap", args.name, "state_dict", "LAST_iter%d" % (idx)))


def valid(train_env, tok, val_envs={}):
    agent = Seq2SeqAgent(train_env, "", tok, args.maxAction, seed=args.seed)

    print("Loaded the listener model at iter %d from %s" % (agent.load(args.load), args.load))

    for env_name, (env, evaluator) in val_envs.items():
        agent.logs = defaultdict(list)
        agent.env = env

        iters = None
        agent.test(use_dropout=False, feedback='argmax', iters=iters)
        result = agent.get_results()

        if env_name != '':
            score_summary, _ = evaluator.score(result)
            loss_str = "Env name: %s, " % env_name
            loss_str += format_results(score_summary)
            print(loss_str)

        if args.submit:
            json.dump(
                result,
                open(os.path.join(log_dir, "submit_%s.json" % env_name), 'w'),
                sort_keys=True, indent=4, separators=(',', ': ')
            )


def evaluate_with_outputs(train_env, tok, val_envs={}):
    agent = Seq2SeqAgent(train_env, "", tok, args.maxAction, seed=args.seed)
    feedback = args.decode_feedback
    sample_size = 10

    print("Loaded the listener model at iter %d from %s" % (agent.load(args.load), args.load))
    print("Feedback method: ", feedback)
    print("Sample size: ", sample_size)

    for env_name, (env, evaluator) in val_envs.items():
        agent.logs = defaultdict(list)
        agent.env = env

        if feedback == 'argmax':
            iters = None
            agent.test(use_dropout=False, feedback=feedback, iters=iters)
            result = agent.get_results()

            score_summary, all_preds = evaluator.score(result)
            loss_str = "Env name: %s, " % env_name
            loss_str += format_results(score_summary)
            print(loss_str)

        elif feedback == 'sample':
            for k in range(sample_size):
                iters = None
                agent.test(use_dropout=False, feedback=feedback, iters=iters)
                result = agent.get_results()

                score_summary, all_preds = evaluator.score(result, sample_idx=k)
                evaluator.gt = all_preds
                loss_str = "Env name: %s, " % env_name
                loss_str += "Sample index: %s, " % str(k)
                loss_str += format_results(score_summary)
                print(loss_str)

        else:
            print("Unknown decode feedback method: ", feedback)
            sys.exit()

        if args.submit:
            filename = os.path.basename(env_name).split(".")[0]
            file_path = os.path.join(log_dir, "%s.json" % filename)
            with open(file_path, 'w') as f:
                json.dump(all_preds, f, indent=2)

            print("Saved eval info to ", file_path)


def setup(seed):
    print("Random seed: ", seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)

def train_val(test_only=False):
    ''' Train on the training set, and validate on seen and unseen splits. '''
    setup(args.seed)
    tok = get_tokenizer(args)

    feat_dict = read_img_features(features, args.feature_size, test_only=test_only)

    if test_only:
        featurized_scans = None
        val_env_names = ['val_train_seen']
    else:
        featurized_scans = set([key.split("_")[0] for key in list(feat_dict.keys())])
        #val_env_names = ['val_train_seen', 'val_seen', 'val_unseen']
        val_env_names = ['val_seen', 'val_unseen']

    if args.train == "finetune_listener_outputs":
        train_speaker_outputs = True
        train_env_name = args.train_speaker_output_files
        print("train data: \n", args.train_speaker_output_files)
        speaker_outputs = True
        val_env_names = args.speaker_output_files
        print("val data: \n", args.speaker_output_files)
    elif args.train == "eval_listener_outputs":
        train_speaker_outputs = False
        train_env_name = ['train']
        speaker_outputs = True
        val_env_names = args.speaker_output_files
        print("eval data: \n", args.speaker_output_files)
    else:
        train_speaker_outputs = False
        train_env_name = ['train']
        speaker_outputs = False

    train_env = R2RBatch(feat_dict, batch_size=args.batchSize, splits=train_env_name, tokenizer=tok, name='train',
                         speaker_outputs=train_speaker_outputs)

    from collections import OrderedDict

    #if args.submit:
    #    val_env_names.append('test')
    #else:
    #    pass

    # if args.train == "eval_listener_outputs":
    #     speaker_outputs = True
    #     val_env_names = args.speaker_output_files
    #     print(args.speaker_output_files)
    # else:
    #     speaker_outputs = False

    val_envs = OrderedDict(
        ((split,
          (R2RBatch(feat_dict, batch_size=args.batchSize, splits=[split], tokenizer=tok, speaker_outputs=speaker_outputs),
           Evaluation([split], featurized_scans, tok, speaker_outputs=speaker_outputs))
          )
         for split in val_env_names
         )
    )

    if args.train == 'listener' or args.train == "finetune_listener_outputs":
        train(train_env, tok, args.iters, val_envs=val_envs, val_env_names=val_env_names)
    #elif args.train == "finetune_listener_outputs":
    #    train(train_env, tok, args.iters, val_envs=val_envs, val_env_names=val_env_names, log_every=500)
    elif args.train == 'validlistener':
        valid(train_env, tok, val_envs=val_envs)
    elif args.train == "eval_listener_outputs":
        evaluate_with_outputs(train_env, tok, val_envs=val_envs)
    else:
        assert False

def train_val_augment(test_only=False):
    """
    Train the listener with the augmented data
    """
    setup(args.seed)

    # Create a batch training environment that will also preprocess text
    tok_bert = get_tokenizer(args)

    # Load the env img features
    feat_dict = read_img_features(features, args.feature_size, test_only=test_only)

    if test_only:
        featurized_scans = None
        val_env_names = ['val_train_seen']
    else:
        featurized_scans = set([key.split("_")[0] for key in list(feat_dict.keys())])
        val_env_names = ['val_train_seen', 'val_seen', 'val_unseen']

    # Load the augmentation data
    aug_path = args.aug
    # Create the training environment
    train_env = R2RBatch(feat_dict, batch_size=args.batchSize, splits=['train'], tokenizer=tok_bert, name='train')
    aug_env   = R2RBatch(feat_dict, batch_size=args.batchSize, splits=[aug_path], tokenizer=tok_bert, name='aug')

    # Setup the validation data
    val_envs = {split: (R2RBatch(feat_dict, batch_size=args.batchSize, splits=[split], tokenizer=tok_bert),
                Evaluation([split], featurized_scans, tok_bert))
                for split in val_env_names}

    # Start training
    train(train_env, tok_bert, args.iters, val_envs=val_envs, aug_env=aug_env)


if __name__ == "__main__":
    if args.train in ['listener', 'validlistener', 'eval_listener_outputs', 'finetune_listener_outputs']:
        train_val(test_only=args.test_only)
    elif args.train == 'auglistener':
        train_val_augment(test_only=args.test_only)
    else:
        assert False
