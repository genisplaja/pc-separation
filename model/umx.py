# Based on https://github.com/sigsep/open-unmix-pytorch/blob/master/openunmix/model.py
import numpy as np
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch.nn import LSTM, BatchNorm1d, Linear, Parameter
from typing import Optional, Mapping

from . import Separator
from dsp.filtering import wiener
from dsp.transforms import make_filterbanks, ComplexNorm


class OpenUnmix(nn.Module):
    """OpenUnmix Core spectrogram based separation module.
    Args:
        nb_bins (int): Number of input time-frequency bins (Default: `4096`).
        nb_channels (int): Number of input audio channels (Default: `2`).
        hidden_size (int): Size for bottleneck layers (Default: `512`).
        nb_layers (int): Number of Bi-LSTM layers (Default: `3`).
        unidirectional (bool): Use causal model useful for realtime purpose.
            (Default `False`)
        input_mean (ndarray or None): global data mean of shape `(nb_bins, )`.
            Defaults to zeros(nb_bins)
        input_scale (ndarray or None): global data mean of shape `(nb_bins, )`.
            Defaults to ones(nb_bins)
        max_bin (int or None): Internal frequency bin threshold to
            reduce high frequency content. Defaults to `None` which results
            in `nb_bins`
    """

    def __init__(
        self,
        nb_bins: int = 4096,
        nb_channels: int = 2,
        hidden_size: int = 512,
        nb_layers: int = 3,
        unidirectional: bool = False,
        input_mean: Optional[np.ndarray] = None,
        input_scale: Optional[np.ndarray] = None,
        max_bin: Optional[int] = None,
    ):
        super(OpenUnmix, self).__init__()

        self.nb_output_bins = nb_bins
        if max_bin:
            self.nb_bins = max_bin
        else:
            self.nb_bins = self.nb_output_bins

        self.hidden_size = hidden_size

        self.fc1 = Linear(self.nb_bins * nb_channels, hidden_size, bias=False)

        self.bn1 = BatchNorm1d(hidden_size)

        if unidirectional:
            lstm_hidden_size = hidden_size
        else:
            lstm_hidden_size = hidden_size // 2

        self.lstm = LSTM(
            input_size=hidden_size,
            hidden_size=lstm_hidden_size,
            num_layers=nb_layers,
            bidirectional=not unidirectional,
            batch_first=False,
            dropout=0.4 if nb_layers > 1 else 0,
        )

        fc2_hiddensize = hidden_size * 2
        self.fc2 = Linear(in_features=fc2_hiddensize, out_features=hidden_size, bias=False)

        self.bn2 = BatchNorm1d(hidden_size)

        self.fc3 = Linear(
            in_features=hidden_size,
            out_features=self.nb_output_bins * nb_channels,
            bias=False,
        )

        self.bn3 = BatchNorm1d(self.nb_output_bins * nb_channels)

        if input_mean is not None:
            input_mean = torch.from_numpy(-input_mean[: self.nb_bins]).float()
        else:
            input_mean = torch.zeros(self.nb_bins)

        if input_scale is not None:
            input_scale = torch.from_numpy(1.0 / input_scale[: self.nb_bins]).float()
        else:
            input_scale = torch.ones(self.nb_bins)

        self.input_mean = Parameter(input_mean)
        self.input_scale = Parameter(input_scale)

        self.output_scale = Parameter(torch.ones(self.nb_output_bins).float())
        self.output_mean = Parameter(torch.ones(self.nb_output_bins).float())

    def freeze(self):
        # set all parameters as not requiring gradient, more RAM-efficient
        # at test time
        for p in self.parameters():
            p.requires_grad = False
        self.eval()

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: input spectrogram of shape
                `(nb_samples, nb_channels, nb_bins, nb_frames)`
        Returns:
            Tensor: filtered spectrogram of shape
                `(nb_samples, nb_channels, nb_bins, nb_frames)`
        """

        # permute so that batch is last for lstm
        x = x.permute(3, 0, 1, 2)
        # get current spectrogram shape
        nb_frames, nb_samples, nb_channels, nb_bins = x.data.shape

        mix = x.detach().clone()

        # crop
        x = x[..., : self.nb_bins]
        # shift and scale input to mean=0 std=1 (across all bins)
        x = x + self.input_mean
        x = x * self.input_scale

        # to (nb_frames*nb_samples, nb_channels*nb_bins)
        # and encode to (nb_frames*nb_samples, hidden_size)
        x = self.fc1(x.reshape(-1, nb_channels * self.nb_bins))
        # normalize every instance in a batch
        x = self.bn1(x)
        x = x.reshape(nb_frames, nb_samples, self.hidden_size)
        # squash range ot [-1, 1]
        x = torch.tanh(x)

        # apply 3-layers of stacked LSTM
        lstm_out = self.lstm(x)

        # lstm skip connection
        x = torch.cat([x, lstm_out[0]], -1)

        # first dense stage + batch norm
        x = self.fc2(x.reshape(-1, x.shape[-1]))
        x = self.bn2(x)

        x = F.relu(x)

        # second dense stage + layer norm
        x = self.fc3(x)
        x = self.bn3(x)

        # reshape back to original dim
        x = x.reshape(nb_frames, nb_samples, nb_channels, self.nb_output_bins)

        # apply output scaling
        x *= self.output_scale
        x += self.output_mean

        # since our output is non-negative, we can apply RELU
        x = F.relu(x) * mix
        # permute back to (nb_samples, nb_channels, nb_bins, nb_frames)
        return x.permute(1, 2, 3, 0)


class UMXSeparator(Separator):
    def __init__(self,
                 device,
                 model_cfg,
                 residual=True,
                 wiener_win_len=300,
                 num_iter=0,
                 softmask=False):
        super(UMXSeparator, self).__init__(device, model_cfg)
        self._residual = residual
        self._window_size = self._model_cfg.stft.window_size
        self._hop_size = self._model_cfg.stft.hop_size
        self._max_freq_bins = bandwidth_to_max_bin(rate=self._sample_rate,
                                                   n_fft=self._window_size,
                                                   bandwidth=model_cfg.model.bandwidth)
        self._wiener_win_len = wiener_win_len
        self._num_iter = num_iter
        self._softmask = softmask

        self._stft, self._istft = make_filterbanks(
            n_fft=self._window_size,
            n_hop=self._hop_size,
            center=self._model_cfg.stft.center,
            device=self._device
        )
        self._complexnorm = ComplexNorm(mono=self._model_cfg.audio.mono)
        self._target_models = dict()

    def load_model(self,
                   targets=None,
                   model_dir=None):

        target_models = {}

        if targets is None:
            targets = ['piano', 'orch']

        for target in targets:
            # load open unmix model
            target_unmix = OpenUnmix(
                nb_bins=self._window_size // 2 + 1,
                nb_channels=self._num_channels,
                hidden_size=self._model_cfg.train.hidden_size,
                max_bin=self._max_freq_bins
            )

            state_dict = torch.load(os.path.join(model_dir, f'{target}_best.pth'),
                                    map_location=self._device)
            target_unmix.load_state_dict(state_dict, strict=False)
            target_unmix.eval()
            target_unmix.to(self._device)
            target_models[target] = target_unmix

        self._target_models = target_models

    def forward(self,
                mix: Tensor = None) -> Tensor:
        """Performing the separation on mix input
        Args:
            mix (Tensor): [shape=(nb_samples, nb_channels, nb_timesteps)]
                mixture audio waveform
        Returns:
            Tensor: stacked tensor of separated waveforms
                shape `(nb_samples, nb_targets, nb_channels, nb_timesteps)`
        """
        mix = mix.to(self._device)
        nb_sources = len(self._target_models)
        nb_samples = mix.shape[0]

        # getting the STFT of mix:
        # (nb_samples, nb_channels, nb_bins, nb_frames, 2)
        mix_stft = self._stft(mix)
        X = self._complexnorm(mix_stft)

        # initializing spectrograms variable
        spectrograms = torch.zeros(X.shape + (nb_sources,),
                                   dtype=mix.dtype,
                                   device=self._device)

        for j, target_name in enumerate(self._target_models):
            target_module = self._target_models[target_name]
            # apply current model to get the source spectrogram
            target_spectrogram = target_module(X.detach().clone())
            spectrograms[..., j] = target_spectrogram

        # transposing it as
        # (nb_samples, nb_frames, nb_bins,{1,nb_channels}, nb_sources)
        spectrograms = spectrograms.permute(0, 3, 2, 1, 4)

        # rearranging it into:
        # (nb_samples, nb_frames, nb_bins, nb_channels, 2) to feed
        # into filtering methods
        mix_stft = mix_stft.permute(0, 3, 2, 1, 4)

        # create an additional target if we need to build a residual
        if self._residual:
            # we add an additional target
            nb_sources += 1

        if nb_sources == 1 and self._num_iter > 0:
            raise Exception(
                "Cannot use EM if only one target is estimated."
                "Provide two targets or create an additional "
                "one with `--residual`"
            )

        nb_frames = spectrograms.shape[1]
        targets_stft = torch.zeros(
            mix_stft.shape + (nb_sources,),
            dtype=mix.dtype,
            device=mix_stft.device
        )

        for sample in range(nb_samples):
            pos = 0
            if self._wiener_win_len is not None:
                wiener_win_len = self._wiener_win_len
            else:
                wiener_win_len = nb_frames
            while pos < nb_frames:
                cur_frame = torch.arange(pos, min(nb_frames, pos + wiener_win_len))
                pos = int(cur_frame[-1]) + 1

                targets_stft[sample, cur_frame] = wiener(
                    spectrograms[sample, cur_frame],
                    mix_stft[sample, cur_frame],
                    self._num_iter,
                    softmask=self._softmask,
                    residual=self._residual,
                )

        # getting to (nb_samples, nb_targets, channel, fft_size, n_frames, 2)
        targets_stft = targets_stft.permute(0, 5, 3, 2, 1, 4).contiguous()

        # inverse STFT
        estimates = self._istft(targets_stft, length=mix.shape[2])

        return estimates, targets_stft

    def to_dict(self,
                estimates: Tensor,
                aggregate_dict: Optional[dict] = None) -> dict:
        """Convert estimates as stacked tensor to dictionary
        Args:
            estimates (Tensor): separated targets of shape
                (nb_samples, nb_targets, nb_channels, nb_timesteps)
            aggregate_dict (dict or None)
        Returns:
            (dict of str: Tensor):
        """
        estimates_dict = {}
        for k, target in enumerate(self._target_models):
            estimates_dict[target] = estimates[k, :, ...]

        # in the case of residual, we added another source
        if self._residual:
            estimates_dict["residual"] = estimates[-1, :, ...]

        if aggregate_dict is not None:
            new_estimates = {}
            for key in aggregate_dict:
                new_estimates[key] = torch.tensor(0.0)
                for target in aggregate_dict[key]:
                    new_estimates[key] = new_estimates[key] + estimates_dict[target]
            estimates_dict = new_estimates
        return estimates_dict

    def separate(self,
                 mix: Tensor):
        estimates, _ = self.forward(mix)
        estimates_dict = self.to_dict(estimates.squeeze(0))
        return estimates_dict
def bandwidth_to_max_bin(rate: float,
                         n_fft: int,
                         bandwidth: float) -> np.ndarray:
    """Convert bandwidth to maximum bin count
    Assuming lapped transforms such as STFT
    Parameters
    ----------
        rate (int): Sample rate
        n_fft (int): FFT length
        bandwidth (float): Target bandwidth in Hz

    Returns
    -------
        np.ndarray: maximum frequency bin
    """
    freqs = np.linspace(0, rate / 2, n_fft // 2 + 1, endpoint=True)

    return np.max(np.where(freqs <= bandwidth)[0]) + 1