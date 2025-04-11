# torch_xla models

These models use the [torch_xla][1] framework.

## Running locally on a TPU VM

1. Setup environment as per [README][README-examples].

1. Export key environment variables:

   ```sh
   export HF_TOKEN='... hugging face token ...'
   export XLA_IR_DEBUG=1
   export XLA_HLO_DEBUG=1
   ```

1. Run the trainer. The default is to train Llama 3.0 8B sharded over 4 v6e chips.

   ```sh
   python3 torchprime/torch_xla_models/train.py
   ```

    For v5e add ```block_size=1024``` parameter to prevent OOM.

## Running on a XPK cluster

First follow the [distributed training][distributed-training] guide to setup the
cluster information.

Then export key environment variables in your local environment:

```sh
export HF_TOKEN='... hugging face token ...'
export XLA_IR_DEBUG=1
export XLA_HLO_DEBUG=1
```

Finally pick from one of these recipes, and it will build a docker image and
launch it on XPK.

### Llama 3.0 8B on v6e-256

Recipe for global batch size 256, sequence length 8192.
Expected step duration: 1.625s. MFU: 33.53%.

```sh
tp run torchprime/torch_xla_models/train.py \
    model=llama-3-8b \
    global_batch_size=256 \
    block_size=8192 \
    profile_step=5 \
    ici_mesh.fsdp=256
```

Recipe for global batch size 512, sequence length 8192.
Expected step duration: 2.991s. MFU: 36.43%.

```sh
tp run torchprime/torch_xla_models/train.py \
    model=llama-3-8b \
    global_batch_size=512 \
    block_size=8192 \
    profile_step=5 \
    ici_mesh.fsdp=256
```

### Llama 3.1 8B on v6e-256

<!-- TODO(https://github.com/AI-Hypercomputer/torchprime/issues/135): publish perf data. -->

Recipe for global batch size 512, sequence length 8192:

```sh
tp run torchprime/torch_xla_models/train.py \
    model=llama-3.1-8b \
    global_batch_size=512 \
    block_size=8192 \
    profile_step=5 \
    ici_mesh.fsdp=256
```

### Llama 3.1 405B on v6e-256

Recipe for global batch size 256, sequence length 8192. We need to use a larger
dataset.

Expected step duration: 67.212s. MFU: 34.96%.

```sh
export LIBTPU_INIT_ARGS='--xla_tpu_enable_flash_attention=false --xla_tpu_enable_async_collective_fusion=true --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true --xla_tpu_enable_async_collective_fusion_multiple_steps=true --xla_tpu_overlap_compute_collective_tc=true --xla_enable_async_all_gather=true --xla_tpu_scoped_vmem_limit_kib=98304 --xla_tpu_enable_all_experimental_scheduler_features=true --xla_tpu_enable_scheduler_memory_pressure_tracking=true --xla_tpu_host_transfer_overlap_limit=24 --xla_tpu_aggressive_opt_barrier_removal=ENABLED --xla_lhs_prioritize_async_depth_over_stall=ENABLED --xla_tpu_enable_ag_backward_pipelining=true --xla_should_allow_loop_variant_parameter_in_chain=ENABLED --xla_should_add_loop_invariant_op_in_chain=ENABLED --xla_max_concurrent_host_send_recv=100 --xla_tpu_scheduler_percent_shared_memory_limit=100 --xla_latency_hiding_scheduler_rerun=2 --xla_tpu_spmd_rng_bit_generator_unsafe=true'

tp run torchprime/torch_xla_models/train.py \
    model=llama-3.1-405b \
    global_batch_size=256 \
    block_size=8192 \
    ici_mesh.fsdp=64 \
    ici_mesh.tensor=4 \
    profile_step=5 \
    profile_duration=240000 \
    dataset_config_name=wikitext-103-raw-v1 \
    max_steps=50 \
    logging_steps=10
```

### Llama 3.1 405B on 2 pods of v6e-256

<!-- TODO(b/408348551): Add back profile_step after fixing MegaScale hang. -->

Recipe for global batch size 512, sequence length 8192. We need to use a larger
dataset and profile later for longer for the DCN performance to stabilize.

Expected step duration: 69.081s. MFU: 34.01%.

```sh
export LIBTPU_INIT_ARGS='--xla_tpu_enable_flash_attention=false --xla_tpu_enable_async_collective_fusion=true --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true --xla_tpu_enable_async_collective_fusion_multiple_steps=true --xla_tpu_overlap_compute_collective_tc=true --xla_enable_async_all_gather=true --xla_tpu_scoped_vmem_limit_kib=98304 --xla_tpu_enable_all_experimental_scheduler_features=true --xla_tpu_enable_scheduler_memory_pressure_tracking=true --xla_tpu_host_transfer_overlap_limit=24 --xla_tpu_aggressive_opt_barrier_removal=ENABLED --xla_lhs_prioritize_async_depth_over_stall=ENABLED --xla_tpu_enable_ag_backward_pipelining=true --xla_should_allow_loop_variant_parameter_in_chain=ENABLED --xla_should_add_loop_invariant_op_in_chain=ENABLED --xla_max_concurrent_host_send_recv=100 --xla_tpu_scheduler_percent_shared_memory_limit=100 --xla_latency_hiding_scheduler_rerun=2 --xla_tpu_spmd_rng_bit_generator_unsafe=true'

tp run torchprime/torch_xla_models/train.py \
    model=llama-3.1-405b \
    global_batch_size=512 \
    block_size=8192 \
    dcn_mesh.fsdp=2 \
    ici_mesh.fsdp=64 \
    ici_mesh.tensor=4 \
    dataset_config_name=wikitext-103-raw-v1 \
    max_steps=50 \
    logging_steps=10
```

### Mixtral 8x7B on v6e-256

<!-- TODO(https://github.com/AI-Hypercomputer/torchprime/issues/137): publish perf data -->

Recipe for global batch size 512, sequence length 8192.

```sh
tp run torchprime/torch_xla_models/train.py \
    model=mixtral-8x7b \
    global_batch_size=512 \
    ici_mesh.fsdp=256 \
    dataset_config_name=wikitext-103-raw-v1 \
    profile_step=5
```

## Key Components

- `train.py`: Main training script that sets up the model, data, and training loop
- `configs/base.yaml`: Configuration file for the training script
- `configs/model`: Configuration files for models
- `configs/model/sharding`: Configuration files for distributing the training
  over many chips
- `configs/model/remat`: Configuration files for rematerialization strategy e.g.
  activation checkpointing, host offloading
- `llama/model.py`: Implementation of the Llama model family
- `mixtral/model.py`: Implementation of the Mixtral model family

[1]: https://github.com/pytorch/xla
[README-examples]: ../../README.md#examples
[distributed-training]: ../../README.md#distributed-training
