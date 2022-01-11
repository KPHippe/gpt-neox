# coding=utf-8
# Copyright (c) 2021, EleutherAI contributors
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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
#
# This file has been modified from its original version
#

"""Pretrain utilities."""
from datetime import datetime
from functools import partial

import math
import sys
from typing import Dict, Tuple, List

import torch
import deepspeed
import numpy as np

from neox.utils import (
    get_ltor_masks_and_position_ids,
    reduce_losses,
    filter_trainable_params,
)


from neox import print_rank_0, mpu
from neox.model import (
    GPT2ModelPipe,
    SoftEmbedding,
    get_params_for_weight_decay_optimization,
)
from neox.checkpointing import load_checkpoint, save_checkpoint
from neox.data.data_utils import build_train_valid_test_data_iterators
from neox.initialize import initialize_neox
from neox.learning_rates import AnnealingLR
from neox.logging import tb_wandb_log, training_log
from neox.utils import (
    OverflowMonitor,
    get_noise_scale_logger,
    count_params,
    CharCounter,
)
from neox.model.gpt2_model import cross_entropy
from eval_tasks import run_eval_harness


def pretrain(neox_args):
    """
    Main training program.

    This function will run the following in this order:
        - Initializes the model
        - Initializes the optimizer and learning rate scheduler
        - Initializes the data iterators
        - Runs the training loop

        1) initialize Megatron.
        2) setup the model, optimizer and lr schedule
        3) call train_val_test_data_provider to get train/val/test datasets.
        4) train the model.

    Arguments:
        neox_args: an instance of NeoXArgs containing the configuration for pretraining.

    """

    # Initalize megatron (distributed args, logging, etc.)
    initialize_neox(neox_args=neox_args)

    # Setup model, optimizer, and learning rate.
    model, optimizer, lr_scheduler = setup_model_and_optimizer(
        neox_args=neox_args, inference=False, get_key_value=True
    )

    # Data stuff.
    (
        train_data_iterator,
        valid_data_iterator,
        test_data_iterator,
    ) = build_train_valid_test_data_iterators(neox_args=neox_args)

    # Print setup timing.
    print_rank_0("Done with setups. \nStarting training ...")

    # launch wandb after everything is set up, so if any error occurs in the setup process, the run isn't logged to wandb
    neox_args.initialize_wandb()

    iteration = 0
    if neox_args.do_train and neox_args.train_iters > 0:
        # run training

        iteration = train(
            neox_args=neox_args,
            model=model,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            train_data_iterator=train_data_iterator,
            valid_data_iterator=valid_data_iterator,
        )

    if neox_args.do_valid:
        # run validation at the end of training
        evaluate_and_print_results(
            neox_args=neox_args,
            prefix="the end of training for val data",
            forward_step_func=forward_step,
            data_iterator=valid_data_iterator,
            model=model,
            iteration=iteration,
            verbose=False,
        )

    if neox_args.save and iteration != 0:
        # save checkpoint at the end of training
        save_checkpoint(
            neox_args=neox_args,
            iteration=iteration,
            model=model,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
        )

    if neox_args.do_test:
        # Run on test data.
        prefix = "the end of training for test data"
        evaluate_and_print_results(
            neox_args=neox_args,
            prefix=prefix,
            forward_step_func=forward_step,
            data_iterator=test_data_iterator,
            model=model,
            iteration=0,  # iteration 0 in order to always use full test data
            verbose=True,
        )


def _get_batch(neox_args, tokenizer, keys, data, datatype):
    """Support function for get_batch / get_batch pipe (to avoid code repetition)"""
    data_b = mpu.broadcast_data(keys, data, datatype)

    # Unpack.
    tokens_ = data_b["text"].long()
    labels = tokens_[:, 1:].contiguous()
    tokens = tokens_[:, :-1].contiguous()

    # Get the masks and position ids.
    attention_mask, loss_mask, position_ids = get_ltor_masks_and_position_ids(
        tokens, tokenizer.eod, neox_args.eod_mask_loss
    )

    return tokens, labels, loss_mask, attention_mask, position_ids


