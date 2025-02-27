#!/usr/bin/python3
import argparse
import concurrent.futures
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time

import bjoern
import falcon
import jinja2
import pandas as pd
import requests
import schedule
from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import start_http_server

from gamutrf.sigwindows import calc_db
from gamutrf.sigwindows import choose_record_signal
from gamutrf.sigwindows import choose_recorders
from gamutrf.sigwindows import get_center
from gamutrf.sigwindows import parse_freq_excluded
from gamutrf.sigwindows import scipy_find_sig_windows
from gamutrf.utils import MTU, ROLLOVERHZ

SOCKET_TIMEOUT = 1.0
SCAN_FRES = 1e4
PEAK_DBS = {}


def falcon_response(resp, text, status):
    resp.status = status
    resp.text = text
    resp.content_type = 'text/html'


def ok_response(resp, text='ok!'):
    falcon_response(resp, text=text, status=falcon.HTTP_200)


def error_response(resp, text='error!'):
    falcon_response(resp, text=text, status=falcon.HTTP_500)


def load_template(name):
    path = os.path.join('templates', name)
    with open(os.path.abspath(path), 'r') as fp:
        return jinja2.Template(fp.read())


class ActiveRequests:
    def on_get(self, req, resp):
        all_jobs = schedule.get_jobs()
        ok_response(resp, f'{all_jobs}')


class ScannerForm:
    def on_get(self, req, resp):
        template = load_template('scanner_form.html')
        ok_response(resp, template.render(bins=PEAK_DBS))


class Result:
    def on_post(self, req, resp):
        # TODO validate input
        try:
            recorder = f'http://{req.media["worker"]}:8000/'
            signal_hz = int(int(req.media['frequency']) * 1e6)
            record_bps = int(int(req.media['bandwidth']) * (1024 * 1024))
            record_samples = int(record_bps * int(req.media['duration']))
            recorder_args = f'record/{signal_hz}/{record_samples}/{record_bps}'
            timeout = int(req.media['duration'])
            response = None
            if int(req.media['repeat']) == -1:
                schedule.every(timeout).seconds.do(run_threaded, record, recorder=recorder,
                                                   recorder_args=recorder_args, timeout=timeout).tag(f'{recorder}{recorder_args}-{timeout}')
                ok_response(resp)
            else:
                response = recorder_req(recorder, recorder_args, timeout)
                time.sleep(timeout)
                for _ in range(int(req.media['repeat'])):
                    response = recorder_req(recorder, recorder_args, timeout)
                    time.sleep(timeout)
                if response:
                    ok_response(resp)
                else:
                    ok_response(
                        resp, f'Request {recorder} {recorder_args} failed.')
        except Exception as e:
            error_response(resp, f'{e}')


def record(recorder, recorder_args, timeout):
    recorder_req(recorder, recorder_args, timeout)


def run_threaded(job_func, recorder, recorder_args, timeout):
    job_thread = threading.Thread(
        target=job_func, args=(recorder, recorder_args, timeout,))
    job_thread.start()


def init_prom_vars():
    prom_vars = {
        'last_bin_freq_time': Gauge('last_bin_freq_time', 'epoch time last signal in each bin', labelnames=('bin_mhz',)),
        'worker_record_request': Gauge('worker_record_request', 'record requests made to workers', labelnames=('worker',)),
        'freq_power': Gauge('freq_power', 'bin frequencies and db over time', labelnames=('bin_freq',)),
        'new_bins': Counter('new_bins', 'frequencies of new bins', labelnames=('bin_freq',)),
        'old_bins': Counter('old_bins', 'frequencies of old bins', labelnames=('bin_freq',)),
        'bin_freq_count': Counter('bin_freq_count', 'count of signals in each bin', labelnames=('bin_mhz',)),
        'frame_counter': Counter('frame_counter', 'number of frames processed'),
    }
    return prom_vars


def update_prom_vars(peak_dbs, new_bins, old_bins, prom_vars):
    freq_power = prom_vars['freq_power']
    new_bins_prom = prom_vars['new_bins']
    old_bins_prom = prom_vars['old_bins']
    for freq in peak_dbs:
        freq_power.labels(bin_freq=freq).set(peak_dbs[freq])
    for nbin in new_bins:
        new_bins_prom.labels(bin_freq=nbin).inc()
    for obin in old_bins:
        old_bins_prom.labels(bin_freq=obin).inc()


