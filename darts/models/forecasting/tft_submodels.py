"""
Implementation of ``nn.Modules`` for temporal fusion transformer.
"""
from abc import ABC, abstractmethod
import math
from typing import Dict, Tuple, List, Union, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import rnn

# # # 

"""
Implementations of flexible GRU and LSTM that can handle sequences of length 0.
"""

HiddenState = Union[Tuple[torch.Tensor, torch.Tensor], torch.Tensor]

# 0: all False, 1: all True, 2: optimal
adaption_case = 0
# setting GRN, VSN and RESAMPLE to True might have better learning rates

if adaption_case == 0:
    USE_ADAPTION_GLU = False  # use True, see https://leimao.github.io/blog/Gated-Linear-Units/
    USE_ADAPTION_GRN = False
    USE_ADAPTION_VSN = False
    USE_ADAPTION_RESAMPLE = False
    USE_ADAPTION_SCALED_DOT_TRANSFORM = False  # this doesn't exist anymore
    USE_ADAPTION_NO_ATTENTION_DROPOUT = False
    USE_ADAPTION_NO_BIAS_IMHA = False
    USE_BUILTIN_LSTM = False
elif adaption_case == 1:
    USE_ADAPTION_GLU = True  # use True, see https://leimao.github.io/blog/Gated-Linear-Units/
    USE_ADAPTION_GRN = True
    USE_ADAPTION_VSN = True
    USE_ADAPTION_RESAMPLE = True
    USE_ADAPTION_SCALED_DOT_TRANSFORM = True  # this doesn't exist anymore
    USE_ADAPTION_NO_ATTENTION_DROPOUT = True
    USE_ADAPTION_NO_BIAS_IMHA = True
    USE_BUILTIN_LSTM = False
elif adaption_case == 2:
    USE_ADAPTION_GLU = False  # use True, see https://leimao.github.io/blog/Gated-Linear-Units/
    USE_ADAPTION_GRN = True
    USE_ADAPTION_VSN = True
    USE_ADAPTION_RESAMPLE = False
    USE_ADAPTION_SCALED_DOT_TRANSFORM = False  # this doesn't exist anymore
    USE_ADAPTION_NO_ATTENTION_DROPOUT = False
    USE_ADAPTION_NO_BIAS_IMHA = True
    USE_BUILTIN_LSTM = False
else:
    raise ValueError('adaption case not corect')


class QuantileLoss(nn.Module):
    """From: https://medium.com/the-artificial-impostor/quantile-regression-part-2-6fdbc26b2629"""

    def __init__(self, quantiles):
        """
        Arguments:
            quantiles: list of quantiles
        """
        super().__init__()
        self.quantiles = quantiles

    def forward(self, preds, target):
        assert not target.requires_grad
        assert preds.shape[0] == target.shape[0]
        losses = []
        for i, q in enumerate(self.quantiles):
            errors = target - preds[:, i]
            losses.append(
                torch.max(
                    (q - 1) * errors,
                    q * errors
                ).unsqueeze(1))
        loss = torch.mean(
            torch.sum(torch.cat(losses, dim=1), dim=1))
        return loss


