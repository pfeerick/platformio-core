# Copyright (c) 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import atexit
import platform
import Queue
import re
import threading
from collections import deque
from os import getenv, sep
from os.path import join
from time import sleep, time
from traceback import format_exc

import click
import requests

from platformio import __version__, app, exception, util


class TelemetryBase(object):

    def __init__(self):
        self._params = {}

    def __getitem__(self, name):
        return self._params.get(name, None)

    def __setitem__(self, name, value):
        self._params[name] = value

    def __delitem__(self, name):
        if name in self._params:
            del self._params[name]

    def send(self, hittype):
        raise NotImplementedError()


class MeasurementProtocol(TelemetryBase):

    TID = "UA-1768265-9"
    PARAMS_MAP = {
        "screen_name": "cd",
        "event_category": "ec",
        "event_action": "ea",
        "event_label": "el",
        "event_value": "ev"
    }

    def __init__(self):
        TelemetryBase.__init__(self)
        self['v'] = 1
        self['tid'] = self.TID
        self['cid'] = app.get_cid()

        self['sr'] = "%dx%d" % click.get_terminal_size()
        self._prefill_screen_name()
        self._prefill_appinfo()
        self._prefill_custom_data()

    def __getitem__(self, name):
        if name in self.PARAMS_MAP:
            name = self.PARAMS_MAP[name]
        return TelemetryBase.__getitem__(self, name)

    def __setitem__(self, name, value):
        if name in self.PARAMS_MAP:
            name = self.PARAMS_MAP[name]
        TelemetryBase.__setitem__(self, name, value)

    def _prefill_appinfo(self):
        self['av'] = __version__

        # gather dependent packages
        dpdata = []
        dpdata.append("PlatformIO/%s" % __version__)
        if app.get_session_var("caller_id"):
            dpdata.append("Caller/%s" % app.get_session_var("caller_id"))
        if getenv("PLATFORMIO_IDE"):
            dpdata.append("IDE/%s" % getenv("PLATFORMIO_IDE"))
        self['an'] = " ".join(dpdata)

    def _prefill_custom_data(self):

        def _filter_args(items):
            result = []
            stop = False
            for item in items:
                item = str(item).lower()
                result.append(item)
                if stop:
                    break
                if item == "account":
                    stop = True
            return result

        caller_id = str(app.get_session_var("caller_id"))
        self['cd1'] = util.get_systype()
        self['cd2'] = "Python/%s %s" % (platform.python_version(),
                                        platform.platform())
        # self['cd3'] = " ".join(_filter_args(sys.argv[1:]))
        self['cd4'] = 1 if (not util.is_ci()
                            and (caller_id or not util.is_container())) else 0
        if caller_id:
            self['cd5'] = caller_id.lower()

    def _prefill_screen_name(self):

        def _first_arg_from_list(args_, list_):
            for _arg in args_:
                if _arg in list_:
                    return _arg
            return None

        if not app.get_session_var("command_ctx"):
            return
        ctx_args = app.get_session_var("command_ctx").args
        args = [str(s).lower() for s in ctx_args if not str(s).startswith("-")]
        if not args:
            return
        cmd_path = args[:1]
        if args[0] in ("platform", "platforms", "serialports", "device",
                       "settings", "account"):
            cmd_path = args[:2]
        if args[0] == "lib" and len(args) > 1:
            lib_subcmds = ("builtin", "install", "list", "register", "search",
                           "show", "stats", "uninstall", "update")
            sub_cmd = _first_arg_from_list(args[1:], lib_subcmds)
            if sub_cmd:
                cmd_path.append(sub_cmd)
        elif args[0] == "remote" and len(args) > 1:
            remote_subcmds = ("agent", "device", "run", "test")
            sub_cmd = _first_arg_from_list(args[1:], remote_subcmds)
            if sub_cmd:
                cmd_path.append(sub_cmd)
                if len(args) > 2 and sub_cmd in ("agent", "device"):
                    remote2_subcmds = ("list", "start", "monitor")
                    sub_cmd = _first_arg_from_list(args[2:], remote2_subcmds)
                    if sub_cmd:
                        cmd_path.append(sub_cmd)
        self['screen_name'] = " ".join([p.title() for p in cmd_path])

    def send(self, hittype):
        if not app.get_setting("enable_telemetry"):
            return

        self['t'] = hittype

        # correct queue time
        if "qt" in self._params and isinstance(self['qt'], float):
            self['qt'] = int((time() - self['qt']) * 1000)

        MPDataPusher().push(self._params)


