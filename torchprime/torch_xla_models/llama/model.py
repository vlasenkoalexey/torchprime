# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
"""PyTorch LLaMA model."""

import math

import torch
import torch_xla.debug.profiler as xp
import torch_xla.distributed.spmd as xs
from omegaconf import DictConfig
from torch import nn
from transformers.activations import ACT2FN
from transformers.utils import logging

from torchprime.layers.sequential import HomogeneousSequential
from torchprime.rope.rope import RopeScaling, llama3_rope_frequencies
from torchprime.torch_xla_models import offloading
from torchprime.torch_xla_models.loss import cross_entropy_loss

logger = logging.get_logger(__name__)


class LlamaRMSNorm(nn.Module):
  def __init__(self, hidden_size, eps=1e-6):
    """
    LlamaRMSNorm is equivalent to T5LayerNorm
    """
    super().__init__()
    self.weight = nn.Parameter(torch.ones(hidden_size))
    self.variance_epsilon = eps

  def forward(self, hidden_states):
    input_dtype = hidden_states.dtype
    hidden_states = hidden_states.to(torch.float32)
    variance = hidden_states.pow(2).mean(-1, keepdim=True)
    hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
    return self.weight * hidden_states.to(input_dtype)


class LlamaRotaryEmbedding(nn.Module):
  inv_freq: nn.Buffer

  def __init__(
    self,
    head_dim,
    rope_theta,
    scaling: RopeScaling | None = None,
  ):
    super().__init__()
    inv_freq = llama3_rope_frequencies(head_dim, theta=rope_theta, scaling=scaling)
    self.register_buffer("inv_freq", inv_freq, persistent=False)

  @torch.no_grad()
  def forward(self, x, position_ids):
    # x: [bs, num_attention_heads, seq_len, head_size]
    inv_freq_expanded = (
      self.inv_freq[None, :, None].float().expand(position_ids.shape[0], -1, 1)
    )
    position_ids_expanded = position_ids[:, None, :].float()
    # Force float32 since bfloat16 loses precision on long contexts
    # See https://github.com/huggingface/transformers/pull/29285
    device_type = x.device.type
    device_type = (
      device_type if isinstance(device_type, str) and device_type != "mps" else "cpu"
    )
    with torch.autocast(device_type=device_type, enabled=False):
      freqs = (inv_freq_expanded.float() @ position_ids_expanded.float()).transpose(
        1, 2
      )
      emb = torch.cat((freqs, freqs), dim=-1)
      cos = emb.cos()
      sin = emb.sin()
    return cos.to(dtype=x.dtype), sin.to(dtype=x.dtype)


