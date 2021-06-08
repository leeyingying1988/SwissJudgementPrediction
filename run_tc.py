#!/usr/bin/env python
# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning multi-lingual models on SJP (e.g. Bert, DistilBERT, XLM).
    Adapted from `examples/text-classification/run_glue.py`"""
import faulthandler
import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, multilabel_confusion_matrix, \
    classification_report, confusion_matrix
from sklearn.preprocessing import MultiLabelBinarizer
from typing import Optional

import numpy as np
from datasets import load_dataset, load_metric

import transformers
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    EvalPrediction,
    HfArgumentParser,
    Trainer,
    TrainingArguments,
    default_data_collator,
    set_seed, EarlyStoppingCallback,
)
from transformers.trainer_utils import get_last_checkpoint, is_main_process
from transformers.utils import check_min_version

os.environ['TOKENIZERS_PARALLELISM'] = "True"
os.environ['WANDB_PROJECT'] = 'SwissJudgementPrediction'
os.environ['WANDB_MODE'] = "online"

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.6.0.dev0")

logger = logging.getLogger(__name__)

faulthandler.enable()


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.

    Using `HfArgumentParser` we can turn this class
    into argparse arguments to be able to specify them on
    the command line.
    """

    max_seq_length: Optional[int] = field(
        default=128,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
                    "than this will be truncated, sequences shorter will be padded."
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )
    pad_to_max_length: bool = field(
        default=True,
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
                    "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
                    "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
                    "value if set."
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of prediction examples to this "
                    "value if set."
        },
    )
    server_ip: Optional[str] = field(default=None, metadata={"help": "For distant debugging."})
    server_port: Optional[str] = field(default=None, metadata={"help": "For distant debugging."})


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        default=None, metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    language: str = field(
        default=None, metadata={"help": "Evaluation language. Also train language if `train_language` is set to None."}
    )
    train_language: Optional[str] = field(
        default=None, metadata={"help": "Train language if it is different from the evaluation language."}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    do_lower_case: Optional[bool] = field(
        default=False,
        metadata={"help": "arg to indicate if tokenizer should do lower case in AutoTokenizer.from_pretrained()"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
                    "with private models)."
        },
    )
    problem_type: str = field(
        default="single_label_classification",
        metadata={
            "help": "Problem type for XxxForSequenceClassification models. "
                    "Can be one of (\"regression\", \"single_label_classification\", \"multi_label_classification\")."
        },
    )
    prediction_threshold: int = field(
        default=0,
        metadata={
            "help": "Used in multilabel classification for determining when a given label is assigned. "
                    "This is normally 0 when using the tanh function in the output layer "
                    "and 0.5 if the sigmoid function is used."
                    "This is a hyperparameter which can additionally be tuned to improve the "
                    "multilabel classification performance as discussed here: "
                    "https://www.csie.ntu.edu.tw/~cjlin/papers/threshold.pdf"
        },
    )


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # for better charts when we have a group run with multiple seeds
    os.environ["WANDB_RUN_GROUP"] = training_args.run_name

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Setup distant debugging if needed
    if data_args.server_ip and data_args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(data_args.server_ip, data_args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.setLevel(logging.INFO if is_main_process(training_args.local_rank) else logging.WARN)

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )

    # Set the verbosity to info of the Transformers logger (on main process only):
    if is_main_process(training_args.local_rank):
        transformers.utils.logging.set_verbosity_info()
        transformers.utils.logging.enable_default_handler()
        transformers.utils.logging.enable_explicit_format()
    logger.info(f"Training/evaluation parameters {training_args}")

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if training_args.do_train:
        train_dataset = load_dataset("csv", data_files={"train": 'data/train.csv'})['train']

    if training_args.do_eval:
        eval_dataset = load_dataset("csv", data_files={"validation": 'data/val.csv'})['validation']

    if training_args.do_predict:
        predict_dataset = load_dataset("csv", data_files={"test": 'data/test.csv'})['test']

    # Labels
    with open('data/labels.json', 'r') as f:
        label_dict = json.load(f)
        label_dict['id2label'] = {int(k): v for k, v in label_dict['id2label'].items()}
        label_dict['label2id'] = {k: int(v) for k, v in label_dict['label2id'].items()}
        label_list = list(label_dict["label2id"].keys())
    num_labels = len(label_list)

    if model_args.problem_type == 'multi_label_classification':
        mlb = MultiLabelBinarizer().fit([label_list])

    # Load pretrained model and tokenizer
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        num_labels=num_labels,
        id2label=label_dict["id2label"],
        label2id=label_dict["label2id"],
        finetuning_task="text-classification",
        problem_type=model_args.problem_type,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        do_lower_case=model_args.do_lower_case,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        model_args.model_name_or_path,
        from_tf=bool(".ckpt" in model_args.model_name_or_path),
        config=config,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )

    # Preprocessing the datasets
    # Padding strategy
    if data_args.pad_to_max_length:
        padding = "max_length"
    else:
        # We will pad later, dynamically at batch creation, to the max sequence length in each batch
        padding = False

    def preprocess_function(batch):
        # Tokenize the texts
        tokenized = tokenizer(batch["text"], padding=padding, max_length=data_args.max_seq_length, truncation=True, )

        # Map labels to IDs
        if model_args.problem_type == 'multi_label_classification':
            tokenized["label"] = [mlb.transform([eval(labels)])[0] for labels in batch["label"]]
        if model_args.problem_type == 'single_label_classification':
            if label_dict["label2id"] is not None and "label" in batch:
                tokenized["label"] = [label_dict["label2id"][l] for l in batch["label"]]
        return tokenized

    if training_args.do_train:
        if data_args.max_train_samples is not None:
            train_dataset = train_dataset.select(range(data_args.max_train_samples))
        train_dataset = train_dataset.map(
            preprocess_function,
            batched=True,
            load_from_cache_file=not data_args.overwrite_cache,
            remove_columns=train_dataset.column_names,
        )
        # Log a random sample from the training set:
        for index in random.sample(range(len(train_dataset)), 1):
            logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    if training_args.do_eval:
        if data_args.max_eval_samples is not None:
            eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))
        eval_dataset = eval_dataset.map(
            preprocess_function,
            batched=True,
            load_from_cache_file=not data_args.overwrite_cache,
            remove_columns=eval_dataset.column_names,
        )

    if training_args.do_predict:
        if data_args.max_predict_samples is not None:
            predict_dataset = predict_dataset.select(range(data_args.max_predict_samples))
        predict_dataset = predict_dataset.map(
            preprocess_function,
            batched=True,
            load_from_cache_file=not data_args.overwrite_cache,
            remove_columns=predict_dataset.column_names,
        )

    def labels_to_bools(labels):
        return [tl == 1 for tl in labels]

    def preds_to_bools(preds):
        return [pl > model_args.prediction_threshold for pl in preds]

    # You can define your custom compute_metrics function. It takes an `EvalPrediction` object (a namedtuple with a
    # predictions and label_ids field) and has to return a dictionary string to float.
    def compute_metrics(p: EvalPrediction):
        labels = p.label_ids
        preds = p.predictions[0] if isinstance(p.predictions, tuple) else p.predictions
        if model_args.problem_type == 'multi_label_classification':
            # for multi_label_classification we need boolean arrays for each example
            labels = labels_to_bools(labels)
            preds = preds_to_bools(preds)
        if model_args.problem_type == 'single_label_classification':
            preds = np.argmax(preds, axis=1)

        accuracy = accuracy_score(labels, preds)
        # weighted averaging is a better evaluation metric for imbalanced label distributions
        precision, recall, f1_score, _ = precision_recall_fscore_support(labels, preds, average='weighted')
        return {
            'accuracy': accuracy,
            'precision': precision,
            'recall': recall,
            'f1_score': f1_score,
        }

    # Data collator will default to DataCollatorWithPadding, so we change it if we already did the padding.
    if data_args.pad_to_max_length:
        data_collator = default_data_collator
    elif training_args.fp16:
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8)
    else:
        data_collator = None

    # Initialize our Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        compute_metrics=compute_metrics,
        tokenizer=tokenizer,
        data_collator=data_collator,
        # callbacks=[EarlyStoppingCallback(early_stopping_patience=3)],
    )

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        metrics = train_result.metrics
        max_train_samples = (
            data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
        )
        metrics["train_samples"] = min(max_train_samples, len(train_dataset))

        trainer.save_model()  # Saves the tokenizer too for easy upload

        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

    # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate(eval_dataset=eval_dataset)

        max_eval_samples = data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
        metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))

        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    # Prediction
    if training_args.do_predict:
        logger.info("*** Predict ***")
        preds, labels, metrics = trainer.predict(predict_dataset, metric_key_prefix="predict")

        max_predict_samples = (
            data_args.max_predict_samples if data_args.max_predict_samples is not None else len(predict_dataset)
        )
        metrics["predict_samples"] = min(max_predict_samples, len(predict_dataset))

        trainer.log_metrics("predict", metrics)
        trainer.save_metrics("predict", metrics)

        if model_args.problem_type == 'multi_label_classification':
            preds, labels = preds_to_bools(preds), labels_to_bools(labels)
        if model_args.problem_type == 'single_label_classification':
            preds = np.argmax(preds, axis=1)
        output_predict_file = os.path.join(training_args.output_dir, "predictions.txt")
        output_report_file = os.path.join(training_args.output_dir, "prediction_report.txt")
        if trainer.is_world_process_zero():
            # write predictions file
            with open(output_predict_file, "w") as writer:
                writer.write("index\tprediction\n")
                for index, pred in enumerate(preds):
                    if model_args.problem_type == 'multi_label_classification':
                        pred_strings = mlb.inverse_transform(np.array([pred]))[0]
                    if model_args.problem_type == 'single_label_classification':
                        pred_strings = [label_dict["id2label"][pred]]
                    writer.write(f"{index}\t{pred_strings}\n")

            # write report file
            with open(output_report_file, "w") as writer:
                if model_args.problem_type == 'multi_label_classification':
                    title = "Multilabel Confusion Matrix\n"
                    matrices = multilabel_confusion_matrix(labels, preds)
                if model_args.problem_type == 'single_label_classification':
                    title = "Singlelabel Confusion Matrix\n"
                    matrices = confusion_matrix(labels, preds)

                writer.write(title)
                writer.write("=" * 75 + "\n\n")
                writer.write("reading help:\nTN FP\nFN TP\n\n")
                for i in range(len(matrices)):
                    writer.write(f"{label_list[i]}\n{str(matrices[i])}\n")
                writer.write("\n" * 3)

                writer.write("Classification Report\n")
                writer.write("=" * 75 + "\n\n")
                report = classification_report(labels, preds, target_names=label_list)
                writer.write(str(report))


if __name__ == "__main__":
    main()