@util.singleton
class MPDataPusher(object):

    MAX_WORKERS = 5

    def __init__(self):
        self._queue = Queue.LifoQueue()
        self._failedque = deque()
        self._http_session = requests.Session()
        self._http_offline = False
        self._workers = []

    def push(self, item):
        # if network is off-line
        if self._http_offline:
            if "qt" not in item:
                item['qt'] = time()
            self._failedque.append(item)
            return

        self._queue.put(item)
        self._tune_workers()

    def in_wait(self):
        return self._queue.unfinished_tasks

    def get_items(self):
        items = list(self._failedque)
        try:
            while True:
                items.append(self._queue.get_nowait())
        except Queue.Empty:
            pass
        return items

    def _tune_workers(self):
        for i, w in enumerate(self._workers):
            if not w.is_alive():
                del self._workers[i]

        need_nums = min(self._queue.qsize(), self.MAX_WORKERS)
        active_nums = len(self._workers)
        if need_nums <= active_nums:
            return

        for i in range(need_nums - active_nums):
            t = threading.Thread(target=self._worker)
            t.daemon = True
            t.start()
            self._workers.append(t)

    def _worker(self):
        while True:
            try:
                item = self._queue.get()
                _item = item.copy()
                if "qt" not in _item:
                    _item['qt'] = time()
                self._failedque.append(_item)
                if self._send_data(item):
                    self._failedque.remove(_item)
                self._queue.task_done()
            except:  # pylint: disable=W0702
                pass

    def _send_data(self, data):
        if self._http_offline:
            return False
        try:
            r = self._http_session.post(
                "https://ssl.google-analytics.com/collect",
                data=data,
                headers=util.get_request_defheaders(),
                timeout=1)
            r.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            # skip Bad Request
            if 400 >= e.response.status_code < 500:
                return True
        except:  # pylint: disable=W0702
            pass
        self._http_offline = True
        return False


def on_command():
    resend_backuped_reports()

    mp = MeasurementProtocol()
    mp.send("screenview")

    if util.is_ci():
        measure_ci()


def measure_ci():
    event = {"category": "CI", "action": "NoName", "label": None}

    envmap = {
        "APPVEYOR": {
            "label": getenv("APPVEYOR_REPO_NAME")
        },
        "CIRCLECI": {
            "label":
            "%s/%s" % (getenv("CIRCLE_PROJECT_USERNAME"),
                       getenv("CIRCLE_PROJECT_REPONAME"))
        },
        "TRAVIS": {
            "label": getenv("TRAVIS_REPO_SLUG")
        },
        "SHIPPABLE": {
            "label": getenv("REPO_NAME")
        },
        "DRONE": {
            "label": getenv("DRONE_REPO_SLUG")
        }
    }

    for key, value in envmap.iteritems():
        if getenv(key, "").lower() != "true":
            continue
        event.update({"action": key, "label": value['label']})

    on_event(**event)


def on_run_environment(options, targets):
    opts = [
        "%s=%s" % (opt, value.replace("\n", ", ") if "\n" in value else value)
        for opt, value in sorted(options.items())
    ]
    targets = [t.title() for t in targets or ["run"]]
    on_event("Env", " ".join(targets), "&".join(opts))


def on_event(category, action, label=None, value=None, screen_name=None):
    mp = MeasurementProtocol()
    mp['event_category'] = category[:150]
    mp['event_action'] = action[:500]
    if label:
        mp['event_label'] = label[:500]
    if value:
        mp['event_value'] = int(value)
    if screen_name:
        mp['screen_name'] = screen_name[:2048]
    mp.send("event")


def on_exception(e):

    def _cleanup_description(text):
        text = text.replace("Traceback (most recent call last):", "")
        text = re.sub(
            r'File "([^"]+)"',
            lambda m: join(*m.group(1).split(sep)[-2:]),
            text,
            flags=re.M)
        text = re.sub(r"\s+", " ", text, flags=re.M)
        return text.strip()

    skip_conditions = [
        isinstance(e, cls)
        for cls in (IOError, exception.ReturnErrorCode,
                    exception.AbortedByUser, exception.NotGlobalLibDir,
                    exception.InternetIsOffline,
                    exception.NotPlatformIOProject,
                    exception.UserSideException)
    ]
    try:
        skip_conditions.append("[API] Account: " in str(e))
    except UnicodeEncodeError as ue:
        e = ue
    if any(skip_conditions):
        return
    is_crash = any([
        not isinstance(e, exception.PlatformioException),
        "Error" in e.__class__.__name__
    ])
    mp = MeasurementProtocol()
    description = _cleanup_description(format_exc() if is_crash else str(e))
    mp['exd'] = ("%s: %s" % (type(e).__name__, description))[:2048]
    mp['exf'] = 1 if is_crash else 0
    mp.send("exception")


@atexit.register
def _finalize():
    timeout = 1000  # msec
    elapsed = 0
    try:
        while elapsed < timeout:
            if not MPDataPusher().in_wait():
                break
            sleep(0.2)
            elapsed += 200
        backup_reports(MPDataPusher().get_items())
    except KeyboardInterrupt:
        pass


def backup_reports(items):
    if not items:
        return

    KEEP_MAX_REPORTS = 100
    tm = app.get_state_item("telemetry", {})
    if "backup" not in tm:
        tm['backup'] = []

    for params in items:
        # skip static options
        for key in params.keys():
            if key in ("v", "tid", "cid", "cd1", "cd2", "sr", "an"):
                del params[key]

        # store time in UNIX format
        if "qt" not in params:
            params['qt'] = time()
        elif not isinstance(params['qt'], float):
            params['qt'] = time() - (params['qt'] / 1000)

        tm['backup'].append(params)

    tm['backup'] = tm['backup'][KEEP_MAX_REPORTS * -1:]
    app.set_state_item("telemetry", tm)


def resend_backuped_reports():
    tm = app.get_state_item("telemetry", {})
    if "backup" not in tm or not tm['backup']:
        return False

    for report in tm['backup']:
        mp = MeasurementProtocol()
        for key, value in report.items():
            mp[key] = value
        mp.send(report['t'])

    # clean
    tm['backup'] = []
    app.set_state_item("telemetry", tm)