def get_batch(neox_args, data_iterator):
    """Generate a batch"""

    # Items and their type.
    keys = ["text"]
    datatype = torch.int64

    # Broadcast data.
    if data_iterator is not None:
        data = next(data_iterator)
    else:
        data = None
    return _get_batch(
        neox_args=neox_args,
        tokenizer=neox_args.tokenizer,
        keys=keys,
        data=data,
        datatype=datatype,
    )


def get_batch_pipe(data, neox_args):
    """A modification of get_batch() to work with the latest batch instead of an iterator."""
    # Items and their type.
    keys = ["text"]
    datatype = torch.int64

    tokens, labels, loss_mask, attention_mask, position_ids = _get_batch(
        neox_args, neox_args.tokenizer, keys, data, datatype
    )
    # unpack data
    return (tokens, position_ids, attention_mask), (labels, loss_mask)


def forward_step(data_iterator, model, neox_args, return_logits=False):
    """Forward step."""
    neox_args.timers("forward")
    if neox_args.is_pipe_parallel:
        return model.eval_batch(data_iterator, return_logits=return_logits)

    # Get the batch.
    if neox_args.timers is not None:
        neox_args.timers("batch generator").start()
    tokens, labels, loss_mask, attention_mask, position_ids = get_batch(
        neox_args=neox_args, data_iterator=data_iterator
    )
    if neox_args.timers is not None:
        neox_args.timers("batch generator").stop()

    outputs = model((tokens, position_ids, attention_mask))
    loss = cross_entropy(
        outputs, (labels, loss_mask), _fp16=neox_args.fp16_lm_cross_entropy
    )
    if return_logits:
        return loss, outputs
    return loss


def get_model(neox_args, inference=False, get_key_value=True) -> torch.nn.Module:
    """
    Initializes a GPT2ModelPipe model.
    If specified, also initializes soft prompt tuning layers / adapters.

    Arguments:
        neox_args: NeoX arguments.
        inference: Whether to initialize the model for inference.
        get_key_value: Whether to cache key value pairs (in inference)

    Returns:
        model: The GPT2ModelPipe model (a torch.nn.Module).
    """

    print_rank_0("building GPT2 model ...")

    # Build model on cpu.
    model = GPT2ModelPipe(
        neox_args=neox_args,
        parallel_output=True,
        topology=mpu.get_topology(),
        inference=inference,
        get_key_value=get_key_value,
    )

    ### soft prompt tuning stuff ###
    if neox_args.soft_prompt_tuning is not None and neox_args.soft_prompt_tuning.get(
        "enabled", False
    ):
        soft_prompt = SoftEmbedding(
            neox_args,
            wte=getattr(model, "0").word_embeddings,
            n_tokens=neox_args.soft_prompt_tuning.get("n_tokens", 10),
            init_string=neox_args.soft_prompt_tuning.get("init_string", ""),
            init_range=neox_args.soft_prompt_tuning.get("init_range", 0.5),
        )
        model.insert_layers(
            layers=soft_prompt, idx=1
        )  # insert the soft prompt layer directly after the word embeddings

        # freeze everything but the soft prompt
        for name, param in model.named_parameters():
            if not "soft_embedding" in name:
                param.requires_grad = False

    if not neox_args.is_pipe_parallel:
        # Export PipeParallel model to nn.Sequential model to avoid the overhead of deepspeed's pipe parallel training
        model = model.to_sequential()

    return model