def rotate_half(x):
  """Rotates half the hidden dims of the input."""
  x1 = x[..., : x.shape[-1] // 2]
  x2 = x[..., x.shape[-1] // 2 :]
  return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, position_ids=None, unsqueeze_dim=1):
  """Applies Rotary Position Embedding to the query and key tensors.

  Args:
      q (`torch.Tensor`): The query tensor.
      k (`torch.Tensor`): The key tensor.
      cos (`torch.Tensor`): The cosine part of the rotary embedding.
      sin (`torch.Tensor`): The sine part of the rotary embedding.
      position_ids (`torch.Tensor`, *optional*):
          Deprecated and unused.
      unsqueeze_dim (`int`, *optional*, defaults to 1):
          The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
          sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
          that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
          k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
          cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
          the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
  Returns:
      `tuple(torch.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
  """
  cos = cos.unsqueeze(unsqueeze_dim)
  sin = sin.unsqueeze(unsqueeze_dim)
  q_embed = (q * cos) + (rotate_half(q) * sin)
  k_embed = (k * cos) + (rotate_half(k) * sin)
  return q_embed, k_embed


class LlamaMLP(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.config = config
    self.hidden_size = config.hidden_size
    self.intermediate_size = config.intermediate_size
    self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
    self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
    self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
    self.act_fn = ACT2FN[config.hidden_act]

  @xp.trace_me("LlamaMLP")
  def forward(self, x):
    down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
    return down_proj


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
  """
  This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
  num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
  """
  batch, num_key_value_heads, slen, head_dim = hidden_states.shape
  if n_rep == 1:
    return hidden_states
  hidden_states = hidden_states[:, :, None, :, :].expand(
    batch, num_key_value_heads, n_rep, slen, head_dim
  )
  return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class LlamaAttention(nn.Module):
  """Multi-headed attention from 'Attention Is All You Need' paper"""

  def __init__(self, config: DictConfig, layer_idx: int | None = None):
    super().__init__()
    self.config = config
    self.layer_idx = layer_idx
    if layer_idx is None:
      logger.warning_once(
        f"Instantiating {self.__class__.__name__} without passing a `layer_idx` is not recommended and will "
        "lead to errors during the forward call if caching is used. Please make sure to provide a `layer_idx` "
        "when creating this class."
      )

    self.attention_dropout = config.attention_dropout
    self.hidden_size = config.hidden_size
    self.num_heads = config.num_attention_heads
    self.head_dim = self.hidden_size // self.num_heads
    self.num_key_value_heads = config.num_key_value_heads
    self.num_key_value_groups = self.num_heads // self.num_key_value_heads
    self.max_position_embeddings = config.max_position_embeddings
    self.rope_theta = config.rope_theta
    self.is_causal = True

    if (self.head_dim * self.num_heads) != self.hidden_size:
      raise ValueError(
        f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
        f" and `num_heads`: {self.num_heads})."
      )

    self.q_proj = nn.Linear(
      self.hidden_size,
      self.num_heads * self.head_dim,
      bias=config.attention_bias,
    )
    self.k_proj = nn.Linear(
      self.hidden_size,
      self.num_key_value_heads * self.head_dim,
      bias=config.attention_bias,
    )
    self.v_proj = nn.Linear(
      self.hidden_size,
      self.num_key_value_heads * self.head_dim,
      bias=config.attention_bias,
    )
    self.o_proj = nn.Linear(
      self.hidden_size, self.hidden_size, bias=config.attention_bias
    )
    self._init_rope()

  def _init_rope(self):
    scaling = self.config.get("rope_scaling", None)
    if scaling is not None:
      scaling = RopeScaling(**scaling)
    self.rotary_emb = LlamaRotaryEmbedding(
      head_dim=self.head_dim, rope_theta=self.rope_theta, scaling=scaling
    )

  @xp.trace_me("LlamaAttention")
  def forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: torch.Tensor | None = None,
    position_ids: torch.LongTensor | None = None,
  ) -> torch.FloatTensor:
    bsz, q_len, _ = hidden_states.size()

    query_states = self.q_proj(hidden_states)
    key_states = self.k_proj(hidden_states)
    value_states = self.v_proj(hidden_states)

    query_states = query_states.view(
      bsz, q_len, self.num_heads, self.head_dim
    ).transpose(1, 2)
    key_states = key_states.view(
      bsz, q_len, self.num_key_value_heads, self.head_dim
    ).transpose(1, 2)
    value_states = value_states.view(
      bsz, q_len, self.num_key_value_heads, self.head_dim
    ).transpose(1, 2)

    cos, sin = self.rotary_emb(value_states, position_ids)
    query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin)

    if self.config.attention_kernel != "splash_attention":
      key_states = repeat_kv(key_states, self.num_key_value_groups)
      value_states = repeat_kv(value_states, self.num_key_value_groups)

    # Non FA path doesn't deal with 2D sharding.
    partition_spec = None
    if xs.get_global_mesh() is not None:
      partition_spec = (("data", "fsdp"), "tensor", None, None)
      segment_ids_partition_spec = (("data", "fsdp"), None)
    if self.config.attention_kernel == "splash_attention":
      # Integrated with PyTorch/XLA Pallas Splash Attention:
      assert (
        xs.get_global_mesh() is not None
      ), "Global mesh is required for Splash Attention"
      from torchprime.torch_xla_models.experimental.custom_kernel import (
        SplashAttentionConfig,
        splash_attention,
      )

      sa_config = SplashAttentionConfig(
        mesh=str(xs.get_global_mesh()),
        qkv_partition_spec=partition_spec,
        segment_ids_partition_spec=segment_ids_partition_spec,
      )
      query_states /= math.sqrt(self.head_dim)
      attn_output = splash_attention(
        query_states, key_states, value_states, sa_config.to_json()
      )
    elif self.config.attention_kernel == "flash_attention":
      # Integrated with PyTorch/XLA Pallas Flash Attention:
      from torch_xla.experimental.custom_kernel import flash_attention

      query_states /= math.sqrt(self.head_dim)

      attn_output = flash_attention(
        query_states,
        key_states,
        value_states,
        causal=True,
        partition_spec=partition_spec,
      )
    else:
      attn_weights = torch.matmul(query_states, key_states.transpose(2, 3)) / math.sqrt(
        self.head_dim
      )

      if attention_mask is not None:  # no matter the length, we just slice it
        causal_mask = attention_mask[:, :, :, : key_states.shape[-2]]
        attn_weights = attn_weights + causal_mask

      # upcast attention to fp32
      attn_weights = nn.functional.softmax(
        attn_weights, dim=-1, dtype=torch.float32
      ).to(query_states.dtype)
      attn_weights = nn.functional.dropout(
        attn_weights, p=self.attention_dropout, training=self.training
      )
      attn_output = torch.matmul(attn_weights, value_states)

    if attn_output.size() != (bsz, self.num_heads, q_len, self.head_dim):
      raise ValueError(
        f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
        f" {attn_output.size()}"
      )

    attn_output = attn_output.transpose(1, 2).contiguous()

    attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

    attn_output = self.o_proj(attn_output)

    return attn_output


