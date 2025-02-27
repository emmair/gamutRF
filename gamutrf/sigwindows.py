#!/usr/bin/python3
import logging
import random
from collections import Counter
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.signal import find_peaks


ROLLOVERHZ = 100e6
CSV = '.csv'
CSVCOLS = ['ts', 'freq', 'db']


def read_csv_chunks(args):
    minmhz = args.minhz / 1e6
    leftover_df = pd.DataFrame()

    def detect_frames(df):
        freqdiff = df['freq'].diff().abs()
        df['frame'] = 0
        # Detect tuning wraparound, where frequency changed by more than 100MHz
        df.loc[freqdiff > (ROLLOVERHZ / 1e6), ['frame']] = 1
        df['frame'] = (   # pylint: disable=unsupported-assignment-operation,disable=unsubscriptable-object
            df['frame'].cumsum().fillna(0).astype(np.uint64))  # pylint: disable=unsubscriptable-object

    def preprocess_frames(df):
        df.set_index('frame', inplace=True)
        df['ts'] = df.groupby('frame', sort=False)['ts'].transform(min)

    with pd.read_csv(args.csv, header=None, delim_whitespace=True, chunksize=args.nrows) as reader:
        for chunk in reader:
            read_rows = len(chunk)
            logging.info(f'read chunk of {read_rows} from {args.csv}')
            chunk.columns = CSVCOLS
            chunk['freq'] /= 1e6
            df = pd.concat([leftover_df, chunk])
            detect_frames(df)
            read_frames = df['frame'].max(
            )  # pylint: disable=unsubscriptable-object
            if read_frames == 0 and len(chunk) == args.nrows:
                leftover_df = leftover_df.append(chunk[CSVCOLS])
                logging.info(
                    f'buffering incomplete frame - {args.nrows} too small?')
                continue
            leftover_df = df[df['frame'] ==
                             read_frames][CSVCOLS]  # pylint: disable=unsubscriptable-object
            df = df[(df['frame'] < read_frames) & (df['freq'] >= minmhz)
                    ]  # pylint: disable=unsubscriptable-object
            df = calc_db(df)
            df = df[df['db'] >= args.mindb]
            preprocess_frames(df)
            yield df

    if len(leftover_df):
        df = leftover_df
        detect_frames(df)
        df = df[(df['frame'] < read_frames) & (df['freq'] >= minmhz)
                ]  # pylint: disable=unsubscriptable-object
        df = calc_db(df)
        df = df[df['db'] >= args.mindb]
        preprocess_frames(df)
        yield df


def read_csv(args):
    frames = 0

    for df in read_csv_chunks(args):
        for _, frame_df in df.groupby('frame'):
            yield (frames, frame_df)
            if args.maxframe and frames == args.maxframe:
                return
            frames += 1


def choose_recorders(signals, recorder_freq_exclusions):
    suitable_recorders = defaultdict(set)
    for signal in sorted(signals):
        for recorder, excluded in sorted(recorder_freq_exclusions.items()):
            if not freq_excluded(signal, excluded):
                suitable_recorders[signal].add(recorder)
    recorder_assignments = []
    busy_recorders = set()
    for signal, recorders in sorted(suitable_recorders.items(), key=lambda x: x[1]):
        if not recorders:
            continue
        free_recorders = recorders - busy_recorders
        if not free_recorders:
            continue
        recorder = random.choice(list(free_recorders))  # nosec
        busy_recorders.add(recorder)
        recorder_assignments.append((signal, recorder))
    return recorder_assignments


def parse_freq_excluded(freq_exclusions_raw):
    freq_exclusions = []
    for pair in freq_exclusions_raw:
        freq_min, freq_max = pair.split('-')
        if len(freq_min):
            freq_min = int(freq_min)
        else:
            freq_min = None
        if len(freq_max):
            freq_max = int(freq_max)
        else:
            freq_max = None
        freq_exclusions.append((freq_min, freq_max))
    return tuple(freq_exclusions)


def freq_excluded(freq, freq_exclusions):
    for freq_min, freq_max in freq_exclusions:
        if freq_min is not None and freq_max is not None:
            if freq >= freq_min and freq <= freq_max:
                return True
            continue
        if freq_min is None:
            if freq <= freq_max:
                return True
            continue
        if freq >= freq_min:
            return True
    return False


def calc_db(df):
    df['db'] = 20 * np.log10(df[df['db'] != 0]['db'])
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df['rollingdiffdb'] = df[df['db'].notna()]['db'].rolling(5).mean().diff()
    return df


def scipy_find_sig_windows(df, width, prominence, threshold):
    data = df.db.to_numpy()
    peaks, _ = find_peaks(data, prominence=prominence, width=width)
    signals = []
    for peak in peaks:
        row = df.iloc[peak]
        aroundrows = df.iloc[peak-1:peak+1]
        arounddb = aroundrows.db.mean()
        if arounddb > threshold:
            signals.append((row.freq, arounddb))
    return signals


def find_sig_windows(df, window=4, threshold=2, min_bw_mhz=1):
    window_df = df[(df['rollingdiffdb'].rolling(
        window).sum().abs() > (window * threshold))]
    freq_diff = window_df['freq'].diff().fillna(min_bw_mhz)
    signals = []
    in_signal = None
    for row in window_df[freq_diff >= min_bw_mhz].itertuples():
        if in_signal is not None:
            start_freq = in_signal.freq
            end_freq = row.freq
            signal_df = df[(df['freq'] >= start_freq)
                           & (df['freq'] <= end_freq)]
            center_freq = start_freq + ((end_freq - start_freq) / 2)
            signals.append((center_freq, signal_df['db'].max()))
            in_signal = None
        else:
            in_signal = row
    return signals


def get_center(signal_mhz, freq_start_mhz, bin_mhz, record_bw):
    return int(int((signal_mhz - freq_start_mhz) / record_bw) * bin_mhz + freq_start_mhz)


def choose_record_signal(signals, recorders):
    recorder_buckets = Counter()

    # Convert signals into buckets of record_bw size, count how many of each size
    for bucket in signals:
        recorder_buckets[bucket] += 1

    # Now count number of buckets of each count.
    buckets_by_count = defaultdict(set)
    for bucket, count in recorder_buckets.items():
        buckets_by_count[count].add(bucket)

    recorder_freqs = []
    # From least occuring bucket to most occurring, choose a random bucket for each recorder.
    for _count, buckets in sorted(buckets_by_count.items()):
        while buckets and len(recorder_freqs) < recorders:
            bucket = random.choice(list(buckets))  # nosec
            buckets = buckets.remove(bucket)
            recorder_freqs.append(bucket)
        if len(recorder_freqs) == recorders:
            break
    return recorder_freqs