def get_optimizer(model, neox_args) -> Tuple[torch.optim.Optimizer, List[Dict]]:
    """
    Sets up the optimizer for training.

    Arguments:
        model: a GPT2ModelPipe model.
        neox_args: NeoX arguments.

    Returns:
        optimizer: a torch.optim.Optimizer.
        param_groups: a list of the optimizer's parameter groups.
    """
    if neox_args.no_load_optim:
        return None, None
    # Build parameter groups (weight decay and non-decay).
    param_groups = get_params_for_weight_decay_optimization(model, neox_args)
    print_rank_0(
        f'Configuring Optimizer type: {neox_args.optimizer_type} with params: {neox_args.optimizer["params"]}'
    )

    # Add model parallel attribute if it is not set.
    for param_group in param_groups:
        for param in param_group["params"]:
            if not hasattr(param, "model_parallel"):
                param.model_parallel = False

    # Filter out params that don't require a grad (for soft prompt tuning, etc.)
    param_groups = filter_trainable_params(param_groups)

    # init optimizer

    if neox_args.optimizer_type.lower() in ["cpu_adam", "cpu_torch_adam"]:
        if neox_args.optimizer == "cpu_torch_adam":
            cpu_adam_optimizer = torch.optim.Adam
        else:
            from deepspeed.ops.adam import DeepSpeedCPUAdam

            cpu_adam_optimizer = DeepSpeedCPUAdam
        optimizer = cpu_adam_optimizer(
            param_groups,
            weight_decay=neox_args.weight_decay,
            **neox_args.optimizer["params"],
        )
    elif neox_args.optimizer_type.lower() == "onebitadam":
        optimizer = None
        # onebitadam needs to be instantiated within the deepspeed engine to work :|
    elif neox_args.optimizer_type.lower() == "sm3":
        from .optimizers import SM3

        optimizer = SM3(param_groups, **neox_args.optimizer["params"])
    elif neox_args.optimizer_type.lower() == "madgrad_wd":
        from .optimizers import madgrad_wd

        optimizer = madgrad_wd(
            param_groups,
            weight_decay=neox_args.weight_decay,
            **neox_args.optimizer["params"],
        )
    elif neox_args.optimizer_type.lower() == "adam":
        # Use Adam
        if neox_args.use_bnb_optimizer:
            try:
                import bitsandbytes as bnb

                adam_optimizer = bnb.optim.Adam8bit
            except ModuleNotFoundError:
                print(
                    "Please install bitsandbytes following https://github.com/facebookresearch/bitsandbytes."
                )
                raise Exception
        else:
            try:
                # default to apex as it's slightly faster
                from apex.optimizers import FusedAdam as Adam
            except ImportError:
                # if apex isn't installed, use deepspeed's FusedAdam
                print(
                    "WARNING: APEX not installed - defaulting to deepspeed's fused adam"
                )
                from deepspeed.ops.adam import FusedAdam as Adam
            adam_optimizer = Adam
        optimizer = adam_optimizer(
            param_groups,
            weight_decay=neox_args.weight_decay,
            **neox_args.optimizer["params"],
        )
    else:
        raise ValueError(f"Optimizer type {neox_args.optimizer_type} not recognized")

    return optimizer, param_groups


def get_learning_rate_scheduler(
    optimizer: torch.optim.Optimizer, neox_args
) -> AnnealingLR:
    """
    Initialize the learning rate scheduler.

    Arguments:
        optimizer: a torch.optim.Optimizer.
        neox_args: NeoX arguments.

    Returns:
        AnnealingLR: a learning rate scheduler.
    """
    if neox_args.no_load_optim:
        # TODO: this should be configured as a separate arg
        return None
    if neox_args.optimizer_type.lower() == "onebitadam":
        print_rank_0(
            "WARNING: onebitadam requires the lr scheduler be built by deepspeed - "
            "Make sure one is added to your deepspeed config"
        )
        return None

    # Add linear learning rate scheduler.
    if neox_args.lr_decay_iters is not None:
        num_iters = neox_args.lr_decay_iters
    else:
        num_iters = neox_args.train_iters

    return AnnealingLR(
        optimizer,
        start_lr=neox_args.lr,
        warmup_iter=neox_args.warmup * num_iters,
        total_iters=max(1, num_iters),
        decay_style=neox_args.lr_decay_style,
        last_iter=0,
        min_lr=neox_args.min_lr,
        use_checkpoint_lr_scheduler=neox_args.use_checkpoint_lr_scheduler,
        override_lr_scheduler=neox_args.override_lr_scheduler,
    )


