import math

import torch
from torch import nn, einsum
import torch.nn.functional as F

from einops import rearrange

from vector_quantize_pytorch import ResidualVQ

# helper functions

def exists(val):
    return val is not None

# gan losses

def hinge_discr_loss(fake, real):
    return (F.relu(1 + fake) + F.relu(1 - real)).mean()

def hinge_gen_loss(fake):
    return -fake.mean()

def leaky_relu(p = 0.1):
    return nn.LeakyReLU(0.1)

# gans

class MultiScaleDiscriminator(nn.Module):
    def __init__(
        self,
        channels = 16,
        layers = 4,
        groups = 4,
        chan_max = 1024,
        input_channels = 1
    ):
        super().__init__()
        self.init_conv = nn.Conv1d(input_channels, channels, 7)
        self.conv_layers = nn.ModuleList([])

        curr_channels = channels

        for _ in range(layers):
            chan_out = min(curr_channels * 4, chan_max)

            self.conv_layers.append(nn.Sequential(
                nn.Conv1d(curr_channels, chan_out, 8, stride = 4, padding = 4),
                leaky_relu()
            ))

            curr_channels = chan_out

        self.final_conv = nn.Sequential(
            nn.Conv1d(curr_channels, curr_channels, 3),
            leaky_relu(),
            nn.Conv1d(curr_channels, 1, 1),
        )

    def forward(self, x):
        x = self.init_conv(x)

        for layer in self.conv_layers:
            x = layer(x)

        return self.final_conv(x)

# sound stream

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x

class CausalConv1d(nn.Module):
    def __init__(self, chan_in, chan_out, kernel_size, **kwargs):
        super().__init__()
        kernel_size = kernel_size
        dilation = kwargs.get('dilation', 1)
        self.causal_padding = dilation * (kernel_size - 1)

        self.conv = nn.Conv1d(chan_in, chan_out, kernel_size, **kwargs)

    def forward(self, x):
        x = F.pad(x, (self.causal_padding, 0))
        return self.conv(x)

def ResidualUnit(chan_in, chan_out, dilation, kernel_size = 7):
    return Residual(nn.Sequential(
        CausalConv1d(chan_in, chan_out, kernel_size, dilation = dilation),
        nn.ELU(),
        CausalConv1d(chan_out, chan_out, 1),
        nn.ELU()
    ))

def EncoderBlock(chan_in, chan_out, stride):
    return nn.Sequential(
        ResidualUnit(chan_in, chan_in, 1),
        ResidualUnit(chan_in, chan_in, 3),
        ResidualUnit(chan_in, chan_in, 9),
        CausalConv1d(chan_in, chan_out, 2 * stride, stride = stride)
    )

def DecoderBlock(chan_in, chan_out, stride):
    even_stride = (stride % 2 == 0)
    padding = (stride + (0 if even_stride else 1)) // 2
    output_padding = 0 if even_stride else 1

    return nn.Sequential(
        nn.ConvTranspose1d(chan_in, chan_out, 2 * stride, stride = stride, padding = padding, output_padding = output_padding),
        ResidualUnit(chan_out, chan_out, 1),
        ResidualUnit(chan_out, chan_out, 3),
        ResidualUnit(chan_out, chan_out, 9),
    )

class SoundStream(nn.Module):
    def __init__(
        self,
        *,
        channels = 32,
        strides = (2, 4, 5, 8),
        channel_mults = (2, 4, 8, 16),
        codebook_dim = 512,
        codebook_size = 1024,
        rq_num_quantizers = 8,
        input_channels = 1,
        discr_multi_scales = (1, 0.5, 0.25),
        recon_loss_weight = 1.,
        adversarial_loss_weight = 1.
    ):
        super().__init__()

        layer_channels = tuple(map(lambda t: t * channels, channel_mults))
        layer_channels = (channels, *layer_channels)
        chan_in_out_pairs = tuple(zip(layer_channels[:-1], layer_channels[1:]))

        encoder_blocks = []

        for ((chan_in, chan_out), layer_stride) in zip(chan_in_out_pairs, strides):
            encoder_blocks.append(EncoderBlock(chan_in, chan_out, layer_stride))

        self.encoder = nn.Sequential(
            CausalConv1d(input_channels, channels, 7),
            *encoder_blocks,
            CausalConv1d(layer_channels[-1], codebook_dim, 3)
        )

        self.rq = ResidualVQ(
            dim = codebook_dim,
            num_quantizers = rq_num_quantizers,
            codebook_size = codebook_size,
            kmeans_init = True,
            threshold_ema_dead_code = 2,
            sync_kmeans = False
        )

        decoder_blocks = []

        for ((chan_in, chan_out), layer_stride) in zip(reversed(chan_in_out_pairs), reversed(strides)):
            decoder_blocks.append(DecoderBlock(chan_out, chan_in, layer_stride))

        self.decoder = nn.Sequential(
            CausalConv1d(codebook_dim, layer_channels[-1], 7),
            *decoder_blocks,
            CausalConv1d(channels, input_channels, 7)
        )

        # discriminators

        self.discr_multi_scales = discr_multi_scales
        self.discriminators = nn.ModuleList([MultiScaleDiscriminator() for _ in range(len(discr_multi_scales))])

        # loss weights

        self.recon_loss_weight = recon_loss_weight
        self.adversarial_loss_weight = adversarial_loss_weight

    def forward(
        self,
        x,
        return_discr_loss = False
    ):
        if x.ndim == 2:
            x = rearrange(x, 'b n -> b 1 n')

        orig_x = x.clone()

        x = self.encoder(x)

        x = rearrange(x, 'b c n -> b n c')
        x, indices, commit_loss = self.rq(x)
        x = rearrange(x, 'b n c -> b c n')

        recon_x = self.decoder(x)

        # multi-scale discriminator loss

        if return_discr_loss:
            real, fake = orig_x, recon_x.detach()
            discr_losses = []

            for discr, scale in zip(self.discriminators, self.discr_multi_scales):
                scaled_real, scaled_fake = map(lambda t: F.interpolate(t, scale_factor = scale), (real, fake))

                real_logits, fake_logits = map(discr, (scaled_real, scaled_fake))
                one_discr_loss = hinge_discr_loss(fake_logits, real_logits)
                discr_losses.append(one_discr_loss)

            return torch.stack(discr_losses).mean()

        # recon loss

        recon_loss = F.mse_loss(orig_x, recon_x)

        # generator loss

        adversarial_losses = []

        for discr, scale in zip(self.discriminators, self.discr_multi_scales):
            scaled_fake = F.interpolate(recon_x, scale_factor = scale)
            fake_logits = discr(scaled_fake)
            one_adversarial_loss = hinge_gen_loss(fake_logits)
            adversarial_losses.append(one_adversarial_loss)

        adversarial_loss = torch.stack(adversarial_losses).mean()

        return recon_loss * self.recon_loss_weight + adversarial_loss * self.adversarial_loss_weight