if not USE_BUILTIN_LSTM:
    class RNN(ABC, nn.RNNBase):
        """
        Base class flexible RNNs.

        Forward function can handle sequences of length 0.
        """

        @abstractmethod
        def handle_no_encoding(self,
                               hidden_state: HiddenState,
                               no_encoding: torch.BoolTensor,
                               initial_hidden_state: HiddenState) -> HiddenState:
            """
            Mask the hidden_state where there is no encoding.

            Args:
                hidden_state (HiddenState): hidden state where some entries need replacement
                no_encoding (torch.BoolTensor): positions that need replacement
                initial_hidden_state (HiddenState): hidden state to use for replacement

            Returns:
                HiddenState: hidden state with propagated initial hidden state where appropriate
            """
            pass

        @abstractmethod
        def init_hidden_state(self,
                              x: torch.Tensor) -> HiddenState:
            """
            Initialise a hidden_state.

            Args:
                x (torch.Tensor): network input

            Returns:
                HiddenState: default (zero-like) hidden state
            """
            pass

        @abstractmethod
        def repeat_interleave(self,
                              hidden_state: HiddenState,
                              n_samples: int) -> HiddenState:
            """
            Duplicate the hidden_state n_samples times.

            Args:
                hidden_state (HiddenState): hidden state to repeat
                n_samples (int): number of repetitions

            Returns:
                HiddenState: repeated hidden state
            """
            pass

        def forward(self,
                    x: Union[rnn.PackedSequence, torch.Tensor],
                    hx: HiddenState = None,
                    lengths: torch.LongTensor = None,
                    enforce_sorted: bool = True) -> Tuple[Union[rnn.PackedSequence, torch.Tensor], HiddenState]:
            """
            Forward function of rnn that allows zero-length sequences.

            Functions as normal for RNN. Only changes output if lengths are defined.

            Args:
                x (Union[rnn.PackedSequence, torch.Tensor]): input to RNN. either packed sequence or tensor of
                    padded sequences
                hx (HiddenState, optional): hidden state. Defaults to None.
                lengths (torch.LongTensor, optional): lengths of sequences. If not None, used to determine correct returned
                    hidden state. Can contain zeros. Defaults to None.
                enforce_sorted (bool, optional): if lengths are passed, determines if RNN expects them to be sorted.
                    Defaults to True.

            Returns:
                Tuple[Union[rnn.PackedSequence, torch.Tensor], HiddenState]: output and hidden state.
                    Output is packed sequence if input has been a packed sequence.
            """
            if isinstance(x, rnn.PackedSequence) or lengths is None:
                assert lengths is None, "cannot combine x of type PackedSequence with lengths argument"
                return super().forward(x, hx=hx)
            else:
                min_length = lengths.min()
                max_length = lengths.max()
                assert min_length >= 0, "sequence lengths must be great equals 0"

                if max_length == 0:
                    hidden_state = self.init_hidden_state(x)
                    if self.batch_first:
                        out = torch.zeros(lengths.shape[0], x.shape[1], self.hidden_size, dtype=x.dtype, device=x.device)
                    else:
                        out = torch.zeros(x.shape[0], lengths.shape[0], self.hidden_size, dtype=x.dtype, device=x.device)
                    return out, hidden_state
                else:
                    pack_lengths = lengths.where(lengths > 0, torch.ones_like(lengths))
                    packed_out, hidden_state = super().forward(
                        rnn.pack_padded_sequence(
                            x, pack_lengths.cpu(), enforce_sorted=enforce_sorted, batch_first=self.batch_first
                        ),
                        hx=hx,
                    )
                    # replace hidden cell with initial input if encoder_length is zero to determine correct initial state
                    if min_length == 0:
                        no_encoding = (lengths == 0)[
                            None, :, None
                        ]  # shape: n_layers * n_directions x batch_size x hidden_size
                        if hx is None:
                            initial_hidden_state = self.init_hidden_state(x)
                        else:
                            initial_hidden_state = hx
                        # propagate initial hidden state when sequence length was 0
                        hidden_state = self.handle_no_encoding(hidden_state, no_encoding, initial_hidden_state)

                    # return unpacked sequence
                    out, _ = rnn.pad_packed_sequence(packed_out, batch_first=self.batch_first)
                    return out, hidden_state


    class LSTM(RNN, nn.LSTM):
        """LSTM that can handle zero-length sequences"""

        def handle_no_encoding(self,
                               hidden_state: HiddenState,
                               no_encoding: torch.BoolTensor,
                               initial_hidden_state: HiddenState) -> HiddenState:

            hidden, cell = hidden_state
            hidden = hidden.masked_scatter(no_encoding, initial_hidden_state[0])
            cell = cell.masked_scatter(no_encoding, initial_hidden_state[0])
            return hidden, cell

        def init_hidden_state(self,
                              x: torch.Tensor) -> HiddenState:

            num_directions = 2 if self.bidirectional else 1
            if self.batch_first:
                batch_size = x.shape[0]
            else:
                batch_size = x.shape[1]
            hidden = torch.zeros(
                (self.num_layers * num_directions, batch_size, self.hidden_size),
                device=x.device,
                dtype=x.dtype,
            )
            cell = torch.zeros(
                (self.num_layers * num_directions, batch_size, self.hidden_size),
                device=x.device,
                dtype=x.dtype,
            )
            return hidden, cell

        def repeat_interleave(self,
                              hidden_state: HiddenState,
                              n_samples: int) -> HiddenState:

            hidden, cell = hidden_state
            hidden = hidden.repeat_interleave(n_samples, 1)
            cell = cell.repeat_interleave(n_samples, 1)
            return hidden, cell