def setup_model_and_optimizer(neox_args, inference=False, get_key_value=True, iteration=None):
    """
    Sets up the model, optimizer and learning rate scheduler, as well as initializing the deepspeed engine.

    Args:
        neox_args: NeoX arguments.
        inference: Whether to setup the model for inference. TODO: expand on this - what is different specifically?
        get_key_value: Whether to cache key value pairs (in inference)
    """
    model = get_model(
        neox_args=neox_args, inference=inference, get_key_value=get_key_value
    )
    optimizer, param_groups = get_optimizer(model=model, neox_args=neox_args)
    lr_scheduler = get_learning_rate_scheduler(optimizer=optimizer, neox_args=neox_args)

    if neox_args.no_load_optim:
        model_parameters = None
    else:
        model_parameters = param_groups if optimizer is None else None

    model, optimizer, _, lr_scheduler = deepspeed.initialize(
        model=model,
        optimizer=optimizer,
        args=neox_args,
        lr_scheduler=lr_scheduler,
        dist_init_required=False,
        model_parameters=model_parameters,
        config_params=neox_args.deepspeed_config,
        mpu=mpu if not neox_args.is_pipe_parallel else None,
    )

    if neox_args.is_pipe_parallel:
        # we need to set these values after deepspeed.initialize, so we do it here
        model.set_has_bool_tensors(True)
        model.set_batch_fn(partial(get_batch_pipe, neox_args=neox_args))

    if neox_args.load is not None:
        neox_args.iteration = load_checkpoint(
            neox_args=neox_args,
            model=model,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            inference=inference,
            iteration=iteration,
        )
        print_rank_0(
            f"Loading checkpoint and starting from iteration {neox_args.iteration}"
        )
    else:
        neox_args.iteration = 0

    # count the number of parameters in the model
    neox_args.total_params = count_params(model.module)
    print_rank_0(f' > total params: {"{:,}".format(neox_args.total_params)}')
    return model, optimizer, lr_scheduler


def backward_step(neox_args, timers, model, loss):
    """Backward step."""

    # Backward pass.
    neox_args.timers("backward-backward").start()
    model.backward(loss)
    neox_args.timers("backward-backward").stop()

    # DeepSpeed backward propagation already addressed all reduce communication.
    # Reset the timer to avoid breaking timer logs below.
    timers("backward-allreduce").reset()


def train_step(neox_args, data_iterator, model, optimizer) -> Tuple[dict, bool]:
    """
    Runs a single training step.

    Args:
        neox_args: NeoX arguments.
        data_iterator: Training data iterator.
        model: A Deepspeed model engine instance.
        optimizer: A pytorch optimizer instance.

    Returns:
        loss_dict: The loss value reduced across processes.
        skipped_iter: A boolean indicating whether the iteration was skipped.
    """

    if neox_args.is_pipe_parallel:
        # Pipeline parallelism schedules forward/backward/step, so we hand that off to deepspeed.
        loss_dict = train_step_pipe(
            neox_args=neox_args, model=model, data_iterator=data_iterator
        )
    else:
        # If not pipeline parallel, we do the forward/backward/step ourselves.
        losses = []
        for _ in range(neox_args.gradient_accumulation_steps):
            # Forward model for one step.
            neox_args.timers("forward").start()
            loss = forward_step(
                neox_args=neox_args,
                data_iterator=data_iterator,
                model=model,
            )
            neox_args.timers("forward").stop()
            losses.append(loss)
            # Calculate gradients, reduce across processes, and clip.
            neox_args.timers("backward").start()
            backward_step(
                neox_args=neox_args,
                optimizer=optimizer,
                model=model,
                loss=loss,
            )
            neox_args.timers("backward").stop()

            # Update parameters.
            neox_args.timers("optimizer").start()
            model.step()

            neox_args.timers("optimizer").stop()

        loss_dict = {
            "lm_loss": reduce_losses(losses).mean()
        }  # reduces losses across machines for logging

    if neox_args.precision == "fp16" and model.optimizer.overflow:
        skipped_iter = 1
    else:
        skipped_iter = 0

    return loss_dict, skipped_iter


