#!/usr/bin/env python3
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import datasets
import librosa
import numpy as np
import pandas as pd
import soundfile as sf
import torch
import torchaudio
import transformers
import wandb
from packaging import version
from sklearn.metrics import accuracy_score, f1_score
from transformers import (
    HfArgumentParser,
    Wav2Vec2FeatureExtractor,
    TrainingArguments,
    is_apex_available,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint, is_main_process

from ArgumentParser import ModelArguments, DataTrainingArguments
from DidTrainer import DidTrainer
from model_klaam import Wav2Vec2KlaamModel
from models import Wav2Vec2ClassificationModel
from processors import CustomWav2Vec2Processor

os.environ['WANDB_PROJECT'] = 'w2v_did'
os.environ['WANDB_LOG_MODEL'] = 'true'

if is_apex_available():
    pass

if version.parse(torch.__version__) >= version.parse("1.6"):
    _is_native_amp_available = True

logger = logging.getLogger(__name__)


@dataclass
class DataCollatorCTCWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor (:class:`~transformers.Wav2Vec2Processor`)
            The processor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        max_length (:obj:`int`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_length_labels (:obj:`int`, `optional`):
            Maximum length of the ``labels`` returned list and optionally padding length (see above).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    processor: CustomWav2Vec2Processor
    padding: Union[bool, str] = True
    max_length: Optional[int] = 160000
    max_length_labels: Optional[int] = None
    pad_to_multiple_of: Optional[int] = 160000
    pad_to_multiple_of_labels: Optional[int] = None
    number_of_labels: Optional[int] = 5

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lenghts and need
        # different padding methods

        input_features = [{"input_values": feature["input_values"]} for feature in features]

        def onehot(lbl):
            onehot = [0] * self.number_of_labels
            onehot[int(lbl)] = 1
            return onehot

        output_features = [onehot(feature["labels"]) for feature in features]

        batch = self.processor.pad(
            input_features,
            padding=True,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )
        # for val in batch['input_values']:
        #   print(val[:10])
        #   print(val[-10:])
        # print(batch['input_values'].shape)
        batch["labels"] = torch.tensor(output_features)
        # print(batch["labels"].argmax(-1))
        return batch


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        exit(1)

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

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Get the datasets:

    labels_csv = pd.read_csv(data_args.labels_csv)
    label_idx = []
    label_names = []
    for i in range(0, len(labels_csv)):
        label_idx.append(labels_csv.iloc[i, 0])
        label_names.append(labels_csv.iloc[i, 1])

    train_dataset = datasets.load_dataset("DidDataset.py", data_dir=data_args.data_path, split="train",
                                          data_files={'labels_csv': data_args.labels_csv},
                                          cache_dir=model_args.cache_dir)
    eval_dataset = datasets.load_dataset("DidDataset.py", data_dir=data_args.data_path, split="test",
                                         data_files={'labels_csv': data_args.labels_csv},
                                         cache_dir=model_args.cache_dir)

    feature_extractor = Wav2Vec2FeatureExtractor(
        feature_size=1, sampling_rate=16_000, padding_value=0.0, do_normalize=True, return_attention_mask=True
    )

    if model_args.model_name_or_path == "facebook/wav2vec2-large-xlsr-53":
        processor = CustomWav2Vec2Processor(feature_extractor=feature_extractor)
        model = Wav2Vec2ClassificationModel.from_pretrained(
            model_args.model_name_or_path,
            attention_dropout=0.01,
            hidden_dropout=0.01,
            feat_proj_dropout=0.0,
            mask_time_prob=0.05,
            layerdrop=0.01,
            gradient_checkpointing=True,
        )
        model.build_layers(window_length=data_args.window_length, output_size=len(label_idx))
    else:
        processor = CustomWav2Vec2Processor.from_pretrained(model_args.model_name_or_path)
        model = Wav2Vec2KlaamModel.from_pretrained(model_args.model_name_or_path).to("cuda")

    if model_args.freeze_feature_extractor:
        model.freeze_feature_extractor()

    if data_args.max_train_samples is not None:
        train_dataset = train_dataset.select(range(data_args.max_train_samples))

    if data_args.max_val_samples is not None:
        eval_dataset = eval_dataset.select(range(data_args.max_val_samples))

    # Preprocessing the datasets.
    # We need to read the audio files as arrays and tokenize the targets.
    def speech_file_to_array_fn(batch):
        start = 0
        stop = data_args.window_length
        srate = 16_000
        if batch["file"].endswith('.wav'):
            speech_array, sampling_rate = sf.read(batch["file"], start=start * srate, stop=stop * srate)
        elif batch["file"].endswith('.mp3'):
            speech_array, sampling_rate = torchaudio.load(batch["file"])
            speech_array = speech_array[0].numpy()[:stop * srate]
        batch["speech"] = librosa.resample(np.asarray(speech_array), sampling_rate, srate)
        batch["sampling_rate"] = srate
        batch["parent"] = batch["label"]
        return batch

    train_dataset = train_dataset.map(
        speech_file_to_array_fn,
        remove_columns=train_dataset.column_names,
        num_proc=data_args.preprocessing_num_workers,
    )
    eval_dataset = eval_dataset.map(
        speech_file_to_array_fn,
        remove_columns=eval_dataset.column_names,
        num_proc=data_args.preprocessing_num_workers,
    )

    def prepare_dataset(batch):
        # check that all files have the correct sampling rate
        assert (
                len(set(batch["sampling_rate"])) == 1
        ), f"Make sure all inputs have the same sampling rate of {processor.feature_extractor.sampling_rate}."
        batch["input_values"] = processor(batch["speech"], sampling_rate=batch["sampling_rate"][0]).input_values
        batch["labels"] = batch["parent"]
        return batch

    train_dataset = train_dataset.map(
        prepare_dataset,
        remove_columns=train_dataset.column_names,
        batch_size=training_args.per_device_train_batch_size,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
    )
    eval_dataset = eval_dataset.map(
        prepare_dataset,
        remove_columns=eval_dataset.column_names,
        batch_size=training_args.per_device_train_batch_size,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
    )

    from sklearn.metrics import classification_report, confusion_matrix

    def compute_metrics(pred):
        labels = pred.label_ids.argmax(-1)
        preds = pred.predictions.argmax(-1)
        acc = accuracy_score(labels, preds)
        f1 = f1_score(labels, preds, average='macro')
        report = classification_report(y_true=labels, y_pred=preds, labels=label_idx, target_names=label_names)
        matrix = confusion_matrix(y_true=labels, y_pred=preds)
        print(report)
        print(matrix)

        wandb.log(
            {"conf_mat": wandb.plot.confusion_matrix(probs=None, y_true=labels, preds=preds, class_names=label_names)})
        # wandb.sklearn.plot_confusion_matrix(labels, preds, label_names)
        # wandb.sklearn.plot_precision_recall(labels, preds)

        return {"accuracy": acc, "f1_score": f1}

    wandb.init(name=training_args.output_dir, config=training_args)

    # Data collator
    data_collator = DataCollatorCTCWithPadding(processor=processor,
                                               padding=True,
                                               max_length=(data_args.window_length * 16000),
                                               pad_to_multiple_of=(data_args.window_length * 16000),
                                               number_of_labels=len(label_idx))

    # Initialize our Trainer
    trainer = DidTrainer(
        model=model,
        data_collator=data_collator,
        args=training_args,
        compute_metrics=compute_metrics,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        tokenizer=processor.feature_extractor,
    )

    print("Window_lenght: " + str(data_args.window_length))
    # Training
    if training_args.do_train:
        if last_checkpoint is not None:
            checkpoint = last_checkpoint
        elif os.path.isdir(model_args.model_name_or_path):
            checkpoint = model_args.model_name_or_path
        else:
            checkpoint = None
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        trainer.save_model()

        # save the feature_extractor and the tokenizer
        if is_main_process(training_args.local_rank):
            processor.save_pretrained(training_args.output_dir)

        metrics = train_result.metrics
        max_train_samples = (
            data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
        )
        metrics["train_samples"] = min(max_train_samples, len(train_dataset))

        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

    # Evaluation
    results = {}
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate()
        max_val_samples = data_args.max_val_samples if data_args.max_val_samples is not None else len(eval_dataset)
        metrics["eval_samples"] = min(max_val_samples, len(eval_dataset))

        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    return results


if __name__ == "__main__":
    main()
