#!/usr/bin/env python3
import argparse
import concurrent.futures
import os
import time

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.mlab import detrend
from matplotlib.mlab import detrend_none
from matplotlib.mlab import stride_windows
from matplotlib.mlab import window_hanning
from scipy.fft import fft  # pylint disable=no-name-in-module
from scipy.fft import fftfreq  # pylint disable=no-name-in-module

from gamutrf.utils import get_nondot_files
from gamutrf.utils import is_fft
from gamutrf.utils import parse_filename
from gamutrf.utils import replace_ext
from gamutrf.sample_reader import read_recording


def spectral_helper(x, NFFT=None, Fs=None, detrend_func=None,
                    window=None, noverlap=None, pad_to=None,
                    scale_by_freq=None, mode=None, skip_fft=False):
    if Fs is None:
        Fs = 2
    if noverlap is None:
        noverlap = 0
    if detrend_func is None:
        detrend_func = detrend_none
    if window is None:
        window = window_hanning

    window = np.abs(window(np.ones(NFFT, np.complex64)))
    if len(window) != NFFT:
        raise ValueError(
            "The window length must match the data's first dimension")

    # if NFFT is set to None use the whole signal
    if NFFT is None:
        NFFT = 256

    if mode is None or mode == 'default':
        mode = 'psd'

    if pad_to is None:
        pad_to = NFFT

    if mode != 'psd':
        scale_by_freq = False
    elif scale_by_freq is None:
        scale_by_freq = True

    # For real x, ignore the negative frequencies unless told otherwise
    numFreqs = pad_to
    if pad_to % 2:
        freqcenter = (pad_to - 1)//2 + 1
    else:
        freqcenter = pad_to//2

    results = []

    for i in x:
        if skip_fft:
            # TODO: assume NFFT is factor of sps.
            result = np.reshape(i, (NFFT, int(len(i) / NFFT)), order='F')
        else:
            result = stride_windows(i, NFFT, noverlap, axis=0)
            result = detrend(result, detrend_func, axis=0)
            result = fft(result, n=pad_to, axis=0)[
                :numFreqs, :]  # pylint: disable=invalid-sequence-index

        if mode == 'psd':
            result *= np.conj(result)
            # MATLAB divides by the sampling frequency so that density function
            # has units of dB/Hz and can be integrated by the plotted frequency
            # values. Perform the same scaling here.
            if scale_by_freq:
                result /= Fs
                # Scale the spectrum by the norm of the window to compensate for
                # windowing loss; see Bendat & Piersol Sec 11.5.2.
                result /= (window**2).sum()
            else:
                # In this case, preserve power in the segment, not amplitude
                result /= window.sum()**2
        elif mode == 'magnitude':
            result = np.abs(result) / window.sum()
        elif mode == 'complex':
            result /= window.sum()
        elif mode in ('angle', 'phase'):
            # we unwrap the phase later to handle the onesided vs. twosided case
            result = np.angle(result)
            if mode == 'phase':
                # we unwrap the phase here to handle the onesided vs. twosided case
                result = np.unwrap(result, axis=0)
        result = np.apply_along_axis(np.real, 1, result)
        results.append(result)

    if results:
        lastresult = np.hstack(results)
        t_x = lastresult.shape[0] * lastresult.shape[1]
        t = np.arange(NFFT/2, t_x - NFFT/2 + 1, NFFT - noverlap)/Fs
        freqs = fftfreq(pad_to, 1/Fs)[:numFreqs]
        # center the frequency range at zero
        freqs = np.roll(freqs, -freqcenter, axis=0)
        lastresult = np.roll(lastresult, -freqcenter, axis=0)
        return lastresult, freqs, t
    return (None, None, None)


def specgram(x, NFFT=None, Fs=None, Fc=None, detrend=None,
             window=None, noverlap=None,
             xextent=None, pad_to=None,
             scale_by_freq=None, mode=None, scale=None,
             skip_fft=False):
    if NFFT is None:
        NFFT = 256  # same default as in mlab.specgram()
    if Fc is None:
        Fc = 0  # same default as in mlab._spectral_helper()
    if noverlap is None:
        noverlap = 128  # same default as in mlab.specgram()
    if Fs is None:
        Fs = 2  # same default as in mlab._spectral_helper()

    if mode == 'complex':
        raise ValueError('Cannot plot a complex specgram')

    if scale is None or scale == 'default':
        if mode in ['angle', 'phase']:
            scale = 'linear'
        else:
            scale = 'dB'
    elif mode in ['angle', 'phase'] and scale == 'dB':
        raise ValueError('Cannot use dB scale with angle or phase mode')

    spec, freqs, t = spectral_helper(x=x, NFFT=NFFT, Fs=Fs,
                                     detrend_func=detrend, window=window,
                                     noverlap=noverlap, pad_to=pad_to,
                                     scale_by_freq=scale_by_freq,
                                     mode=mode, skip_fft=skip_fft)
    if scale == 'linear':
        Z = spec
    elif scale == 'dB':
        if mode is None or mode == 'default' or mode == 'psd':
            Z = 10. * np.log10(spec)
        else:
            Z = 20. * np.log10(spec)
    else:
        raise ValueError(f'Unknown scale {scale!r}')

    Z = np.flipud(Z)

    if xextent is None:
        # padding is needed for first and last segment:
        pad_xextent = (NFFT-noverlap) / Fs / 2
        xextent = np.min(t) - pad_xextent, np.max(t) + pad_xextent
    xmin, xmax = xextent
    freqs += Fc
    freqs /= 1e6
    extent = xmin, xmax, freqs[0], freqs[-1]

    return Z, extent