else:
    LSTM = nn.LSTM


class TimeDistributedEmbeddingBag(nn.EmbeddingBag):
    def __init__(self,
                 *args,
                 batch_first: bool = False,
                 **kwargs):

        super().__init__(*args, **kwargs)
        self.batch_first = batch_first

    def forward(self, x):

        if len(x.size()) <= 2:
            return super().forward(x)

        # Squash samples and timesteps into a single axis
        x_reshape = x.contiguous().view(-1, x.shape[-1])  # (samples * timesteps, input_size)

        y = super().forward(x_reshape)

        # We have to reshape Y
        if self.batch_first:
            y = y.contiguous().view(x.shape[0], -1, y.shape[-1])  # (samples, timesteps, output_size)
        else:
            y = y.view(-1, x.shape[1], y.shape[-1])  # (timesteps, samples, output_size)
        return y


class MultiEmbedding(nn.Module):
    def __init__(self,
                 embedding_sizes: Dict[str, Tuple[int, int]],
                 categorical_groups: Dict[str, List[str]],
                 embedding_paddings: List[str],
                 x_categoricals: List[str],
                 max_embedding_size: int):

        super().__init__()
        self.embedding_sizes = {key: list(size_tuple) for key, size_tuple in embedding_sizes.items()}
        self.categorical_groups = categorical_groups
        self.embedding_paddings = embedding_paddings
        self.max_embedding_size = max_embedding_size
        self.x_categoricals = x_categoricals

        self.embeddings = self.init_embeddings()

    def init_embeddings(self):
        embeddings = nn.ModuleDict()
        for name in self.embedding_sizes:
            embedding_size = min(self.embedding_sizes[name][1], self.max_embedding_size)
            self.embedding_sizes[name][1] = embedding_size

            if name in self.categorical_groups:  # embedding bag if related embeddings
                embeddings[name] = TimeDistributedEmbeddingBag(
                    self.embedding_sizes[name][0], embedding_size, mode="sum", batch_first=True
                )
            else:
                if name in self.embedding_paddings:
                    padding_idx = 0
                else:
                    padding_idx = None
                embeddings[name] = nn.Embedding(
                    self.embedding_sizes[name][0],
                    embedding_size,
                    padding_idx=padding_idx,
                )
        return embeddings

    def names(self):
        return list(self.keys())

    def items(self):
        return self.embeddings.items()

    def keys(self):
        return self.embeddings.keys()

    def values(self):
        return self.embeddings.values()

    def __getitem__(self, name: str):
        return self.embeddings[name]

    def forward(self, x):
        input_vectors = {}
        for name, emb in self.embeddings.items():
            if name in self.categorical_groups:
                input_vectors[name] = emb(
                    x[
                        ...,
                        [self.x_categoricals.index(cat_name) for cat_name in self.categorical_groups[name]],
                    ]
                )
            else:
                input_vectors[name] = emb(x[..., self.x_categoricals.index(name)])
        return input_vectors


class TimeDistributed(nn.Module):
    """==========================================CHECKED==========================================
    TODO: NOT USED IN TFT BUT MAYBE REPLACE WITH ResampleNorm() OR TimeDistributedInterpolation()
    Takes any module and stacks the time dimension with the batch dimenison of inputs before apply the module
    From: https://discuss.pytorch.org/t/any-pytorch-function-can-work-as-keras-timedistributed/1346/4"""
    def __init__(self, module: nn.Module, batch_first: bool = False):
        super().__init__()
        self.module = module
        self.batch_first = batch_first

    def forward(self, x):

        if len(x.size()) <= 2:
            return self.module(x)

        # Squash samples and timesteps into a single axis
        x_reshape = x.contiguous().view(-1, x.shape[-1])  # (samples * timesteps, input_size)

        y = self.module(x_reshape)

        # We have to reshape Y
        if self.batch_first:
            y = y.contiguous().view(x.shape[0], -1, y.shape[-1])  # (samples, timesteps, output_size)
        else:
            y = y.view(-1, x.shape[1], y.shape[-1])  # (timesteps, samples, output_size)
        return y


