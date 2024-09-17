import logging
import numpy as np
import os
import sys
import functools

import datasets
import torch
import transformers
from timeit import default_timer as timer

import torch_xla.runtime as xr
import torch_xla.core.xla_model as xm
import torch_xla.debug.metrics as met
import torch_xla.distributed.spmd as xs
import torch_xla.distributed.parallel_loader as pl
import torch_xla.debug.profiler as xp
import torch_xla.debug.metrics as met

from dataclasses import dataclass, field
from itertools import chain
from typing import Optional, Union, List, Dict, Tuple

from datasets import load_dataset
from torch import nn
from torch.optim import AdamW
from torch.utils.data import DataLoader, Dataset, IterableDataset, RandomSampler, SequentialSampler
from torch_xla.experimental.spmd_fully_sharded_data_parallel import SpmdFullyShardedDataParallel as FSDPv2
from torch_xla.distributed.fsdp.wrap import (
    size_based_auto_wrap_policy,
    transformer_auto_wrap_policy,
)
from torch_xla.distributed.fsdp import checkpoint_module
from torch_xla.amp.syncfree import AdamW as AdamWXLA
from torch_xla.distributed.fsdp.utils import apply_xla_patch_to_nn_linear

from transformers import (
    CONFIG_MAPPING,
    AutoConfig,
    AutoModelForMaskedLM,
    LlamaForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    HfArgumentParser,
    TrainingArguments,
    is_torch_xla_available,
    set_seed,
    get_scheduler,
    SchedulerType,
    default_data_collator
)
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version
from transformers.trainer_pt_utils import get_module_class_from_name
from transformers.modeling_outputs import CausalLMOutputWithPast, MaskedLMOutput, BaseModelOutputWithPastAndCrossAttentions

check_min_version("4.39.3")
logger = logging.getLogger(__name__)

xr.use_spmd()
assert xr.is_spmd() == True


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    model_id: Optional[str] = field(
        default="meta-llama/Llama-2-7b-hf", metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Name of the tokenizer if different from model_id"}
    )
    torch_dtype: Optional[str] = field(
        default="bfloat16",
        metadata={
            "help": (
                "Override the default `torch.dtype` and load the model under this dtype. If `auto` is passed, the "
                "dtype will be automatically derived from the model's weights."
            ),
            "choices": ["auto", "bfloat16", "float16", "float32"],
        },
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={
            "help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )


@dataclass
class MoreTrainingArguments(TrainingArguments):
    profile_step: Optional[int] = field(
        default=-1, metadata={"help": "Step to profile"}
    )
    profile_logdir: Optional[str] = field(
        default=".", metadata={"help": "Directory to store the profile"}
    )
    profile_duration: Optional[int] = field(
        default="20000", metadata={"help": "Duration (ms) to capture profile"}
    )


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    block_size: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Optional input sequence length after tokenization. "
                "The training dataset will be truncated in block of this size for training. "
                "Default to the model max input length for single sentence inputs (take into account special tokens)."
            )
        },
    )

    def __post_init__(self):
        if self.dataset_name is None and self.train_file is None and self.validation_file is None:
            raise ValueError("Need either a dataset name or a training/validation file.")