def plot_spectrogram(x, spectrogram_filename, nfft, fs, fc, cmap, ytics, bare, noverlap, skip_fft, width, height, dpi):
    fig = plt.figure()
    fig.set_size_inches(width, height)
    axes = fig.add_subplot(111)
    axes.set_xlabel('time (s)')
    axes.set_ylabel('freq (MHz)')
    Z, extent = specgram(
        x, NFFT=nfft, Fs=fs, Fc=fc, noverlap=noverlap, skip_fft=skip_fft)
    im = axes.imshow(Z, cmap=cmap, extent=extent, origin='upper')
    axes.axis('auto')
    axes.minorticks_on()
    plt.locator_params(axis='y', nbins=ytics)

    if bare:
        axes.set_axis_off()
        plt.subplots_adjust(top=1, bottom=0, right=1,
                            left=0, hspace=0, wspace=0)
        plt.margins(0, 0)
        axes.xaxis.set_major_locator(plt.NullLocator())
        axes.yaxis.set_major_locator(plt.NullLocator())

    plt.sci(im)
    plt.savefig(spectrogram_filename, dpi=dpi)
    # must call this in specific order to avoid pyplot leak
    axes.images.remove(im)
    fig.clear()
    plt.close()
    plt.cla()
    plt.clf()


def process_recording(args, recording):
    freq_center, sample_rate, sample_dtype, sample_len, _sample_type, _sample_bits = parse_filename(
        recording)
    spectrogram_filename = replace_ext(recording, args.iext, all_ext=True)
    if args.skip_exist and os.path.exists(spectrogram_filename):
        print(f'skipping {recording}')
        return
    if is_fft(recording):
        if not args.skip_fft:
            print(f'skipping precomputed FFT {recording}')
            return
    else:
        if args.skip_fft:
            print(f'{recording} not a precomputed FFT, skipping')
            return
    print(f'processing {recording}')
    samples = read_recording(recording, sample_rate, sample_dtype, sample_len)
    plot_spectrogram(
        samples,
        spectrogram_filename,
        args.nfft,
        sample_rate,
        freq_center,
        args.cmap,
        args.ytics,
        args.bare,
        args.noverlap,
        args.skip_fft,
        args.width,
        args.height,
        args.dpi)


def process_all_recordings(args):
    if os.path.isdir(args.recording):
        recordings = get_nondot_files(args.recording)
    else:
        recordings = [args.recording]
    if args.workers == 1:
        for recording in sorted(recordings):
            process_recording(args, recording)
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            for recording in sorted(recordings):
                executor.submit(process_recording, args, recording)
            executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(
        description='draw spectrogram from recording')
    parser.add_argument('recording', default='', type=str,
                        help='filename of recording, or directory')
    parser.add_argument('--nfft', default=int(2048), type=int,
                        help='number of FFT points')
    parser.add_argument('--ytics', default=20, type=int,
                        help='number of y tics')
    # https://ai.googleblog.com/2019/08/turbo-improved-rainbow-colormap-for.html (turbo scheme suggested for ML).
    # yellow signal > blue signal.
    parser.add_argument('--cmap', default='turbo_r', type=str,
                        help='pyplot colormap (see https://matplotlib.org/stable/tutorials/colors/colormaps.html)')
    parser.add_argument('--bare', dest='bare', action='store_true')
    parser.add_argument('--no-bare', dest='bare', action='store_false')
    parser.add_argument('--iext', dest='iext', default='png', type=str,
                        help='extension (image type) to use for spectrogram')
    parser.add_argument('--noverlap', dest='noverlap', default=0, type=int,
                        help='number of overlapping FFT windows')
    parser.add_argument('--workers', dest='workers', default=1, type=int,
                        help='number of parallel workers')
    parser.add_argument('--skip-exist', dest='skip_exist', action='store_true',
                        help='skip existing images')
    parser.add_argument('--no-skip-exist', dest='skip_exist', action='store_false',
                        help='overwrite existing images')
    parser.add_argument('--skip-fft', dest='skip_fft', action='store_true',
                        help='skip FFT')
    parser.add_argument('--no-skip-fft', dest='skip_fft', action='store_false',
                        help='calculate FFT')
    parser.add_argument('--loop', dest='loop', default=0, type=int,
                        help='if > 0, run in a loop')
    parser.add_argument('--width', default=11, type=int,
                        help='plot width')
    parser.add_argument('--height', default=8, type=int,
                        help='plot height')
    parser.add_argument('--dpi', default=100, type=int,
                        help='plot DPI')
    parser.set_defaults(bare=False, skip_exist=False, skip_fft=False)
    args = parser.parse_args()
    while True:
        process_all_recordings(args)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == '__main__':
    main()