class TimeDistributedInterpolation(nn.Module):
    """==========================================CHECKED==========================================
    interpolates input size to output size.
    This is lke TimeDistributed with interpolation
    """
    def __init__(self,
                 output_size: int,
                 batch_first: bool = False):
        super().__init__()
        self.output_size = output_size
        self.batch_first = batch_first

    def interpolate(self, x):
        upsampled = F.interpolate(x.unsqueeze(1), self.output_size, mode="linear", align_corners=True).squeeze(1)
        return upsampled

    def forward(self, x):

        if len(x.size()) <= 2:
            return self.interpolate(x)

        # Squash samples and timesteps into a single axis
        x_reshape = x.contiguous().view(-1, x.shape[-1])  # (samples * timesteps, input_size)

        y = self.interpolate(x_reshape)

        # We have to reshape Y
        if self.batch_first:
            y = y.contiguous().view(x.shape[0], -1, y.shape[-1])  # (samples, timesteps, output_size)
        else:
            y = y.view(-1, x.shape[1], y.shape[-1])  # (timesteps, samples, output_size)

        return y


class GatedLinearUnit(nn.Module):
    """==========================================CHECKED=========================================="""
    def __init__(self,
                 input_size: int,
                 hidden_size: int = None,
                 dropout: float = None):

        """Applies a Gated Linear Unit (GLU) to an input, see equation (5).
            Args:
                input_size: input size of x
                hidden_size: Dimension of GLU
                dropout: Dropout rate to apply if any
        """

        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size or input_size

        self.dropout = nn.Dropout(dropout) if dropout is not None else dropout

        if not USE_ADAPTION_GLU:
            # TODO: WHY IS IT hidden_size * 2??? is it because glu() splits half along a given dimension?
            self.fc = nn.Linear(self.input_size, self.hidden_size * 2)

            self.init_weights()
        else:
            self.fc1 = nn.Linear(self.input_size, self.hidden_size)
            self.fc2 = nn.Linear(self.input_size, self.hidden_size)
            self.sigmoid = nn.Sigmoid()
            self.init_weights()

    def init_weights(self):
        for n, p in self.named_parameters():
            if "bias" in n:
                torch.nn.init.zeros_(p)
            elif "fc" in n:
                torch.nn.init.xavier_uniform_(p)

    def forward(self, x):
        if self.dropout is not None:
            x = self.dropout(x)

        if not USE_ADAPTION_GLU:
            x = self.fc(x)
            x = F.glu(x, dim=-1)
        else:  # I actually think this is better, see https://leimao.github.io/blog/Gated-Linear-Units/
            x_sig = self.sigmoid(self.fc1(x))
            x = self.fc2(x)
            x = torch.mul(x_sig, x)
        return x