class Trainer:
    """The trainer."""

    def __init__(
        self,
        model: nn.Module,
        args: MoreTrainingArguments,
        train_dataset: Optional[Union[Dataset, IterableDataset]],
        optimizers: Tuple[torch.optim.Optimizer,
                          torch.optim.lr_scheduler.LambdaLR],
    ):
        self.args = args
        self.optimizer, self.lr_scheduler = optimizers
        self.model = model
        self._check_model_optimizer_placement(self.model, self.optimizer)
        self.device = xm.xla_device()
        self.train_batch_size = args.per_device_train_batch_size
        self.train_dataset = train_dataset

        self.use_fsdp = True if args.fsdp else False

        # Set up SPMD mesh
        num_devices = xr.global_runtime_device_count()
        xs.set_global_mesh(xs.Mesh(np.array(range(num_devices)),
                           (num_devices, 1), axis_names=("fsdp", "tensor")))
        self.input_sharding_spec = xs.ShardingSpec(
            xs.get_global_mesh(), ("fsdp", None))
        logger.info(f"Logical mesh shape: {xs.get_global_mesh().shape()}")
        logger.info(f"Input sharding: {self.input_sharding_spec}")

    def _check_model_optimizer_placement(self, model, optimizer):
        for param in model.parameters():
            model_device = param.device
            break
        for param_group in optimizer.param_groups:
            if len(param_group["params"]) > 0:
                optimizer_device = param_group["params"][0].device
                break
        if model_device != optimizer_device:
            raise ValueError(
                "The model and the optimizer parameters are not on the same device."
            )

    def _get_train_dataloader(self):
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")

        dataloader = DataLoader(
            self.train_dataset,
            # Data collator will default to DataCollatorWithPadding, so we change it.
            collate_fn=default_data_collator,
            batch_size=self.train_batch_size,
            num_workers=self.args.dataloader_num_workers,
            pin_memory=self.args.dataloader_pin_memory,
            drop_last=self.args.dataloader_drop_last
        )
        loader = pl.MpDeviceLoader(dataloader,
                                   self.device,
                                   input_sharding=self.input_sharding_spec,
                                   loader_prefetch_size=self.train_batch_size,
                                   device_prefetch_size=4)
        return loader

    def _wrap_model(self, model):

        if self.use_fsdp:
            auto_wrap_policy = None
            auto_wrapper_callable = None
            default_transformer_cls_names_to_wrap = getattr(
                model, "_no_split_modules", None)
            default_transformer_cls_names_to_wrap = None
            fsdp_transformer_layer_cls_to_wrap = self.args.fsdp_config.get(
                "transformer_layer_cls_to_wrap", default_transformer_cls_names_to_wrap
            )
            if self.args.fsdp_config["min_num_params"] > 0:
                auto_wrap_policy = functools.partial(
                    size_based_auto_wrap_policy, min_num_params=self.args.fsdp_config[
                        "min_num_params"]
                )
            elif fsdp_transformer_layer_cls_to_wrap is not None:
                transformer_cls_to_wrap = set()
                for layer_class in fsdp_transformer_layer_cls_to_wrap:
                    transformer_cls = get_module_class_from_name(
                        model, layer_class)
                    if transformer_cls is None:
                        raise Exception(
                            "Could not find the transformer layer class to wrap in the model.")
                    else:
                        transformer_cls_to_wrap.add(transformer_cls)
                logger.info(
                    f"Llama classes to wrap: {transformer_cls_to_wrap}")
                auto_wrap_policy = functools.partial(
                    transformer_auto_wrap_policy,
                    # Transformer layer class to wrap
                    transformer_layer_cls=transformer_cls_to_wrap,
                )

            if self.args.fsdp_config["xla_fsdp_grad_ckpt"]:
                # Apply gradient checkpointing to auto-wrapped sub-modules if specified
                logger.info("Enabling gradient checkpointing")

                def auto_wrapper_callable(m, *args, **kwargs):
                    target_cls = FSDPv2
                    return target_cls(checkpoint_module(m), *args, **kwargs)

            def shard_output(output, mesh):
                real_output = None
                if isinstance(output, torch.Tensor):
                    real_output = output
                elif isinstance(output, tuple):
                    real_output = output[0]
                elif isinstance(output, CausalLMOutputWithPast):
                    real_output = output.logits
                elif isinstance(output, MaskedLMOutput):
                    real_output = output.logits
                if real_output is None:
                    raise ValueError(
                        "Something went wrong, the output of the model shouldn't be `None`")
                xs.mark_sharding(real_output, mesh, ("fsdp", None, None))

            model = FSDPv2(
                model,
                shard_output=shard_output,
                auto_wrap_policy=auto_wrap_policy,
                auto_wrapper_callable=auto_wrapper_callable,
            )

            def patched_optimizer_step(optimizer, barrier=False, optimizer_args={}):
                loss = optimizer.step(**optimizer_args)
                if barrier:
                    xm.mark_step()
                return loss

            xm.optimizer_step = patched_optimizer_step
        else:
            logger.info("Using DDP. Model not wrapped")

        return model

    def _log_metrics(self, step, start_time, loss,  sample_count):
        xm.mark_step()
        loss = loss.item()
        now = timer()
        elapsed_time = now - start_time
        samples_per_sec = sample_count / elapsed_time
        logger.info(
            f"Step: {step}, loss: {loss:0.4f}, Step time: {elapsed_time:0.2f} Samples: {sample_count} Samples/sec: {samples_per_sec:0.4f}"
        )
        self.run_history["step_history"].append(
            {
                "step": step,
                "loss": loss,
                "elapsed_time": elapsed_time,
                "sample_count": sample_count,
            }
        )

    def _save_checkpoint(self):
        pass

    def train_loop(self):
        self.model.train()
        self.model.zero_grad()
        # TBD restart from a given step. May skip the x number of batches
        # For now we assume that we wil never train for mor than one epoch
        start_step = 1
        max_step = self.args.max_steps
        train_loader = self._get_train_dataloader()
        train_iterator = iter(train_loader)
        model = self._wrap_model(self.model)

        logger.info("Starting training")
        logger.info(f"    Using {'FSDP' if self.use_fsdp else 'DDP'}")
        logger.info(f"    Start step: {start_step}")
        logger.info(f"    Max step: {max_step}")
        logger.info(f"    Global batch size: {self.train_batch_size}")

        self.run_history = {
            "step_history": [],
            "elapsed_time": 0.0
        }
        sample_count = self.train_batch_size * self.args.logging_steps
        total_steps = 0
        start_time = timer()
        adjusted_total_steps = -10
        for step in range(start_step, max_step + 1):
            try:
                batch = next(train_iterator)
            except StopIteration:
                break

            if adjusted_total_steps == 0:
                adjusted_start_time = timer()

            outputs = model(**batch)
            loss = outputs.loss
            loss.backward()
            self.optimizer.step()
            self.lr_scheduler.step()
            model.zero_grad()

            if step % self.args.logging_steps == 0:
                # xm.add_step_closure(
                #    self._log_metrics,
                #    args=(step, start_time, loss, sample_count),
                #    run_async=False,
                # )
                self._log_metrics(step, start_time, loss, sample_count)
                start_time = timer()
            total_steps += 1
            adjusted_total_steps += 1

            # Capture profile at the prefer step
            if step == self.args.profile_step:
                # Wait until device execution catches up to tracing before triggering the profile. This will
                # interrupt training slightly on the hosts which are capturing, but by waiting after tracing
                # for the step, the interruption will be minimal.
                xm.wait_device_ops()
                xp.trace_detached('127.0.0.1:9012', self.args.profile_logdir, self.args.profile_duration)

        adjusted_elapsed_time = timer() - adjusted_start_time

        logger.info("Finished training run")
        logger.info(self.run_history)

        logger.info("Performance summary")
        logger.info(f"  Number of steps: {adjusted_total_steps}")
        logger.info(f"  Elapsed time: {adjusted_elapsed_time:0.2f}")
        logger.info(
            f"  Steps per second: {adjusted_total_steps/adjusted_elapsed_time:0.2f}")