# relative positional bias

class RelativePositionBias(nn.Module):
    def __init__(
        self,
        num_buckets = 32,
        max_distance = 128,
        heads = 8
    ):
        super().__init__()
        self.num_buckets = num_buckets
        self.max_distance = max_distance
        self.relative_attention_bias = nn.Embedding(num_buckets, heads)

    @staticmethod
    def _relative_position_bucket(relative_position, causal = True, num_buckets = 32, max_distance = 128):
        ret = 0

        n = -relative_position
        n = torch.max(n, torch.zeros_like(n))

        max_exact = num_buckets // 2
        is_small = n < max_exact

        val_if_large = max_exact + (
            torch.log(n.float() / max_exact) / math.log(max_distance / max_exact) * (num_buckets - max_exact)
        ).long()

        val_if_large = torch.min(val_if_large, torch.full_like(val_if_large, num_buckets - 1))

        ret += torch.where(is_small, n, val_if_large)
        return ret

    def forward(self, i, j, device):

        q_pos = torch.arange(j - i, j, dtype = torch.long, device = device)
        k_pos = torch.arange(j, dtype = torch.long, device = device)

        rel_pos = k_pos[None, :] - q_pos[:, None]

        rp_bucket = self._relative_position_bucket(rel_pos, num_buckets = self.num_buckets, max_distance = self.max_distance)
        values = self.relative_attention_bias(rp_bucket)

        return rearrange(values, 'i j h -> h i j')

# feedforward

def FeedForward(dim, mult = 4):
    inner_dim = int(dim * mult)
    return nn.Sequential(
        nn.LayerNorm(dim),
        nn.Linear(dim, inner_dim, bias = False),
        nn.GELU(),
        nn.Linear(inner_dim, dim, bias = False)
    )

# attention

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        inner_dim = dim_head * heads

        self.norm = nn.LayerNorm(dim)

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim, bias = False)

    def forward(self, x, attn_bias = None):
        x = self.norm(x)

        q, k, v = self.to_q(x), *self.to_kv(x).chunk(2, dim = -1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), (q, k, v))

        q = q * self.scale

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        if exists(attn_bias):
            sim = sim + attn_bias

        i, j = sim.shape[-2:]
        causal_mask = torch.ones((i, j), dtype = torch.bool, device = x.device).triu(j - i + 1)
        sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)

        attn = sim.softmax(dim = -1)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# transformer

class Transformer(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth,
        **kwargs
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        self.rel_pos_bias = RelativePositionBias()

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(dim = dim, **kwargs),
                FeedForward(dim = dim)
            ]))

        self.norm = nn.LayerNorm(dim)

    def forward(self, x):
        n, device = x.shape[1], x.device

        rel_pos_bias = self.rel_pos_bias(n, n, device = device)

        for attn, ff in self.layers:
            x = attn(x, attn_bias = rel_pos_bias) + x
            x = ff(x) + x

        return self.norm(x)

# audio LM

class AudioLM(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth,
        **kwargs
    ):
        super().__init__()
        self.attend_semantic = Transformer(dim = dim, depth = depth, **kwargs)
        self.attend_coarse = Transformer(dim = dim, depth = depth, **kwargs)
        self.attend_fine = Transformer(dim = dim, depth = depth, **kwargs)

    def forward(self, x):
        x = self.attend_semantic(x)
        return x