class ResampleNorm(nn.Module):
    """==========================================SEMI-CHECKED==========================================
    Added option USE_ADAPTION_RESAMPLE to see if it's better just with TimeDistributedInterpolation.
    Resamples an input to an output size
    """

    def __init__(self,
                 input_size: int,
                 output_size: int = None):

        super().__init__()

        self.input_size = input_size
        self.output_size = output_size or input_size

        if self.input_size != self.output_size:
            self.resample = TimeDistributedInterpolation(self.output_size, batch_first=True)

        self.mask = nn.Parameter(torch.zeros(self.output_size, dtype=torch.float))
        self.gate = nn.Sigmoid()
        self.norm = nn.LayerNorm(self.output_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.input_size != self.output_size:
            x = self.resample(x)

        x = x * self.gate(self.mask) * 2.0

        output = self.norm(x)
        return output


class AddNorm(nn.Module):
    """==========================================CHECKED=========================================="""
    def __init__(self,
                 input_size: int,
                 skip_size: int = None):
        """Applies skip connection followed by layer normalisation.
        """

        super().__init__()

        self.input_size = input_size
        self.skip_size = skip_size or input_size

        if self.input_size != self.skip_size:
            self.resample = TimeDistributedInterpolation(self.input_size, batch_first=True)

        self.norm = nn.LayerNorm(self.input_size)

    def forward(self, x: torch.Tensor, skip: torch.Tensor):
        """Norm(Add)"""

        if self.input_size != self.skip_size:
            skip = self.resample(skip)

        output = self.norm(x + skip)
        return output


class GateAddNorm(nn.Module):
    """==========================================CHECKED==========================================
    Equation (2)"""
    def __init__(self,
                 input_size: int,
                 hidden_size: int = None,
                 skip_size: int = None,
                 dropout: float = None):

        super().__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size or input_size
        self.skip_size = skip_size or self.hidden_size
        self.dropout = dropout

        self.glu = GatedLinearUnit(self.input_size, hidden_size=self.hidden_size, dropout=self.dropout)
        self.add_norm = AddNorm(self.hidden_size, skip_size=self.skip_size)

    def forward(self, x, skip):
        # skip is the same as residual
        output = self.glu(x)
        output = self.add_norm(output, skip)
        return output


class GatedResidualNetwork(nn.Module):
    """==========================================CHECKED==========================================
    Top right graph in Figure 2 and formulas (2) -- (5)

    """
    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 output_size: int,
                 dropout: float = 0.1,
                 context_size: Optional[int] = None):
        """Applies the gated residual network (GRN) as defined in paper.
        Args:
            hidden_size: Internal state size
            output_size: Size of output layer
            dropout: Dropout rate if dropout is applied
            context_size: size of optional context vector
        """

        super().__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.context_size = context_size
        self.hidden_size = hidden_size
        self.dropout = dropout

        # convert raw input into output size for residuals
        if self.input_size != self.output_size:
            if not USE_ADAPTION_RESAMPLE:
                self.resample_norm = ResampleNorm(self.input_size, self.output_size)
            else:
                self.resample_norm = TimeDistributedInterpolation(self.output_size, batch_first=True)

        try:
            self.fc1 = nn.Linear(self.input_size, self.hidden_size)
        except ZeroDivisionError:
            d = 1
        if self.context_size is not None:
            # according to equation (4), no context bias
            self.context = nn.Linear(self.context_size, self.hidden_size, bias=False)
        self.elu = nn.ELU()

        self.fc2 = nn.Linear(self.hidden_size, self.hidden_size)

        if not USE_ADAPTION_GRN:
            # TODO: what does this do?
            self.init_weights()

        self.gate_add_norm = GateAddNorm(
            input_size=self.hidden_size,
            skip_size=self.output_size,
            hidden_size=self.output_size,
            dropout=self.dropout,
        )

    def init_weights(self):
        # p contains the parameter values
        for name, p in self.named_parameters():
            if "bias" in name:
                torch.nn.init.zeros_(p)
            elif "fc1" in name or "fc2" in name:
                torch.nn.init.kaiming_normal_(p, a=0, mode="fan_in", nonlinearity="leaky_relu")
            elif "context" in name:
                torch.nn.init.xavier_uniform_(p)

    def forward(self, x, context=None, residual=None):
        """seems to be correct"""

        # residual, also called `skip` basically bypasses until add_norm
        if self.input_size != self.output_size:
            residual = self.resample_norm(x)
        else:
            residual = x

        x = self.fc1(x)
        if context is not None:
            context = self.context(context)
            x = x + context
        x = self.elu(x)
        x = self.fc2(x)
        x = self.gate_add_norm(x, residual)
        return x