def process_fft(args, prom_vars, ts, fftbuffer, lastbins):
    global PEAK_DBS
    df = pd.DataFrame(fftbuffer, columns=['ts', 'freq', 'db'])
    df['freq'] /= 1e6
    df = df.sort_values('freq')
    df['freqdiffs'] = df.freq - df.freq.shift()
    mindiff = df.freqdiffs.min()
    maxdiff = df.freqdiffs.max()
    meandiff = df.freqdiffs.mean()
    logging.info(
        'new frame, frequency sample differences min %f mean %f max %f',
        mindiff, meandiff, maxdiff)
    if meandiff > mindiff * 2:
        logging.warning('mean frequency diff larger than minimum - increase scanner sample rate')
        logging.warning(df[df.freqdiffs > mindiff * 2])
    df = calc_db(df)
    if args.fftlog:
        df.to_csv(args.fftlog, sep='\t', index=False)
    monitor_bins = set()
    peak_dbs = {}
    bin_freq_count = prom_vars['bin_freq_count']
    last_bin_freq_time = prom_vars['last_bin_freq_time']
    freq_start_mhz = args.freq_start / 1e6
    freq_end_mhz = args.freq_end / 1e6
    for peak_freq, peak_db in scipy_find_sig_windows(df, width=args.width, prominence=args.prominence, threshold=args.threshold):
        if peak_freq < freq_start_mhz or peak_freq > freq_end_mhz:
            logging.info('ignoring peak at %f MHz', peak_freq)
            continue
        center_freq = get_center(
            peak_freq, freq_start_mhz, args.bin_mhz, args.record_bw_mbps)
        logging.info('detected peak at %f MHz %f dB, assigned bin frequency %f MHz', peak_freq, peak_db, center_freq)
        bin_freq_count.labels(bin_mhz=center_freq).inc()
        last_bin_freq_time.labels(bin_mhz=ts).set(ts)
        monitor_bins.add(center_freq)
        peak_dbs[center_freq] = peak_db
    logging.info('current bins %f to %f MHz: %s',
                 df['freq'].min(), df['freq'].max(), sorted(peak_dbs.items()))
    PEAK_DBS = sorted(peak_dbs.items())
    new_bins = monitor_bins - lastbins
    if new_bins:
        logging.info('new bins: %s', sorted(new_bins))
    old_bins = lastbins - monitor_bins
    if old_bins:
        logging.info('old bins: %s', sorted(old_bins))
    update_prom_vars(peak_dbs, new_bins, old_bins, prom_vars)
    return monitor_bins


def recorder_req(recorder, recorder_args, timeout):
    url = f'{recorder}/v1/{recorder_args}'
    try:
        req = requests.get(url, timeout=timeout)
        logging.debug(str(req))
        return req
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as err:
        logging.debug(str(err))
        return None


def get_freq_exclusions(args):
    recorder_freq_exclusions = {}
    for recorder in args.recorder:
        req = recorder_req(recorder, 'info', args.record_secs)
        if req is None or req.status_code != 200:
            continue
        excluded = json.loads(req.text).get('freq_excluded', None)
        if excluded is None:
            continue
        recorder_freq_exclusions[recorder] = parse_freq_excluded(excluded)
    return recorder_freq_exclusions


def call_record_signals(args, lastbins_history, prom_vars):
    if lastbins_history:
        signals = []
        for bins in lastbins_history:
            signals.extend(list(bins))
        recorder_freq_exclusions = get_freq_exclusions(
            args)
        recorder_count = len(recorder_freq_exclusions)
        record_signals = choose_record_signal(
            signals, recorder_count)
        for signal, recorder in choose_recorders(record_signals, recorder_freq_exclusions):
            signal_hz = int(signal * 1e6)
            record_bps = int(args.record_bw_mbps * (1024 * 1024))
            record_samples = int(
                record_bps * args.record_secs)
            recorder_args = f'record/{signal_hz}/{record_samples}/{record_bps}'
            resp = recorder_req(
                recorder, recorder_args, args.record_secs)
            if resp:
                worker_record_request = prom_vars['worker_record_request']
                worker_record_request.labels(worker=recorder).set(signal_hz)


def zstd_file(uncompressed_file):
    subprocess.check_call(['/usr/bin/zstd', '--rm', uncompressed_file])


