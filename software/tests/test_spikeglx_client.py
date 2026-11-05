"""Compare live-map math with SpikeGLX's AP extrema and voltage normalization."""
from ctypes import POINTER, c_short, cast
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock

import numpy as np

from opendrive.spikeglx_client import (SpikeGLXConfig, SpikeGLXLiveClient, SpikeGLXStreamInfo,
                                        SpikeGLXUnavailable)


class FakeSDK:
    def __init__(self):
        self.factors = [2e-6, 3e-6]
        self.valid = True
        self.calibrations = []
        self.fetches = []
        self.count_streams = []
        self.available = 0

    def c_sglx_getStreamI16ToVolts(self, out, handle, js, ip, channel):
        self.calibrations.append((js, ip, channel))
        out._obj.value = self.factors[channel]
        return self.valid

    def c_sglx_getError(self, handle):
        return b'Calibration unavailable'

    def c_sglx_getStreamSampleCount(self, handle, js, ip):
        self.count_streams.append(js)
        self.available += 10
        return self.available

    def c_sglx_fetch(self, out, n_data, handle, js, ip, start, count, channels, n_channels, downsample):
        self.fetches.append((js, start, count))
        # Extrema in successive blocks lie on opposite sides of zero. Taking
        # the largest single-block p-p would be wrong for a multi-block window.
        pair = (100, -200) if (start // 10) % 2 else (-400, 1100)
        self.buffer = (c_short * (count * n_channels))(*(pair * count))
        cast(out, POINTER(POINTER(c_short)))[0] = cast(self.buffer, POINTER(c_short))
        n_data._obj.value = count * n_channels
        return start


class SpikeGLXMathTests(unittest.TestCase):
    def test_parallel_clients_serialize_native_connection_lifecycle(self):
        # The native library keeps one unsynchronized connection map per
        # process, even when callers use separate handles.
        active = 0
        maximum = 0
        barrier = threading.Barrier(6)
        def native_call(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(.005)
            active -= 1
            return object()
        sdk = Mock()
        for name in ('c_sglx_createHandle', 'c_sglx_connect', 'c_sglx_close', 'c_sglx_destroyHandle'):
            getattr(sdk, name).side_effect = native_call
        def connect_and_close(_):
            client = SpikeGLXLiveClient(SpikeGLXConfig())
            client._load_sdk = Mock(return_value=sdk)
            client.get_stream_info = Mock(return_value=SpikeGLXStreamInfo('test', 'AP', 1, 2, 2, 100, 0, ''))
            barrier.wait(timeout=2)
            client.connect()
            client.close()
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(connect_and_close, range(6)))
        self.assertEqual(maximum, 1)

    def test_initial_calibration_finishes_before_sampling_window_is_chosen(self):
        client = self.client()
        original = client._sglx.c_sglx_getStreamI16ToVolts
        def slow_calibration(*args):
            client._sglx.available += 100
            return original(*args)
        client._sglx.c_sglx_getStreamI16ToVolts = slow_calibration
        stop = threading.Event()
        frames = client.iter_heatmaps(stop, 200)
        try:
            next(frames)
            self.assertGreaterEqual(client._sglx.fetches[0][1], 200)
        finally:
            stop.set(); frames.close()

    def client(self, use_numpy=True):
        client = SpikeGLXLiveClient(SpikeGLXConfig(max_channels=2, use_numpy=use_numpy,
                                                 compute_diagnostics=False, geometry_refresh_s=0))
        client._sglx, client._handle = FakeSDK(), object()
        client.get_stream_info = Mock(return_value=SpikeGLXStreamInfo('test', 'AP', 1, 2, 2, 100, 0, ''))
        return client

    def test_gain_aware_calibration_and_full_int16_span_agree_in_both_paths(self):
        data = (c_short * 6)(-32768, -5, 32767, 10, 0, 2)
        for use_numpy in (False, True):
            with self.subTest(use_numpy=use_numpy):
                client = self.client(use_numpy)
                result = client._peak_to_peak_mv(data, 3, 2)
                np.testing.assert_allclose(result, [65535 * .002, 15 * .003])
                self.assertEqual(client._sglx.calibrations, [(2, 0, 0), (2, 0, 1)])
                client._peak_to_peak_mv(data, 3, 2)
                self.assertEqual(len(client._sglx.calibrations), 2)

    def test_invalid_calibration_is_reported_and_not_cached_as_zero_activity(self):
        client = self.client()
        for value in (0, -1, float('nan'), float('inf')):
            client._sglx.factors[0] = value
            with self.assertRaises(SpikeGLXUnavailable):
                client._channel_i16_to_mv(0)
            self.assertNotIn(0, client._volts_per_i16)
        client._sglx.factors[0] = 2e-6
        client._sglx.valid = False
        with self.assertRaises(SpikeGLXUnavailable):
            client._channel_i16_to_mv(0)
        client._sglx.valid = True
        self.assertAlmostEqual(client._channel_i16_to_mv(0), .002)

    def test_contiguous_window_matches_spikeglx_extrema_across_all_blocks(self):
        for use_numpy in (False, True):
            with self.subTest(use_numpy=use_numpy):
                client = self.client(use_numpy)
                stop = threading.Event()
                frames = client.iter_heatmaps(stop, 200)
                try:
                    first, second = next(frames), next(frames)
                    # SpikeGLX Heatmap::normPkPk: (max-min) * volts/count * 1000.
                    np.testing.assert_allclose(first.peak_to_peak_mv, [500 * .002, 1300 * .003])
                    self.assertEqual(first.peak_to_peak_mv, second.peak_to_peak_mv)
                    self.assertEqual(first.fetched_samples, 20)
                    self.assertEqual(client._sglx.fetches, [(-2, start, 10) for start in (10, 20, 30, 40)])
                    self.assertTrue(all(js == 2 for js in client._sglx.count_streams))
                finally:
                    stop.set()
                    frames.close()

    def test_window_uses_spikeglx_100ms_blocks_and_stops_without_partial_frame(self):
        client = self.client()
        stop = threading.Event()
        frames = client.iter_heatmaps(stop, 150)
        self.assertEqual(next(frames).fetched_samples, 10)
        stop.set()
        with self.assertRaises(StopIteration):
            next(frames)

    def test_older_server_uses_raw_clock_but_still_fetches_filtered_blocks(self):
        client = self.client()
        sdk = client._sglx
        original_count = sdk.c_sglx_getStreamSampleCount
        def count(handle, js, ip):
            if js == -2:
                sdk.count_streams.append(js)
                return 0
            return original_count(handle, js, ip)
        sdk.c_sglx_getStreamSampleCount = count
        sdk.c_sglx_getError = lambda handle: b'GETSTREAMSAMPLECOUNT: stream-js must be in range [0..2].'
        stop = threading.Event()
        frames = client.iter_heatmaps(stop, 200)
        try:
            frame = next(frames)
            np.testing.assert_allclose(frame.peak_to_peak_mv, [1., 3.9])
            self.assertEqual(sdk.count_streams.count(-2), 0)
            self.assertTrue(all(fetch[0] == -2 for fetch in sdk.fetches))
        finally:
            stop.set()
            frames.close()

    def test_missing_block_is_reported_instead_of_colorizing_incomplete_window(self):
        client = self.client()
        fetch = client._sglx.c_sglx_fetch
        def partial(*args):
            head = fetch(*args)
            args[1]._obj.value -= 2
            return head
        client._sglx.c_sglx_fetch = partial
        stop = threading.Event()
        frames = client.iter_heatmaps(stop, 200)
        try:
            with self.assertRaisesRegex(SpikeGLXUnavailable, 'contiguous'):
                next(frames)
        finally:
            stop.set()
            frames.close()
