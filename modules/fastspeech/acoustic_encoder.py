import torch
import torch.nn as nn
from torch.nn import functional as F

from modules.commons.common_layers import (
    NormalInitEmbedding as Embedding,
    XavierUniformInitLinear as Linear,
)
from modules.fastspeech.tts_modules import FastSpeech2Encoder, mel2ph_to_dur
from utils.hparams import hparams
from utils.pitch_utils import f0_to_coarse
from utils.text_encoder import PAD_INDEX


class FastSpeech2AcousticEncoder(FastSpeech2Encoder):
    def forward_embedding(self, txt_tokens, dur_embed):
        # embed tokens and positions
        x = self.embed_scale * self.embed_tokens(txt_tokens)
        x = x + dur_embed
        if hparams['use_pos_embed']:
            if hparams['rel_pos']:
                x = self.embed_positions(x)
            else:
                positions = self.embed_positions(txt_tokens)
                x = x + positions
        x = F.dropout(x, p=self.dropout, training=self.training)
        return x

    def forward(self, txt_tokens, dur_embed):
        """
        :param txt_tokens: [B, T]
        :param dur_embed: [B, T, H]
        :return: {
            'encoder_out': [T x B x H]
        }
        """
        encoder_padding_mask = txt_tokens.eq(self.padding_idx).detach()
        x = self.forward_embedding(txt_tokens, dur_embed)  # [B, T, H]
        x = super()._forward(x, encoder_padding_mask)
        return x


class FastSpeech2Acoustic(nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.txt_embed = Embedding(vocab_size, hparams['hidden_size'], PAD_INDEX)
        self.dur_embed = Linear(1, hparams['hidden_size'])
        self.encoder = FastSpeech2AcousticEncoder(
            self.txt_embed, hidden_size=hparams['hidden_size'], num_layers=hparams['enc_layers'],
            ffn_kernel_size=hparams['enc_ffn_kernel_size'], num_heads=hparams['num_heads']
        )

        self.f0_embed_type = hparams.get('f0_embed_type', 'discrete')
        if self.f0_embed_type == 'discrete':
            self.pitch_embed = Embedding(300, hparams['hidden_size'], PAD_INDEX)
        elif self.f0_embed_type == 'continuous':
            self.pitch_embed = Linear(1, hparams['hidden_size'])
        else:
            raise ValueError('f0_embed_type must be \'discrete\' or \'continuous\'.')

        if hparams.get('use_key_shift_embed', False):
            self.key_shift_embed = Linear(1, hparams['hidden_size'])

        if hparams.get('use_speed_embed', False):
            self.speed_embed = Linear(1, hparams['hidden_size'])

        if hparams['use_spk_id']:
            self.spk_embed = Embedding(hparams['num_spk'], hparams['hidden_size'])

    def forward(self, txt_tokens, mel2ph, f0, key_shift=None, speed=None, spk_embed_id=None, **kwargs):
        dur = mel2ph_to_dur(mel2ph, txt_tokens.shape[1]).float()
        dur_embed = self.dur_embed(dur[:, :, None])
        encoder_out = self.encoder(txt_tokens, dur_embed)

        encoder_out = F.pad(encoder_out, [0, 0, 1, 0])
        mel2ph_ = mel2ph[..., None].repeat([1, 1, encoder_out.shape[-1]])
        condition = torch.gather(encoder_out, 1, mel2ph_)
        return self.forward_variance_embedding(
            condition, f0=f0, key_shift=key_shift, speed=speed, spk_embed_id=spk_embed_id, **kwargs
        )

    def forward_variance_embedding(self, condition, f0, key_shift=None, speed=None, spk_embed_id=None, **kwargs):
        if self.f0_embed_type == 'discrete':
            pitch = f0_to_coarse(f0)
            pitch_embed = self.pitch_embed(pitch)
        else:
            f0_mel = (1 + f0 / 700).log()
            pitch_embed = self.pitch_embed(f0_mel[:, :, None])
        condition += pitch_embed

        if hparams.get('use_key_shift_embed', False):
            key_shift_embed = self.key_shift_embed(key_shift[:, :, None])
            condition += key_shift_embed

        if hparams.get('use_speed_embed', False):
            speed_embed = self.speed_embed(speed[:, :, None])
            condition += speed_embed

        if hparams['use_spk_id']:
            spk_mix_embed = kwargs.get('spk_mix_embed')
            if spk_mix_embed is not None:
                spk_embed = spk_mix_embed
            else:
                spk_embed = self.spk_embed(spk_embed_id)[:, None, :]
            condition += spk_embed

        return condition