def train_step_pipe(neox_args, model, data_iterator):
    """
    Runs a single training step with DeepSpeed's pipeline parallel engine.

    Args:
        neox_args: NeoX arguments.
        model: A Deepspeed model engine instance.
        data_iterator: Training data iterator.

    Returns:
        loss_dict: The loss value reduced across processes.
    """

    loss = model.train_batch(data_iter=data_iterator)
    loss_dict = {"lm_loss": loss}
    # Don't break Megatron's timers because we changed code paths.
    for t in [
        "forward",
        "backward",
        "allreduce",
        "optimizer",
        "batch generator",
        "data loader",
    ]:
        neox_args.timers(t).reset()
    return loss_dict


def train(
    neox_args,
    model,
    optimizer,
    lr_scheduler,
    train_data_iterator,
    valid_data_iterator,
):
    """
    Runs the pretraining loop.

    Args:
        neox_args: NeoX arguments.
        model: A Deepspeed model engine instance.
        optimizer: A pytorch optimizer instance.
        lr_scheduler: A deepspeed learning rate scheduler instance.
        train_data_iterator: Training data iterator.
        valid_data_iterator: Validation data iterator.

    Returns:
        iteration: The number of iterations trained.
    """

    # Turn on training mode which enables dropout.
    model.train()

    # Tracking loss.
    total_loss_dict = {}

    # Iterations.
    iteration = neox_args.iteration

    neox_args.timers("interval time").start()
    report_memory_flag = True

    # get noise scale logger (if neox_args.log_gradient_noise_scale is True)
    noise_scale_logger = get_noise_scale_logger(neox_args)

    # to monitor if we've skipped many iterations in a row and trigger an early exit
    overflow_monitor = OverflowMonitor(optimizer)
    while iteration < neox_args.train_iters:
        loss_dict, skipped_iter = train_step(
            neox_args=neox_args,
            data_iterator=train_data_iterator,
            model=model,
            optimizer=optimizer,
        )
        iteration += 1

        overflow_monitor.check(skipped_iter)  # check for repeated overflow
        if neox_args.log_gradient_noise_scale:  # log noise scale if applicable
            noise_scale_logger.update()

        # get learning rate (if present) - if doing soft prompt tuning + pipe parallel, you
        # may have no tunable parameters on a specific rank
        if optimizer.param_groups:
            lr = optimizer.param_groups[0].get("lr", 0)
        else:
            lr = 0

        # Logging.
        report_memory_flag = training_log(
            neox_args=neox_args,
            loss_dict=loss_dict,
            total_loss_dict=total_loss_dict,
            learning_rate=lr,
            iteration=iteration,
            loss_scale=optimizer.cur_scale if neox_args.precision == "fp16" else None,
            report_memory_flag=report_memory_flag,
            skipped_iter=skipped_iter,
            model=model,
            optimizer=optimizer,
            noise_scale_logger=noise_scale_logger,
        )

        # Checkpointing
        if (
            neox_args.save
            and neox_args.save_interval
            and iteration % neox_args.save_interval == 0
        ):
            save_checkpoint(
                neox_args=neox_args,
                iteration=iteration,
                model=model,
                optimizer=optimizer,
                lr_scheduler=lr_scheduler,
            )

        # Evaluation
        if (
            neox_args.eval_interval
            and iteration % neox_args.eval_interval == 0
            and neox_args.do_valid
        ):
            prefix = "iteration {}".format(iteration)
            evaluate_and_print_results(
                neox_args=neox_args,
                prefix=prefix,
                forward_step_func=forward_step,
                data_iterator=valid_data_iterator,
                model=model,
                iteration=iteration,
                verbose=False,
            )

        if neox_args.exit_interval and iteration % neox_args.exit_interval == 0:
            torch.distributed.barrier()
            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rank = torch.distributed.get_rank()
            print_rank_0(
                "rank: {} | time: {} | exiting the program at iteration {}".format(
                    rank, time_str, iteration
                )
            )
            sys.exit()

    return iteration


