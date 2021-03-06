#
# Copyright (c) 2019, Neptune Labs Sp. z o.o.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import threading
import time
from collections import namedtuple
from itertools import groupby

from future.moves import queue

from neptune.api_exceptions import NeptuneApiException
from neptune.internal.channels.channels import ChannelValue, ChannelIdWithValues
from neptune.internal.threads.neptune_thread import NeptuneThread


class ChannelsValuesSender(object):
    _QUEUED_CHANNEL_VALUE = namedtuple("QueuedChannelValue", ['channel_name', 'channel_type', 'channel_value'])

    __LOCK = threading.RLock()

    def __init__(self, experiment):
        self._experiment = experiment
        self._values_queue = None
        self._sending_thread = None

    def send(self, channel_name, channel_type, channel_value):
        with self.__LOCK:
            if not self._is_running():
                self._start()

        self._values_queue.put(self._QUEUED_CHANNEL_VALUE(
            channel_name=channel_name,
            channel_type=channel_type,
            channel_value=channel_value
        ))

    def join(self):
        with self.__LOCK:
            if self._is_running():
                self._sending_thread.interrupt()
                self._sending_thread.join()
                self._sending_thread = None
                self._values_queue = None

    def _is_running(self):
        return self._values_queue is not None and self._sending_thread is not None and self._sending_thread.is_alive()

    def _start(self):
        self._values_queue = queue.Queue()
        self._sending_thread = ChannelsValuesSendingThread(self._experiment, self._values_queue)
        self._sending_thread.start()


class ChannelsValuesSendingThread(NeptuneThread):
    _SLEEP_TIME = 5
    _MAX_VALUES_BATCH_LENGTH = 100

    def __init__(self, experiment, values_queue):
        super(ChannelsValuesSendingThread, self).__init__(is_daemon=False)
        self._values_queue = values_queue
        self._experiment = experiment
        self._sleep_time = self._SLEEP_TIME
        self._values_batch = []

    def run(self):
        sleep_time = self._SLEEP_TIME
        while not self.is_interrupted() or not self._values_queue.empty():
            sleep_start = time.time()
            try:
                self._values_batch.append(self._values_queue.get(timeout=sleep_time))
                self._values_queue.task_done()
                sleep_time -= time.time() - sleep_start
            except queue.Empty:
                sleep_time = 0

            if sleep_time <= 0 or len(self._values_batch) >= self._MAX_VALUES_BATCH_LENGTH:
                self._process_batch()
                sleep_time = self._SLEEP_TIME

        self._process_batch()

    def join(self, timeout=None):
        self.interrupt()
        super(ChannelsValuesSendingThread, self).join(timeout)

    def _process_batch(self):
        send_start = time.time()
        if self._values_batch:
            self._send_values(self._values_batch)
            self._values_batch = []
        self._sleep_time = self._SLEEP_TIME - (time.time() - send_start)

    def _send_values(self, queued_channels_values):
        channel_key = lambda value: (value.channel_name, value.channel_type)
        queued_grouped_by_channel = {channel: list(values)
                                     for channel, values
                                     in groupby(sorted(queued_channels_values, key=channel_key),
                                                channel_key)}
        channels_with_values = []
        for (channel_name, channel_type) in queued_grouped_by_channel:
            # pylint: disable=protected-access
            channel = self._experiment._get_channel(channel_name, channel_type)
            last_x = channel.x if channel.x else 0
            channel_values = []
            for queued_value in queued_grouped_by_channel[(channel_name, channel_type)]:
                x = queued_value.channel_value.x if queued_value.channel_value.x is not None else last_x + 1
                channel_values.append(ChannelValue(ts=queued_value.channel_value.ts,
                                                   x=x,
                                                   y=queued_value.channel_value.y))
                last_x = x

            channels_with_values.append(ChannelIdWithValues(channel.id, channel_values))

        # pylint: disable=protected-access
        try:
            self._experiment._send_channels_values(channels_with_values)
        except (NeptuneApiException, IOError):
            pass