def main():
    # Parse CLI arguments
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, MoreTrainingArguments))

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Configure logging
    if training_args.should_log:
        # The default of training_args.log_level is passive, so we set log level at info here to have that default.
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    set_seed(training_args.seed)
    server = xp.start_server(9012)
    logger.info(f'Profiling server started: {str(server)}')

    tokenizer_name = model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_id
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    config = AutoConfig.from_pretrained(
        model_args.model_id,
        vocab_size=len(tokenizer),
        torch_dtype=model_args.torch_dtype,
    )
    model = LlamaForCausalLM(config)
    logger.info(f"Loaded model: {model_args.model_id}")
    logger.info(f"Model parameters: {model.num_parameters}")

    model = apply_xla_patch_to_nn_linear(
        model, xs.xla_patched_nn_linear_forward)

    model = model.to(xm.xla_device(), dtype=getattr(
        torch, model_args.torch_dtype))

    optimizer = AdamW(
        params=model.parameters(),
        lr=training_args.learning_rate,
        betas=(training_args.adam_beta1, training_args.adam_beta2),
        eps=training_args.adam_epsilon)

    steps = training_args.max_steps
    lr_scheduler = get_scheduler(
        name=training_args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=training_args.warmup_steps,
        num_training_steps=steps
    )

    # Downloading and loading a dataset from the hub.
    data = load_dataset(
        data_args.dataset_name,
        data_args.dataset_config_name,
        cache_dir=model_args.cache_dir,
    )["train"]
    column_names = list(data.features)
    data = data.map(lambda samples: tokenizer(samples["text"]), batched=True, remove_columns=column_names)

    # Taken from run_clm.py. It's important to group texts evenly to avoid recompilations in TPU.
    block_size = data_args.block_size
    def group_texts(examples):
        from itertools import chain
        # Concatenate all texts.
        concatenated_examples = {k: list(chain(*examples[k])) for k in examples.keys()}
        total_length = len(concatenated_examples[list(examples.keys())[0]])
        # We drop the small remainder, and if the total_length < block_size  we exclude this batch and return an empty dict.
        # We could add padding if the model supported it instead of this drop, you can customize this part to your needs.
        total_length = (total_length // block_size) * block_size
        # Split by chunks of max_len.
        result = {
            k: [t[i : i + block_size] for i in range(0, total_length, block_size)]
            for k, t in concatenated_examples.items()
        }
        result["labels"] = result["input_ids"].copy()
        return result

    data = data.map(group_texts, batched=True)

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=data,
        optimizers=(optimizer, lr_scheduler)
    )

    results = trainer.train_loop()
    logger.info("Training results:")
    logger.info(results)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    main()