class VariableSelectionNetwork(nn.Module):
    """==========================================SEMI-----CHECKED=========================================="""
    def __init__(self,
                 input_sizes: Dict[str, int],
                 hidden_size: int,
                 input_embedding_flags: Optional[Dict[str, bool]] = None,
                 dropout: float = 0.1,
                 context_size: Optional[int] = None,
                 single_variable_grns: Optional[Dict[str, GatedResidualNetwork]] = None,
                 prescalers: Optional[torch.nn.ModuleDict] = None):
        """
        Calcualte weights for ``num_inputs`` variables  which are each of size ``input_size``
        """
        super().__init__()

        self.input_sizes = input_sizes
        self.hidden_size = hidden_size
        self.input_embedding_flags = {} if input_embedding_flags is None else input_embedding_flags
        self.dropout = dropout
        self.context_size = context_size
        single_variable_grns = {} if single_variable_grns is None else single_variable_grns
        prescalers = {} if prescalers is None else prescalers

        self.num_inputs = len(self.input_sizes)
        self.input_sizes_total = sum(self.input_sizes.values())

        if self.num_inputs > 1:
            # right side of figure 2 bottom right graph
            self.vars_flattened_grn = GatedResidualNetwork(
                input_size=self.input_sizes_total,
                hidden_size=min(self.hidden_size, self.num_inputs) if not USE_ADAPTION_VSN else self.hidden_size,
                output_size=self.num_inputs,
                dropout=self.dropout,
                context_size=self.context_size
            )

        # left side of figure 2 bottom right graph
        # TODO: Check prescalers, embeddings, linear transformations
        self.vars_single_grns = nn.ModuleDict()
        self.prescalers = nn.ModuleDict()
        for name, input_size in self.input_sizes.items():
            if name in single_variable_grns:
                self.vars_single_grns[name] = single_variable_grns[name]
            elif self.input_embedding_flags.get(name, False):
                if not USE_ADAPTION_RESAMPLE:
                    self.vars_single_grns[name] = ResampleNorm(input_size, self.hidden_size)
                else:
                    self.vars_single_grns[name] = TimeDistributedInterpolation(self.hidden_size, batch_first=True)
            else:
                self.vars_single_grns[name] = GatedResidualNetwork(
                    input_size=input_size,
                    hidden_size=min(input_size, self.hidden_size) if not USE_ADAPTION_VSN else self.hidden_size,
                    output_size=self.hidden_size,
                    dropout=self.dropout
                )

            if name in prescalers:  # reals need to be first scaled up
                self.prescalers[name] = prescalers[name]
            elif not self.input_embedding_flags.get(name, False):
                self.prescalers[name] = nn.Linear(1, input_size)

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x: Dict[str, torch.Tensor], context: torch.Tensor = None):
        # transform variables and GRN for transformed variables (left side of figure 2 bottom right graph)
        vars_out = []
        transformed = []
        for var_name in self.input_sizes.keys():
            # select embedding belonging to a single input variable
            var_transformed = x[var_name]
            if var_name in self.prescalers:
                var_transformed = self.prescalers[var_name](var_transformed)
            vars_out.append(self.vars_single_grns[var_name](var_transformed))
            transformed.append(var_transformed)

        # TODO: Check stack/cat with dim=-1
        vars_out = torch.stack(vars_out, dim=-1)

        # calculate variable weights with flattened variables (right side of figure 2 bottom right graph)
        flattened = torch.cat(transformed, dim=-1)
        selection_weights = self.vars_flattened_grn(flattened, context)
        selection_weights = self.softmax(selection_weights).unsqueeze(-2)

        # join single variable with variable selection weigths (top of figure 2 bottom right graph)
        outputs = vars_out * selection_weights
        outputs = outputs.sum(dim=-1)
        return outputs, selection_weights


class PositionalEncoder(torch.nn.Module):
    def __init__(self, d_model, max_seq_len=160):
        super().__init__()
        assert d_model % 2 == 0, "model dimension has to be multiple of 2 (encode sin(pos) and cos(pos))"
        self.d_model = d_model
        pe = torch.zeros(max_seq_len, d_model)
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = math.sin(pos / (10000 ** ((2 * i) / d_model)))
                pe[pos, i + 1] = math.cos(pos / (10000 ** ((2 * (i + 1)) / d_model)))
        pe = pe.unsqueeze(0)
        self.register_buffer("pe", pe)

    def forward(self, x):
        with torch.no_grad():
            x = x * math.sqrt(self.d_model)
            seq_len = x.shape[0]
            pe = self.pe[:, :seq_len].view(seq_len, 1, self.d_model)
            x = x + pe
            return x