class LlamaDecoderLayer(nn.Module):
  def __init__(self, config: DictConfig, layer_idx: int):
    super().__init__()
    self.hidden_size = config.hidden_size

    self.self_attn = LlamaAttention(config=config, layer_idx=layer_idx)

    self.mlp = LlamaMLP(config)
    self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
    self.post_attention_layernorm = LlamaRMSNorm(
      config.hidden_size, eps=config.rms_norm_eps
    )

  @xp.trace_me("LlamaDecoderLayer")
  def forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: torch.Tensor | None = None,
    position_ids: torch.Tensor | None = None,
  ) -> torch.Tensor:
    """
    Args:
        hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
        attention_mask (`torch.FloatTensor`, *optional*):
            attention mask of size `(batch_size, sequence_length)` if flash attention is used or `(batch_size, 1,
            query_sequence_length, key_sequence_length)` if default attention is used.
    """
    # This gives the `hidden_states` tensor a name so that we can layer specify
    # to offload this tensor to host RAM to save memory. This is not a standard
    # torch API because there is no such feature in PyTorch. Instead, the name
    # becomes node metadata during FX graph capture.
    hidden_states = offloading.offload_name(hidden_states, "decoder_input")

    residual = hidden_states

    hidden_states = self.input_layernorm(hidden_states)

    # Self Attention
    hidden_states = self.self_attn(
      hidden_states=hidden_states,
      attention_mask=attention_mask,
      position_ids=position_ids,
    )
    hidden_states = residual + hidden_states

    # Fully Connected
    residual = hidden_states
    hidden_states = self.post_attention_layernorm(hidden_states)
    hidden_states = self.mlp(hidden_states)
    hidden_states = residual + hidden_states

    return hidden_states


class LlamaModel(nn.Module):
  """
  Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`LlamaDecoderLayer`]

  Args:
      config: DictConfig
  """

  def __init__(self, config: DictConfig):
    super().__init__()
    self.vocab_size = config.vocab_size
    self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)

    # `HomogeneousSequential` is similar to `nn.Sequential` but can be compiled with
    # `scan` described in https://pytorch.org/xla/release/r2.6/features/scan.html.
    self.layers = HomogeneousSequential(
      *[
        LlamaDecoderLayer(config, layer_idx)
        for layer_idx in range(config.num_hidden_layers)
      ]
    )
    self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

  @xp.trace_me("LlamaModel")
  def forward(
    self,
    input_ids: torch.LongTensor,
    attention_mask: torch.FloatTensor | None = None,
  ) -> torch.Tensor:
    # convert input ids to embeddings
    inputs_embeds = self.embed_tokens(input_ids)

    seq_length = inputs_embeds.size(1)

    # TODO(https://github.com/pytorch/xla/issues/8783): Pass position_ids as `long()`
    # when `scan` can take non-differentiable inputs.
    position_ids = (
      torch.arange(seq_length, device=inputs_embeds.device).unsqueeze(0).float()
    )

    # Create a causal attention mask
    causal_mask = torch.triu(
      torch.full((seq_length, seq_length), float("-inf"), device=inputs_embeds.device),
      diagonal=1,
    )
    causal_mask = causal_mask.unsqueeze(0).unsqueeze(0)  # Add batch and head dimension

    if attention_mask is not None:
      causal_mask = causal_mask * attention_mask[:, None, None, :]

    # decoder layers
    hidden_states = self.layers(
      inputs_embeds, attention_mask=causal_mask, position_ids=position_ids
    )

    hidden_states = self.norm(hidden_states)
    return hidden_states


class LlamaForCausalLM(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.config = config
    self.model = LlamaModel(config)
    self.vocab_size = config.vocab_size
    self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

    # Initialize weights and apply final processing
    self.apply(self._init_weights)

  def _init_weights(self, module):
    std = self.config.initializer_range
    if isinstance(module, nn.Linear):
      module.weight.data.normal_(mean=0.0, std=std)
      if module.bias is not None:
        module.bias.data.zero_()
    elif isinstance(module, nn.Embedding):
      module.weight.data.normal_(mean=0.0, std=std)
      if module.padding_idx is not None:
        module.weight.data[module.padding_idx].zero_()

  @xp.trace_me("LlamaForCausalLM")
  def forward(
    self,
    input_ids: torch.LongTensor,
    labels: torch.LongTensor | None = None,
    attention_mask: torch.FloatTensor | None = None,
  ) -> tuple[torch.FloatTensor, torch.FloatTensor | None]:
    hidden_states = self.model(input_ids=input_ids, attention_mask=attention_mask)
    logits = self.lm_head(hidden_states)
    logits = logits.float()
    if labels is None:
      return logits, None
    loss = cross_entropy_loss(logits, labels=labels, vocab_size=self.config.vocab_size)
    return logits, loss
