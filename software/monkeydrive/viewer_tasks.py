"""Coalesced viewer commands and latest-only background image work."""
from collections import OrderedDict
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class ViewerCommands:
    """Keep the newest state per motor, without losing geometry to a position update.

    Used on both sides of the pipe so a busy viewer cannot replay old positions.
    Only state-setting commands belong here, never physical motor commands.
    """
    def __init__(self):
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._closing = False

    def put(self, message):
        message = dict(message)
        kind = message.get('type')
        with self._condition:
            if self._closing:
                return
            if kind == 'close':
                self._pending.clear()
                self._closing = True
            if kind == 'motor_state':
                message['motor_state'] = dict(message['motor_state'])
                key = ('motor', message['motor_state']['motor_id'])
            elif kind == 'motor_position':
                key = ('motor', message.get('motor_id'))
                previous = self._pending.get(key)
                if previous and previous['type'] == 'motor_state':
                    state = dict(previous['motor_state'])
                    state['absolute'] = message['position']
                    message = {'type': 'motor_state', 'motor_state': state}
            else:
                key = (kind, None)
            # Replacing in place keeps a noisy motor from starving other motors.
            self._pending[key] = message
            self._condition.notify()

    def get(self, timeout=None):
        with self._condition:
            if not self._condition.wait_for(lambda: bool(self._pending), timeout):
                raise queue.Empty
            return self._pending.popitem(last=False)[1]

    def get_nowait(self):
        return self.get(timeout=0)


class LatestWorker:
    """One worker per viewer; computation never touches Tk.

    One pending request/result per key, and obsolete results are discarded.
    submit/invalidate/close and completion callbacks all run on the Tk thread.
    """
    def __init__(self, widget):
        self._widget = widget
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._results = {}
        self._versions = {}
        self._sequence = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name='MRI rendering')
        self._thread.start()
        self._job = widget.after(16, self._poll)

    def submit(self, key, compute, complete):
        with self._condition:
            if self._closed:
                return
            self._sequence += 1
            self._versions[key] = self._sequence
            self._pending[key] = (self._sequence, compute, complete)
            self._results.pop(key, None)
            self._condition.notify()

    def invalidate(self):
        with self._condition:
            self._pending.clear()
            self._results.clear()
            self._versions.clear()

    def close(self):
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._results.clear()
            self._condition.notify()
        if self._job is not None:
            self._widget.after_cancel(self._job)
            self._job = None

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                key, (version, compute, complete) = self._pending.popitem(last=False)
            try:
                result, error = compute(), None
            except Exception as exc:
                result, error = None, exc
            with self._condition:
                if not self._closed and self._versions.get(key) == version:
                    self._results[key] = (version, complete, result, error)

    def _poll(self):
        self._job = None
        try:
            with self._condition:
                results = list(self._results.items())
                self._results.clear()
            for key, (version, complete, result, error) in results:
                if self._closed:
                    break
                if self._versions.get(key) == version:
                    try:
                        complete(result, error)
                    except Exception:
                        logger.exception('Viewer result callback failed')
        finally:
            if not self._closed:
                self._job = self._widget.after(16, self._poll)