def process_fft_lines(args, prom_vars, fifo, ext, executor):
    lastfreq = 0
    fftbuffer = {}
    lastbins_history = []
    lastbins = set()
    frame_counter = prom_vars['frame_counter']
    txt_buf = ''
    last_fft_report = 0
    fft_packets = 0

    while True:
        if os.path.exists(args.log):
            logging.info(f'{args.log} exists, will append first')
            mode = 'ab'
        else:
            logging.info(f'opening {args.log}')
            mode = 'wb'
        openlogts = int(time.time())
        with open(args.log, mode=mode) as l:
            while True:
                schedule.run_pending()
                sock_txt = fifo.read()
                now = int(time.time())
                if sock_txt:
                    fft_packets += 1
                if now - last_fft_report > 5:
                    logging.info('received %u FFT packets, last freq %f MHz', fft_packets, lastfreq / 1e6)
                    fft_packets = 0
                    last_fft_report = now
                if sock_txt is None:
                    time.sleep(0.1)
                    continue
                txt_buf += sock_txt.decode('utf8')
                lines = txt_buf.splitlines()
                if txt_buf.endswith('\n'):
                    txt_buf = ''
                elif lines:
                    txt_buf = lines[-1]
                    lines = lines[:-1]
                rotatelognow = False
                for line in lines:
                    try:
                        ts, freq, pw = [float(x) for x in line.strip().split()]
                    except ValueError as err:
                        logging.info('could not parse FFT data: %s: %s', line, err)
                        continue
                    if pw < 0 or pw > 2:
                        logging.info('power %f out of range on %s', pw, line)
                        continue
                    if freq < 0 or freq > 10e9:
                        logging.info('frequency %f out of range on %s', freq, line)
                        continue
                    if abs(now - ts) > 60:
                        logging.info('timestamp %f out of range on %s', ts, line)
                        continue
                    l.write(line.encode('utf8') + b'\n')
                    rollover = abs(freq - lastfreq) > ROLLOVERHZ and fftbuffer
                    lastfreq = freq
                    if rollover:
                        frame_counter.inc()
                        fftbuffer = [(data[0], freq, data[1]) for freq, data in sorted(fftbuffer.items())]
                        new_lastbins = process_fft(
                            args, prom_vars, ts, fftbuffer, lastbins)
                        if new_lastbins is not None:
                            lastbins = new_lastbins
                            if lastbins:
                                lastbins_history = [lastbins] + lastbins_history
                                lastbins_history = lastbins_history[:args.history]
                            call_record_signals(args, lastbins_history, prom_vars)
                        fftbuffer = {}
                        rotate_age = now - openlogts
                        if rotate_age > args.rotatesecs:
                            rotatelognow = True
                    fftbuffer[round(freq / SCAN_FRES) * SCAN_FRES] = (ts, pw)
                if rotatelognow:
                    break
        new_log = args.log.replace(ext, f'{openlogts}{ext}')
        os.rename(args.log, new_log)
        executor.submit(zstd_file, new_log)


def udp_proxy(args, fifo_name):
    with open(fifo_name, 'wb') as fifo:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock:
            udp_sock.bind((args.logaddr, args.logport))
            while True:
                sock_txt, _ = udp_sock.recvfrom(MTU)
                if sock_txt:
                    fifo.write(sock_txt)
                    fifo.flush()


def find_signals(args, prom_vars):
    try:
        ext = args.log[args.log.rindex('.'):]
    except ValueError:
        logging.fatal(f'cannot parse extension from {args.log}')

    with tempfile.TemporaryDirectory() as tempdir:
        fifo_name = os.path.join(tempdir, 'fftfifo')
        os.mkfifo(fifo_name)

        with concurrent.futures.ProcessPoolExecutor(2) as executor:
            executor.submit(udp_proxy, args, fifo_name)
            with open(fifo_name, 'rb') as fifo:
                os.set_blocking(fifo.fileno(), False)
                process_fft_lines(args, prom_vars, fifo, ext, executor)


def main():
    parser = argparse.ArgumentParser(
        description='watch a scan UDP stream and find signals')
    parser.add_argument('--log', default='scan.log', type=str,
                        help='base path for scan logging')
    parser.add_argument('--fftlog', default='', type=str,
                        help='if defined, path to log last complete FFT frame')
    parser.add_argument('--rotatesecs', default=3600, type=int,
                        help='rotate scan log after this many seconds')
    parser.add_argument('--logaddr', default='127.0.0.1', type=str,
                        help='UDP stream address')
    parser.add_argument('--logport', default=8001, type=int,
                        help='UDP stream port')
    parser.add_argument('--bin_mhz', default=20, type=int,
                        help='monitoring bin width in MHz')
    parser.add_argument('--width', default=3, type=int,
                        help=f'minimum signal sample width to detect a peak (multiple of {SCAN_FRES}Hz)')
    parser.add_argument('--threshold', default=-40, type=float,
                        help='signal finding threshold')
    parser.add_argument('--prominence', default=5, type=float,
                        help='minimum peak prominence (see scipy.signal.find_peaks)')
    parser.add_argument('--history', default=50, type=int,
                        help='number of frames of signal history to keep')
    parser.add_argument('--recorder', action='append', default=[],
                        help='SDR recorder base URLs (e.g. http://host:port/, multiples can be specified)')
    parser.add_argument('--record_bw_mbps', default=20, type=int,
                        help='record bandwidth in mbps')
    parser.add_argument('--record_secs', default=10, type=int,
                        help='record time duration in seconds')
    parser.add_argument('--promport', dest='promport', type=int, default=9000,
                        help='Prometheus client port')
    parser.add_argument(
        '--freq-end', dest='freq_end', type=float, default=float(1e9),
        help='Set freq_end [default=%(default)r]')
    parser.add_argument(
        '--freq-start', dest='freq_start', type=float, default=float(100e6),
        help='Set freq_start [default=%(default)r]')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(message)s')
    prom_vars = init_prom_vars()
    start_http_server(args.promport)
    x = threading.Thread(target=find_signals, args=(args, prom_vars,))
    x.start()
    app = falcon.App()
    scanner_form = ScannerForm()
    result = Result()
    active_requests = ActiveRequests()
    app.add_route('/', scanner_form)
    app.add_route('/result', result)
    app.add_route('/requests', active_requests)
    bjoern.run(app, '0.0.0.0', 80)


if __name__ == '__main__':
    main()
