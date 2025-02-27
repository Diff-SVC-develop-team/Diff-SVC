import math

import torch
import torch.nn as nn
from torch.nn import functional as F

from modules.commons.common_layers import SinusoidalPositionalEmbedding, EncSALayer, BatchNorm1dTBC
from modules.commons.espnet_positional_embedding import RelPositionalEncoding
from utils.hparams import hparams

DEFAULT_MAX_SOURCE_POSITIONS = 2000
DEFAULT_MAX_TARGET_POSITIONS = 2000


class TransformerEncoderLayer(nn.Module):
    def __init__(self, hidden_size, dropout, kernel_size=None, num_heads=2, norm='ln'):
        super().__init__()
        self.op = EncSALayer(
            hidden_size, num_heads, dropout=dropout,
            attention_dropout=0.0, relu_dropout=dropout,
            kernel_size=kernel_size
            if kernel_size is not None else hparams['enc_ffn_kernel_size'],
            padding=hparams['ffn_padding'],
            norm=norm, act=hparams['ffn_act']
        )

    def forward(self, x, **kwargs):
        return self.op(x, **kwargs)


######################
# fastspeech modules
######################
class LayerNorm(torch.nn.LayerNorm):
    """Layer normalization module.
    :param int nout: output dim size
    :param int dim: dimension to be normalized
    """

    def __init__(self, nout, dim=-1):
        """Construct an LayerNorm object."""
        super(LayerNorm, self).__init__(nout, eps=1e-12)
        self.dim = dim

    def forward(self, x):
        """Apply layer normalization.
        :param torch.Tensor x: input tensor
        :return: layer normalized tensor
        :rtype torch.Tensor
        """
        if self.dim == -1:
            return super(LayerNorm, self).forward(x)
        return super(LayerNorm, self).forward(x.transpose(1, -1)).transpose(1, -1)