def evaluate(neox_args, forward_step_fn, data_iterator, model, verbose=False):
    """Evaluation.
    neox_args: NeoX Arguments
    forward_step_fn: function with args `neox_args, timers,
                    data_iterator & model that will run a forward pass on the model
    data_iterator: Iterator that iterates over batches of data. Should return data in the form:
                    {'text': np.array([tokens], dtype=np.int64)}
                    where the size of the array is the model's context size + 1
                    (`get_batch` transforms it into inputs / labels)
    """
    # Turn on evaluation mode which disables dropout.
    model.eval()
    losses = []
    if neox_args.char_level_ppl:
        data_iterator = CharCounter(data_iterator, neox_args.tokenizer)

    with torch.no_grad():
        iteration = 0
        while iteration < neox_args.eval_iters:
            iteration += 1
            if verbose and iteration % neox_args.log_interval == 0:
                print_rank_0(
                    "Evaluating iter {}/{}".format(iteration, neox_args.eval_iters)
                )

            # although we're not accumulating gradients here, we count one iter as train_batch_size_per_gpu * g.a.s
            # to be consistent with deepspeed's pipe parallel engine
            # since pipe parallel already takes gas into account - default to 1 here if pipe parallel is true
            for _ in range(
                1
                if neox_args.is_pipe_parallel
                else neox_args.gradient_accumulation_steps
            ):
                # Forward evaluation
                loss = forward_step_fn(
                    model=model,
                    data_iterator=data_iterator,
                    neox_args=neox_args,
                )
                losses.append(loss)

            # When contiguous memory optimizations are enabled, the buffers
            # allocated by the optimizations are deallocated during backward pass
            # in the absence of backward pass the buffers should be reset after each
            # forward pass
            if neox_args.deepspeed_activation_checkpointing:
                deepspeed.checkpointing.reset()

    # reduces losses across processes for logging & run eval harness tasks
    eval_results = {"lm_loss": reduce_losses(losses).mean().item()}
    eval_results["lm_loss_ppl"] = math.exp(eval_results["lm_loss"])

    if neox_args.char_level_ppl:
        # calculate character level perplexity, if specified
        # if neox_args.char_level_perplexity:
        # unwrap the data_iterator
        tokens_per_char = data_iterator.tokens_per_char()
        print_rank_0(f"Counting chars took {data_iterator.total_time} seconds")

        data_iterator = data_iterator.data_iterator
        eval_results["lm_loss_char_lvl_ppl"] = math.exp(
            eval_results["lm_loss"] * tokens_per_char
        )

    if neox_args.eval_tasks:
        eval_results.update(
            run_eval_harness(
                model, forward_step_fn, neox_args, eval_tasks=neox_args.eval_tasks
            )
        )
    # Move model back to the train mode.
    model.train()
    return eval_results


def evaluate_and_print_results(
    neox_args,
    prefix,
    forward_step_func,
    data_iterator,
    model,
    iteration,
    verbose=False,
):
    """Helper function to evaluate and dump results on screen."""
    total_loss_dict = evaluate(
        neox_args=neox_args,
        forward_step_fn=forward_step_func,
        data_iterator=data_iterator,
        model=model,
        verbose=verbose,
    )
    string = f" validation results at {prefix} | "
    for k, v in total_loss_dict.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                k3 = "_".join([k, k2])
                string += f"{k3} value: {v2:.6E} | "
                tb_wandb_log(
                    f"validation/{k3}",
                    v2,
                    iteration,
                    use_wandb=neox_args.use_wandb,
                    tensorboard_writer=neox_args.tensorboard_writer,
                )
        else:
            string += f"{k} value: {v:.6E} | "
            tb_wandb_log(
                f"validation/{k}",
                v,
                iteration,
                use_wandb=neox_args.use_wandb,
                tensorboard_writer=neox_args.tensorboard_writer,
            )

    length = len(string) + 1
    print_rank_0("-" * length)
    print_rank_0(string)
    print_rank_0("-" * length)