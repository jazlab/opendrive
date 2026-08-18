"""Optional, read-only SpikeGLX live-data adapter."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from ctypes import POINTER, byref, c_bool, c_double, c_int, c_short
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SpikeGLXError(RuntimeError):
    """Base class for SpikeGLX connection and streaming errors."""


class SpikeGLXUnavailable(SpikeGLXError):
    """Raised when the SDK or running SpikeGLX server is unavailable."""


@dataclass(frozen=True)
class SpikeGLXConfig:
    """Connection settings for the SpikeGLX command server."""

    host: str = "localhost"
    port: int = 4142
    js: int = 2
    ip: int = 0
    max_channels: int = 384
    samples_per_fetch: int = 300
    prefer_filtered: bool = True
    include_geometry: bool = True
    geometry_refresh_s: float = 2.0
    compute_diagnostics: bool = True
    use_numpy: bool = True


@dataclass(frozen=True)
class SpikeGLXStreamInfo:
    """Static-ish stream details reported by SpikeGLX."""

    version: str
    stream_label: str
    probe_count: int
    channel_count: int
    ap_channel_count: int
    sample_rate_hz: float
    sample_count: int
    source_path: str


@dataclass(frozen=True)
class SpikeGLXSnapshot:
    """One lightweight live-data sample for UI diagnostics."""

    sample_count: int
    fetched_samples: int
    latest_head_sample: int
    rms_i16: float
    threshold_crossings: int
    chunk_rate_hz: float
    late_chunks: int
    dropped_chunks: int
    peak_to_peak_mv: list[float]
    channel_depths_mm: list[float]
    channel_x_mm: list[float]
    channel_shanks: list[int]
    data_source: str


def sdk_search_paths() -> list[Path]:
    """Return likely Python wrapper folders without importing the SDK."""
    paths: list[Path] = []
    env_path = os.environ.get("SPIKEGLX_SDK_PYTHON", "").strip()
    if env_path:
        paths.append(Path(env_path))

    module_path = Path(__file__).resolve()
    repo_root = module_path.parents[2]
    paths.extend(
        [
            repo_root / "sglx_pkg",
            repo_root / "SpikeGLX-CPP-SDK" / "Windows" / "Python",
            repo_root / "external" / "SpikeGLX-CPP-SDK" / "Windows" / "Python",
            repo_root.parent / "SpikeGLX-CPP-SDK" / "Windows" / "Python",
        ]
    )
    return paths


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _stream_label(js: int, ip: int) -> str:
    if js == 0:
        return f"nidq stream {ip}"
    if js == 1:
        return f"obx stream {ip}"
    if js == 2:
        return f"AP imec probe {ip}"
    if js == -2:
        return f"filtered AP imec probe {ip}"
    return f"stream js={js}, ip={ip}"


@contextmanager
def _spikeglx_command_lock():
    """Serialize SDK command-server calls across MonkeyDrive windows/processes."""
    yield


class SpikeGLXLiveClient:
    """Small wrapper around the SpikeGLX C API used by probe-map viewers."""

    def __init__(self, config: SpikeGLXConfig):
        self.config = config
        self._sglx = None
        self._handle = None
        self._source_path = ""
        self._dll_dirs = []
        self._last_fetch_time = 0.0
        self._last_sample_count = 0
        self._late_chunks = 0
        self._dropped_chunks = 0
        self._volts_per_i16: dict[int, float] = {}
        self._channel_depths_mm: list[float] = []
        self._channel_x_mm: list[float] = []
        self._channel_shanks: list[int] = []
        self._last_geometry_refresh = 0.0
        self._debug_callback = None

    def set_debug_callback(self, callback):
        self._debug_callback = callback

    def _debug(self, message: str):
        if self._debug_callback is None:
            return
        try:
            self._debug_callback(message)
        except Exception:
            pass

    def connect(self) -> SpikeGLXStreamInfo:
        """Connect to SpikeGLX and query the selected live stream."""
        self._sglx = self._load_sdk()
        self._handle = self._sglx.c_sglx_createHandle()
        with _spikeglx_command_lock():
            ok = self._sglx.c_sglx_connect(
                self._handle,
                self.config.host.encode("utf-8"),
                int(self.config.port),
            )
            error = "" if ok else self._error_message()
        if not ok:
            self.close()
            raise SpikeGLXUnavailable(
                f"Could not connect to SpikeGLX at {self.config.host}:{self.config.port}. "
                "In SpikeGLX, open Options > Command Server Settings and enable the Remote "
                f"Command Server, then retry. {error}"
            )

        info = self.get_stream_info()
        self._last_sample_count = info.sample_count
        self._last_fetch_time = time.monotonic()
        return info

    def close(self):
        """Close and destroy the SDK handle."""
        if self._sglx is not None and self._handle is not None:
            with _spikeglx_command_lock():
                try:
                    self._sglx.c_sglx_close(self._handle)
                finally:
                    self._sglx.c_sglx_destroyHandle(self._handle)
        self._handle = None

    def get_stream_info(self) -> SpikeGLXStreamInfo:
        """Read stream metadata from the connected SpikeGLX server."""
        self._require_connected()
        sglx = self._sglx
        handle = self._handle
        config = self.config
        metadata_js = self._metadata_js()

        with _spikeglx_command_lock():
            probe_count = c_int()
            self._debug(f"get_stream_info getStreamNP start js={metadata_js} ip={config.ip}")
            sglx.c_sglx_getStreamNP(byref(probe_count), handle, metadata_js)
            self._debug(f"get_stream_info getStreamNP done probes={probe_count.value}")

            n_types = c_int()
            ap_channel_count = 0
            channel_count = 0
            self._debug("get_stream_info getStreamAcqChans start")
            if sglx.c_sglx_getStreamAcqChans(byref(n_types), handle, metadata_js, config.ip):
                self._debug(f"get_stream_info getStreamAcqChans done n_types={n_types.value}")
                counts = [int(sglx.c_sglx_getint(handle, idx)) for idx in range(max(0, n_types.value))]
                channel_count = sum(max(0, count) for count in counts)
                if counts:
                    ap_channel_count = max(0, counts[0])
            else:
                self._debug("get_stream_info getStreamAcqChans returned false")

            self._debug("get_stream_info sample_rate start")
            sample_rate = float(sglx.c_sglx_getStreamSampleRate(handle, metadata_js, config.ip))
            self._debug(f"get_stream_info sample_rate done value={sample_rate}")
            self._debug("get_stream_info sample_count start")
            sample_count = int(sglx.c_sglx_getStreamSampleCount(handle, metadata_js, config.ip))
            self._debug(f"get_stream_info sample_count done value={sample_count}")
            self._debug("get_stream_info version start")
            version = _decode(sglx.c_sglx_getVersion(handle))
            self._debug(f"get_stream_info version done value={version}")
            now = time.monotonic()
            geometry_stale = (
                config.geometry_refresh_s > 0
                and now - self._last_geometry_refresh >= config.geometry_refresh_s
            )
            if config.include_geometry and (not self._channel_depths_mm or geometry_stale):
                self._debug("get_stream_info geometry start")
                geometry = self._get_channel_geometry(config.ip)
                self._debug(f"get_stream_info geometry done count={len(geometry)}")
                if geometry:
                    self._channel_depths_mm = [entry[2] for entry in geometry]
                    self._channel_x_mm = [entry[1] for entry in geometry]
                    self._channel_shanks = [entry[0] for entry in geometry]
                    self._last_geometry_refresh = now

        return SpikeGLXStreamInfo(
            version=version,
            stream_label=_stream_label(config.js, config.ip),
            probe_count=max(0, int(probe_count.value)),
            channel_count=channel_count,
            ap_channel_count=ap_channel_count,
            sample_rate_hz=sample_rate,
            sample_count=sample_count,
            source_path=self._source_path,
        )

    def fetch_snapshot(self) -> SpikeGLXSnapshot:
        """Fetch a small latest-data chunk and summarize it for the UI."""
        self._require_connected()
        sglx = self._sglx
        handle = self._handle
        config = self.config

        self._debug("fetch_snapshot get_stream_info start")
        info = self.get_stream_info()
        self._debug(
            f"fetch_snapshot get_stream_info done sample_count={info.sample_count} "
            f"ap_chans={info.ap_channel_count} chans={info.channel_count}"
        )
        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_fetch_time)
        produced = max(0, info.sample_count - self._last_sample_count)
        chunk_rate = produced / elapsed
        if info.sample_rate_hz > 0 and produced > info.sample_rate_hz * elapsed * 1.5:
            self._late_chunks += 1

        channels_to_fetch = info.ap_channel_count or info.channel_count or 1
        channels_to_fetch = max(1, min(channels_to_fetch, max(1, config.max_channels)))
        channels = (c_int * channels_to_fetch)(*range(channels_to_fetch))
        data = POINTER(c_short)()
        n_data = c_int()

        fetch_js = -2 if config.prefer_filtered and config.js == 2 else config.js
        data_source = "SpikeGLX filtered AP" if fetch_js == -2 else "raw AP"
        fetch_count = max(1, int(config.samples_per_fetch))
        start_sample = max(0, info.sample_count - fetch_count)

        with _spikeglx_command_lock():
            if fetch_js == -2:
                self._debug(
                    f"fetch_snapshot fetch start js={fetch_js} ip={config.ip} "
                    f"start={start_sample} count={fetch_count} chans={channels_to_fetch}"
                )
                head = int(
                    sglx.c_sglx_fetch(
                        byref(data),
                        byref(n_data),
                        handle,
                        fetch_js,
                        config.ip,
                        start_sample,
                        fetch_count,
                        channels,
                        channels_to_fetch,
                        1,
                    )
                )
                self._debug(f"fetch_snapshot fetch done head={head} n_data={n_data.value}")
            else:
                self._debug(
                    f"fetch_snapshot fetchLatest start js={fetch_js} ip={config.ip} "
                    f"count={fetch_count} chans={channels_to_fetch}"
                )
                head = int(
                    sglx.c_sglx_fetchLatest(
                        byref(data),
                        byref(n_data),
                        handle,
                        fetch_js,
                        config.ip,
                        fetch_count,
                        channels,
                        channels_to_fetch,
                        1,
                    )
                )
                self._debug(f"fetch_snapshot fetchLatest done head={head} n_data={n_data.value}")

        if head == 0:
            self._dropped_chunks += 1
            if fetch_js == -2:
                raise SpikeGLXUnavailable(
                    "SpikeGLX filtered AP fetch returned no samples. Enable filtered IM stream "
                    "buffers on the SpikeGLX IM Setup tab, restart the run if needed, then retry."
                )
            fetched_samples = 0
            rms = 0.0
            crossings = 0
        else:
            fetched_samples = int(n_data.value / channels_to_fetch)
            total = int(n_data.value)
            if total > 0 and config.compute_diagnostics:
                self._debug(f"fetch_snapshot chunk_summary start total={total}")
                rms, crossings = self._chunk_summary(data, total)
                self._debug("fetch_snapshot chunk_summary done")
            else:
                rms = 0.0
                crossings = 0
            self._debug(
                f"fetch_snapshot peak_to_peak start samples={fetched_samples} chans={channels_to_fetch}"
            )
            peak_to_peak_mv = self._peak_to_peak_mv(data, fetched_samples, channels_to_fetch)
            self._debug(f"fetch_snapshot peak_to_peak done values={len(peak_to_peak_mv)}")

        self._last_fetch_time = now
        self._last_sample_count = info.sample_count
        return SpikeGLXSnapshot(
            sample_count=info.sample_count,
            fetched_samples=fetched_samples,
            latest_head_sample=head,
            rms_i16=rms,
            threshold_crossings=crossings,
            chunk_rate_hz=chunk_rate,
            late_chunks=self._late_chunks,
            dropped_chunks=self._dropped_chunks,
            peak_to_peak_mv=peak_to_peak_mv if head != 0 else [0.0] * channels_to_fetch,
            channel_depths_mm=self._channel_depths_mm[:channels_to_fetch],
            channel_x_mm=self._channel_x_mm[:channels_to_fetch],
            channel_shanks=self._channel_shanks[:channels_to_fetch],
            data_source=data_source,
        )

    def _get_channel_geometry(self, probe_index: int) -> list[tuple[int, float, float]]:
        n_values = c_int()
        if not self._sglx.c_sglx_getGeomMap(byref(n_values), self._handle, probe_index):
            return []

        geom: dict[str, str] = {}
        for idx in range(max(0, n_values.value)):
            length = c_int()
            item = _decode(self._sglx.c_sglx_getstr(byref(length), self._handle, idx))
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            geom[key.strip()] = value.strip()

        shanks_by_channel: dict[int, int] = {}
        xs_by_channel: dict[int, float] = {}
        depths_by_channel: dict[int, float] = {}
        for key, value in geom.items():
            if not key.startswith("ch") or "_" not in key:
                continue
            try:
                channel_text, field = key[2:].split("_", 1)
                channel = int(channel_text)
            except ValueError:
                continue
            if field == "s":
                try:
                    shanks_by_channel[channel] = int(float(value))
                except ValueError:
                    pass
            elif field == "x":
                try:
                    xs_by_channel[channel] = float(value) / 1000.0
                except ValueError:
                    pass
            elif field == "z":
                try:
                    depths_by_channel[channel] = float(value) / 1000.0
                except ValueError:
                    pass

        if not depths_by_channel:
            return []
        max_channel = max(depths_by_channel)
        return [
            (
                shanks_by_channel.get(channel, 0),
                xs_by_channel.get(channel, 0.0),
                depths_by_channel.get(channel, channel * 0.01),
            )
            for channel in range(max_channel + 1)
        ]

    def _peak_to_peak_mv(self, data, sample_count: int, channel_count: int) -> list[float]:
        if sample_count <= 0 or channel_count <= 0:
            return []

        if self.config.use_numpy:
            try:
                import numpy as np

                array = np.ctypeslib.as_array(data, shape=(sample_count * channel_count,))
                shaped = array.reshape(sample_count, channel_count)
                ranges = shaped.max(axis=0).astype(np.float64) - shaped.min(axis=0).astype(np.float64)
                multipliers = np.asarray(
                    [self._channel_i16_to_mv(channel) for channel in range(channel_count)],
                    dtype=np.float64,
                )
                return (ranges * multipliers).tolist()
            except Exception:
                pass

        mins = [32767] * channel_count
        maxes = [-32768] * channel_count
        total = sample_count * channel_count
        for idx in range(total):
            channel = idx % channel_count
            value = int(data[idx])
            if value < mins[channel]:
                mins[channel] = value
            if value > maxes[channel]:
                maxes[channel] = value

        values: list[float] = []
        for channel in range(channel_count):
            values.append((maxes[channel] - mins[channel]) * self._channel_i16_to_mv(channel))
        return values

    def _chunk_summary(self, data, total: int) -> tuple[float, int]:
        try:
            import numpy as np

            array = np.ctypeslib.as_array(data, shape=(total,)).astype(np.float64)
            rms = float(np.sqrt(np.mean(array * array)))
            crossings = int(np.count_nonzero(np.abs(array) >= 750))
            return rms, crossings
        except Exception:
            square_sum = 0
            crossings = 0
            for idx in range(total):
                value = int(data[idx])
                square_sum += value * value
                if abs(value) >= 750:
                    crossings += 1
            return (square_sum / total) ** 0.5, crossings

    def _channel_i16_to_mv(self, channel: int) -> float:
        if channel in self._volts_per_i16:
            return self._volts_per_i16[channel] * 1000.0

        multiplier = c_double()
        with _spikeglx_command_lock():
            ok = self._sglx.c_sglx_getStreamI16ToVolts(
                byref(multiplier),
                self._handle,
                self._metadata_js(),
                self.config.ip,
                channel,
            )
        self._volts_per_i16[channel] = float(multiplier.value) if ok else 0.0
        return self._volts_per_i16[channel] * 1000.0

    def _metadata_js(self) -> int:
        return 2 if self.config.js == -2 else self.config.js

    def _load_sdk(self):
        """Import sglx_pkg.sglx from known locations, if available."""
        errors: list[str] = []
        for path in sdk_search_paths():
            try:
                path = path.resolve()
            except OSError:
                continue

            if (path / "sglx_pkg").is_dir():
                python_path = path
                package_path = path / "sglx_pkg"
            elif path.name == "sglx_pkg" and path.is_dir():
                python_path = path.parent
                package_path = path
            else:
                continue

            api_path = python_path.parent / "API"
            try:
                if hasattr(os, "add_dll_directory"):
                    if package_path.is_dir():
                        self._dll_dirs.append(os.add_dll_directory(str(package_path)))
                    if api_path.is_dir():
                        self._dll_dirs.append(os.add_dll_directory(str(api_path)))
                if str(python_path) not in sys.path:
                    sys.path.insert(0, str(python_path))
                module = importlib.import_module("sglx_pkg.sglx")
                self._source_path = str(python_path)
                return module
            except Exception as exc:  # ctypes raises OSError for missing DLLs.
                errors.append(f"{python_path}: {exc}")

        searched = "\n".join(f"- {path}" for path in sdk_search_paths())
        details = "\n".join(errors)
        if details:
            details = f"\n\nImport errors:\n{details}"
        raise SpikeGLXUnavailable(
            "SpikeGLX SDK Python wrapper was not found or could not load SglxApi.dll.\n\n"
            "Install the SpikeGLX CPP SDK and set SPIKEGLX_SDK_PYTHON to its Windows/Python folder, "
            "or place sglx_pkg beside MonkeyDrive.\n\n"
            f"Searched:\n{searched}{details}"
        )

    def _error_message(self) -> str:
        if self._sglx is None or self._handle is None:
            return ""
        try:
            return _decode(self._sglx.c_sglx_getError(self._handle))
        except Exception:
            return ""

    def _require_connected(self):
        if self._sglx is None or self._handle is None:
            raise SpikeGLXUnavailable("SpikeGLX is not connected.")