class DurationPredictor(torch.nn.Module):
    """Duration predictor module.
    This is a module of duration predictor described in `FastSpeech: Fast, Robust and Controllable Text to Speech`_.
    The duration predictor predicts a duration of each frame in log domain from the hidden embeddings of encoder.
    .. _`FastSpeech: Fast, Robust and Controllable Text to Speech`:
        https://arxiv.org/pdf/1905.09263.pdf
    Note:
        The calculation domain of outputs is different between in `forward` and in `inference`. In `forward`,
        the outputs are calculated in log domain but in `inference`, those are calculated in linear domain.
    """

    def __init__(self, idim, n_layers=2, n_chans=384, kernel_size=3, dropout_rate=0.1, offset=1.0, padding='SAME'):
        """Initilize duration predictor module.
        Args:
            idim (int): Input dimension.
            n_layers (int, optional): Number of convolutional layers.
            n_chans (int, optional): Number of channels of convolutional layers.
            kernel_size (int, optional): Kernel size of convolutional layers.
            dropout_rate (float, optional): Dropout rate.
            offset (float, optional): Offset value to avoid nan in log domain.
        """
        super(DurationPredictor, self).__init__()
        self.offset = offset
        self.conv = torch.nn.ModuleList()
        self.kernel_size = kernel_size
        self.padding = padding
        for idx in range(n_layers):
            in_chans = idim if idx == 0 else n_chans
            self.conv += [torch.nn.Sequential(
                torch.nn.ConstantPad1d(((kernel_size - 1) // 2, (kernel_size - 1) // 2)
                                       if padding == 'SAME'
                                       else (kernel_size - 1, 0), 0),
                torch.nn.Conv1d(in_chans, n_chans, kernel_size, stride=1, padding=0),
                torch.nn.ReLU(),
                LayerNorm(n_chans, dim=1),
                torch.nn.Dropout(dropout_rate)
            )]
        if hparams['dur_loss'] in ['mse', 'huber']:
            odims = 1
        elif hparams['dur_loss'] == 'mog':
            odims = 15
        elif hparams['dur_loss'] == 'crf':
            odims = 32
            from torchcrf import CRF
            self.crf = CRF(odims, batch_first=True)
        self.linear = torch.nn.Linear(n_chans, odims)

    def _forward(self, xs, x_masks=None, is_inference=False):
        xs = xs.transpose(1, -1)  # (B, idim, Tmax)
        for f in self.conv:
            xs = f(xs)  # (B, C, Tmax)
            if x_masks is not None:
                xs = xs * (1 - x_masks.float())[:, None, :]

        xs = self.linear(xs.transpose(1, -1))  # [B, T, C]
        xs = xs * (1 - x_masks.float())[:, :, None]  # (B, T, C)
        if is_inference:
            return self.out2dur(xs), xs
        else:
            if hparams['dur_loss'] in ['mse']:
                xs = xs.squeeze(-1)  # (B, Tmax)
        return xs

    def out2dur(self, xs):
        if hparams['dur_loss'] in ['mse']:
            # NOTE: calculate in log domain
            xs = xs.squeeze(-1)  # (B, Tmax)
            dur = torch.clamp(torch.round(xs.exp() - self.offset), min=0).long()  # avoid negative value
        elif hparams['dur_loss'] == 'mog':
            return NotImplementedError
        elif hparams['dur_loss'] == 'crf':
            dur = torch.LongTensor(self.crf.decode(xs)).cuda()
        return dur

    def forward(self, xs, x_masks=None):
        """Calculate forward propagation.
        Args:
            xs (Tensor): Batch of input sequences (B, Tmax, idim).
            x_masks (ByteTensor, optional): Batch of masks indicating padded part (B, Tmax).
        Returns:
            Tensor: Batch of predicted durations in log domain (B, Tmax).
        """
        return self._forward(xs, x_masks, False)

    def inference(self, xs, x_masks=None):
        """Inference duration.
        Args:
            xs (Tensor): Batch of input sequences (B, Tmax, idim).
            x_masks (ByteTensor, optional): Batch of masks indicating padded part (B, Tmax).
        Returns:
            LongTensor: Batch of predicted durations in linear domain (B, Tmax).
        """
        return self._forward(xs, x_masks, True)


class LengthRegulator(torch.nn.Module):
    def __init__(self, pad_value=0.0):
        super(LengthRegulator, self).__init__()
        self.pad_value = pad_value

    def forward(self, dur, dur_padding=None, alpha=1.0):
        """
        Example (no batch dim version):
            1. dur = [2,2,3]
            2. token_idx = [[1],[2],[3]], dur_cumsum = [2,4,7], dur_cumsum_prev = [0,2,4]
            3. token_mask = [[1,1,0,0,0,0,0],
                             [0,0,1,1,0,0,0],
                             [0,0,0,0,1,1,1]]
            4. token_idx * token_mask = [[1,1,0,0,0,0,0],
                                         [0,0,2,2,0,0,0],
                                         [0,0,0,0,3,3,3]]
            5. (token_idx * token_mask).sum(0) = [1,1,2,2,3,3,3]

        :param dur: Batch of durations of each frame (B, T_txt)
        :param dur_padding: Batch of padding of each frame (B, T_txt)
        :param alpha: duration rescale coefficient
        :return:
            mel2ph (B, T_speech)
        """
        assert alpha > 0
        dur = torch.round(dur.float() * alpha).long()
        if dur_padding is not None:
            dur = dur * (1 - dur_padding.long())
        token_idx = torch.arange(1, dur.shape[1] + 1)[None, :, None].to(dur.device)
        dur_cumsum = torch.cumsum(dur, 1)
        dur_cumsum_prev = F.pad(dur_cumsum, [1, -1], mode='constant', value=0)

        pos_idx = torch.arange(dur.sum(-1).max())[None, None].to(dur.device)
        token_mask = (pos_idx >= dur_cumsum_prev[:, :, None]) & (pos_idx < dur_cumsum[:, :, None])
        mel2ph = (token_idx * token_mask.long()).sum(1)
        return mel2ph


class StretchRegulator(torch.nn.Module):
    def forward(self, dur, mel2ph):
        """
        Example (no batch dim version):
            1. dur = [2,4,3]
            2. mel2ph = [1,1,2,2,2,2,3,3,3]
            3. mel2dur = [2,2,4,4,4,4,3,3,3]
            4. bound_mask = [0,1,0,0,0,1,0,0,1]
            5. 1 - bound_mask * mel2dur = [1,-1,1,1,1,-3,1,1,-2] => pad => [0,1,-1,1,1,1,-3,1,1]
            6. stretch_denorm = [0,1,0,1,2,3,0,1,2]

        :param dur: Batch of durations of each frame (B, T_txt)
        :param mel2ph: Batch of mel2ph (B, T_speech)
        :return:
            stretch (B, T_speech)
        """
        dur = F.pad(dur, [1, 0], value=1)  # Avoid dividing by zero
        mel2dur = torch.gather(dur, 1, mel2ph)
        bound_mask = torch.gt(mel2ph[:, 1:], mel2ph[:, :-1])
        bound_mask = F.pad(bound_mask, [0, 1], mode='constant', value=True)
        stretch_delta = 1 - bound_mask * mel2dur
        stretch_delta = F.pad(stretch_delta, [1, -1], mode='constant', value=0)
        stretch_denorm = torch.cumsum(stretch_delta, dim=1)
        stretch = stretch_denorm / mel2dur
        return stretch * (mel2ph > 0)


def mel2ph_to_dur(mel2ph, T_txt, max_dur=None):
    B, _ = mel2ph.shape
    dur = mel2ph.new_zeros(B, T_txt + 1).scatter_add(1, mel2ph, torch.ones_like(mel2ph))
    dur = dur[:, 1:]
    if max_dur is not None:
        dur = dur.clamp(max=max_dur)
    return dur


class FastSpeech2Encoder(nn.Module):
    def __init__(self, embed_tokens, hidden_size, num_layers, ffn_kernel_size=9, dropout=None, num_heads=2,
                 use_pos_embed=False, use_last_norm=True, norm='ln', use_pos_embed_alpha=True):
        super().__init__()
        hidden_size = hparams['hidden_size'] if hidden_size is None else hidden_size
        kernel_size = hparams['enc_ffn_kernel_size'] if ffn_kernel_size is None else ffn_kernel_size
        num_layers = hparams['enc_layers'] if num_layers is None else num_layers
        self.num_layers = num_layers
        embed_dim = self.hidden_size = hidden_size
        self.dropout = dropout if dropout is not None else hparams['dropout']
        self.use_pos_embed = use_pos_embed
        self.use_last_norm = use_last_norm
        if use_pos_embed:
            self.max_source_positions = DEFAULT_MAX_TARGET_POSITIONS
            self.padding_idx = 0
            self.pos_embed_alpha = nn.Parameter(torch.Tensor([1])) if use_pos_embed_alpha else 1
            self.embed_positions = SinusoidalPositionalEmbedding(
                embed_dim, self.padding_idx, init_size=DEFAULT_MAX_TARGET_POSITIONS,
            )

        self.layers = nn.ModuleList([
            TransformerEncoderLayer(self.hidden_size, self.dropout,
                                    kernel_size=kernel_size, num_heads=num_heads)
            for _ in range(self.num_layers)
        ])
        if self.use_last_norm:
            if norm == 'ln':
                self.layer_norm = nn.LayerNorm(embed_dim)
            elif norm == 'bn':
                self.layer_norm = BatchNorm1dTBC(embed_dim)
        else:
            self.layer_norm = None

        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(hidden_size)
        self.padding_idx = 0
        if hparams['rel_pos']:
            self.embed_positions = RelPositionalEncoding(hidden_size, dropout_rate=0.0)
        else:
            self.embed_positions = SinusoidalPositionalEmbedding(
                hidden_size, self.padding_idx, init_size=DEFAULT_MAX_TARGET_POSITIONS,
            )

    def _forward(self, x, padding_mask=None, attn_mask=None, return_hiddens=False):
        """
        :param x: [B, T, C]
        :param padding_mask: [B, T]
        :return: [B, T, C] or [L, B, T, C]
        """
        padding_mask = x.abs().sum(-1).eq(0).detach() if padding_mask is None else padding_mask
        nonpadding_mask_TB = 1 - padding_mask.transpose(0, 1).float()[:, :, None]  # [T, B, 1]
        if self.use_pos_embed:
            positions = self.pos_embed_alpha * self.embed_positions(x[..., 0])
            x = x + positions
            x = F.dropout(x, p=self.dropout, training=self.training)
        # B x T x C -> T x B x C
        x = x.transpose(0, 1) * nonpadding_mask_TB
        hiddens = []
        for layer in self.layers:
            x = layer(x, encoder_padding_mask=padding_mask, attn_mask=attn_mask) * nonpadding_mask_TB
            hiddens.append(x)
        if self.use_last_norm:
            x = self.layer_norm(x) * nonpadding_mask_TB
        if return_hiddens:
            x = torch.stack(hiddens, 0)  # [L, T, B, C]
            x = x.transpose(1, 2)  # [L, B, T, C]
        else:
            x = x.transpose(0, 1)  # [B, T, C]
        return x