class ScaledDotProductAttention(nn.Module):
    """==========================================CHECKED==========================================
    ScaledDotProductAttention is an self-attention mechanism to learn long-term relationships across different time
    steps. It scales values V based on relationship between Keys K and queries Q.
    From equations (9) -- (10)"""

    def __init__(self):

        super(ScaledDotProductAttention, self).__init__()

        self.softmax = nn.Softmax(dim=2)

    def forward(self, q, k, v, mask=None):
        """
        Attributes:
            q: Queries matrix of dimension (N x d_attention)
            k: Keys matrix of dimension (N x d_attention)
            v: Values matrix of dimension (N x d_Values)
            mask: masking if required -- sets softmax to very large value
        """

        attn = torch.bmm(q, k.transpose(1, 2))

        dimension = torch.sqrt(torch.tensor(k.shape[-1]).to(torch.float32))
        attn = attn / dimension

        if mask is not None:
            attn = attn.masked_fill(mask, float('-inf'))
        attn = self.softmax(attn)

        output = torch.bmm(attn, v)
        return output, attn


class InterpretableMultiHeadAttention(nn.Module):
    """==========================================CHECKED==========================================
    Equations (13) -- (16)"""

    def __init__(self,
                 n_head: int,
                 d_model: int,
                 dropout: Optional[float] = None):
        """
        Attributes:
            n_head: number of heads
            d_model: TFT state dimensionality
            d_k: Key/query dimensionality per head
            d_v: value dimensionality
            q_layers: list of queries across heads
            k_layers: list of keys across heads
            v_layers: list of values across heads
            attention: scaled dot product attention layer
            w_h: output head weight matrix to project internal state to the original TFT state size
        """

        super(InterpretableMultiHeadAttention, self).__init__()

        self.n_head = n_head
        self.d_model = d_model
        self.d_k = self.d_q = self.d_v = d_model // n_head

        if not USE_ADAPTION_NO_ATTENTION_DROPOUT:
            self.dropout = nn.Dropout(p=dropout) if dropout is not None else dropout

        bias = True if not USE_ADAPTION_NO_BIAS_IMHA else False
        self.q_layers = nn.ModuleList([nn.Linear(self.d_model, self.d_q, bias=bias) for _ in range(self.n_head)])
        self.k_layers = nn.ModuleList([nn.Linear(self.d_model, self.d_k, bias=bias) for _ in range(self.n_head)])
        # use same value layer to facilitate interpretation
        self.v_layer = nn.Linear(self.d_model, self.d_v, bias=bias)

        self.attention = ScaledDotProductAttention()
        self.w_h = nn.Linear(self.d_v, self.d_model, bias=False)

        self.init_weights()

    def init_weights(self):
        for name, p in self.named_parameters():
            if "bias" not in name:  # layer weigths
                torch.nn.init.xavier_uniform_(p)
            else:  # bias
                torch.nn.init.zeros_(p)

    def forward(self, q, k, v, mask=None) -> Tuple[torch.Tensor, torch.Tensor]:
        heads = []
        attns = []
        vs = self.v_layer(v)
        for i in range(self.n_head):
            qs = self.q_layers[i](q)
            ks = self.k_layers[i](k)
            head, attn = self.attention(qs, ks, vs, mask)
            if not USE_ADAPTION_NO_ATTENTION_DROPOUT:
                if self.dropout is not None:
                    head = self.dropout(head)
            heads.append(head)
            attns.append(attn)

        head = torch.stack(heads, dim=2) if self.n_head > 1 else heads[0]
        attn = torch.stack(attns, dim=2)

        outputs = torch.mean(head, dim=2) if self.n_head > 1 else head

        outputs = self.w_h(outputs)
        if not USE_ADAPTION_NO_ATTENTION_DROPOUT:
            if self.dropout is not None:
                outputs = self.dropout(outputs)

        return outputs, attn