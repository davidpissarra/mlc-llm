"""
Implementation for Mistral.
"""
import dataclasses
import logging
import math
from typing import Any, Dict, Optional

from tvm import te, tir
from tvm.relax.frontend import nn
from tvm.relax.frontend.nn import Tensor, op

from ...support.style import bold
from .llama_model import LlamaConfig, LlamaFFN, RMSNorm, RotaryEmbedding

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class MistralConfig(LlamaConfig):
    """Configuration of the Mistral model."""

    sliding_window: int = 4096
    sliding_window_chunk_size: int = 0

    def __post_init__(self):
        super().__post_init__()
        if self.sliding_window_chunk_size == 0:
            # chunk size same as sliding window by default
            self.sliding_window_chunk_size = self.sliding_window
        self.max_sequence_length = -1
        logger.info(
            "Using sliding window attention, setting %s to -1",
            bold("max_sequence_length"),
        )


class MistralAttention(nn.Module):  # pylint: disable=too-many-instance-attributes
    """Same as LlamaAttention, but with sliding window attention using a rolling buffer cache."""

    def __init__(self, config: MistralConfig, rotary_embedding: RotaryEmbedding):
        self.rotary_embedding = rotary_embedding
        self.hidden_size = config.hidden_size
        self.head_dim = config.head_dim
        self.num_q_heads = config.num_attention_heads
        self.num_kv_heads = config.num_key_value_heads
        self.qkv_proj = nn.MultiLinear(
            in_features=config.hidden_size,
            out_features=[
                self.num_q_heads * self.head_dim,
                self.num_kv_heads * self.head_dim,
                self.num_kv_heads * self.head_dim,
            ],
            bias=False,
        )
        self.o_proj = nn.Linear(config.hidden_size, config.hidden_size, bias=False)
        self.k_cache = nn.KVCache(config.max_sequence_length, [self.num_kv_heads, self.head_dim])
        self.v_cache = nn.KVCache(config.max_sequence_length, [self.num_kv_heads, self.head_dim])

    def forward(  # pylint: disable=too-many-locals
        self,
        hidden_states: Tensor,
        attention_mask: Tensor,
        total_seq_len: tir.Var,
    ):
        """Forward pass of MistralAttention, performing QKV."""
        d, h_q, h_kv, t = self.head_dim, self.num_q_heads, self.num_kv_heads, total_seq_len
        b, s, _ = hidden_states.shape
        assert b == 1, "Only support batch size 1 at this moment."

        q, k, v = self.qkv_proj(hidden_states)
        q = op.reshape(q, (b, s, h_q, d))
        k = op.reshape(k, (b, s, h_kv, d))
        v = op.reshape(v, (b, s, h_kv, d))
        q, k = self.rotary_embedding(q, k, t - s)

        self.k_cache.append(op.squeeze(k, axis=0))
        self.v_cache.append(op.squeeze(v, axis=0))
        k = op.reshape(self.k_cache.view(t), (b, t, h_kv, d))
        v = op.reshape(self.v_cache.view(t), (b, t, h_kv, d))
        if h_kv != h_q:
            k = k.repeat(h_q // h_kv, axis=2)
            v = v.repeat(h_q // h_kv, axis=2)
        q = q.permute_dims([0, 2, 1, 3])  # [b, h, s, d]
        k = k.permute_dims([0, 2, 1, 3])  # [b, h, t, d]
        v = v.permute_dims([0, 2, 1, 3])  # [b, h, t, d]
        attn_weights = op.matmul(
            q, k.permute_dims([0, 1, 3, 2])  # [b, h, s, d] x [b, h, d, t] = [b, h, s, t]
        ) / math.sqrt(d)
        dtype = attn_weights.dtype
        attn_weights = attn_weights.maximum(tir.min_value(dtype)).minimum(attention_mask)
        if dtype == "float32":
            attn_weights = op.softmax(attn_weights, axis=-1)
        else:
            attn_weights = op.softmax(attn_weights.astype("float32"), axis=-1).astype(dtype)
        # [b, h, s, t] x [b, h, t, d] => [b, h, s, d] => [b, s, h, d]
        output = op.matmul(attn_weights, v)
        return self.o_proj(output.permute_dims([0, 2, 1, 3]).reshape((b, s, h_q * d)))


class MistralDecoderLayer(nn.Module):
    """Exact same as LlamaDecoderLayer."""

    def __init__(self, config: MistralConfig, rotary_embedding: RotaryEmbedding):
        rms_norm_eps = config.rms_norm_eps
        self.self_attn = MistralAttention(config, rotary_embedding)
        self.mlp = LlamaFFN(config)
        self.input_layernorm = RMSNorm(config.hidden_size, -1, rms_norm_eps, bias=False)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, -1, rms_norm_eps, bias=False)

    def forward(self, hidden_states: Tensor, attention_mask: Tensor, total_seq_len: tir.Var):
        """Forward pass of a decoder layer; calculate attention, and add an residual connection."""
        hidden_states = (
            self.self_attn(self.input_layernorm(hidden_states), attention_mask, total_seq_len)
            + hidden_states
        )
        hidden_states = self.mlp(self.post_attention_layernorm(hidden_states)) + hidden_states
        return hidden_states


class MistralModel(nn.Module):
    """Exact same as LlamaModel."""

    def __init__(self, config: MistralConfig):
        assert config.hidden_size % config.num_attention_heads == 0
        rotary_embedding = RotaryEmbedding(config)
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList(
            [MistralDecoderLayer(config, rotary_embedding) for _ in range(config.num_hidden_layers)]
        )
        self.norm = RMSNorm(config.hidden_size, -1, config.rms_norm_eps, bias=False)

    def forward(self, inputs: Tensor, total_seq_len: tir.Var, attention_mask: Tensor):
        """Forward pass of the model, passing through all decoder layers."""
        hidden_states = self.embed_tokens(inputs)
        for layer in self.layers:
            hidden_states = layer(hidden_states, attention_mask, total_seq_len)
        hidden_states = self.norm(hidden_states)
        return hidden_states


class MistralForCasualLM(nn.Module):
    """Same as LlamaForCausalLM, except for the use of sliding window attention."""

    def __init__(self, config: MistralConfig):
        self.model = MistralModel(config)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.vocab_size = config.vocab_size
        self.dtype = "float32"

    def to(self, dtype: Optional[str] = None):
        super().to(dtype=dtype)
        if dtype is not None:
            self.dtype = dtype

    def forward(self, inputs: Tensor, total_seq_len: tir.Var, attention_mask: Tensor):
        """Forward pass."""

        def _index(x: te.Tensor):  # x[:-1,:]
            b, s, d = x.shape
            return te.compute((b, 1, d), lambda i, _, k: x[i, s - 1, k], name="index")

        hidden_states = self.model(inputs, total_seq_len, attention_mask)
        hidden_states = op.tensor_expr_op(_index, name_hint="index", args=[hidden_states])
        logits = self.lm_head(hidden_states)
        if logits.dtype != "float32":
            logits = logits.astype("float32")
        return logits

    def prefill(self, inputs: Tensor, total_seq_len: tir.Var):
        """Prefilling the prompt."""

        def _attention_mask(batch_size, seq_len, total_seq_len):
            return te.compute(
                (batch_size, 1, seq_len, total_seq_len),
                lambda b, _, i, j: tir.if_then_else(
                    i < j - (total_seq_len - seq_len),
                    tir.min_value(self.dtype),
                    tir.max_value(self.dtype),
                ),
                name="attention_mask_prefill",
            )

        batch_size, seq_len = inputs.shape
        attention_mask = op.tensor_expr_op(
            _attention_mask,
            name_hint="attention_mask_prefill",
            args=[batch_size, seq_len, total_seq_len],
        )
        return self.forward(inputs, total_seq_len, attention_mask)

    def decode(self, inputs: Tensor, total_seq_len: tir.Var):
        """Decoding step."""
        batch_size, seq_len = inputs.shape
        attention_mask = op.full(
            shape=[batch_size, 1, seq_len, total_seq_len],
            fill_value=tir.max_value(self.dtype),
            dtype=self.dtype,
        )
        return self.forward(inputs, total_seq_len, attention_mask)

    def softmax_with_temperature(self, logits: Tensor, temperature: Tensor):
        """Softmax."""
        return op.softmax(logits / temperature, axis=-1)

    def get_default_spec(self):
        """Needed for ``export_tvm()``."""
        batch_size = 1
        mod_spec = {
            "prefill": {
                "inputs": nn.spec.Tensor([batch_size, "seq_len"], "int32"),
                "total_seq_len": int,
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "packed",
                },
            },
            "decode": {
                "inputs": nn.spec.Tensor([batch_size, 1], "int32"),
                "total_seq_len": int,
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "packed",
                },
            },
            "softmax_with_temperature": {
                "logits": nn.spec.Tensor([1, 1, "vocab_size"], "float32"),
                "temperature": nn.spec.Tensor([], "float32"),
                "$": {
                    "param_mode": "none",
                    "effect_mode": "none",
                },
            },
        }
        return nn.spec.ModuleSpec.from_raw(mod_spec, self)