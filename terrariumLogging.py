# -*- coding: utf-8 -*-
import logging
import logging.handlers
import logging.config

import zipfile
import codecs
import os
import os.path
import time
import glob
import shutil
import threading

from terrariumNotification import terrariumNotification
from terrariumUtils import terrariumUtils


class TimedCompressedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Extended version of TimedRotatingFileHandler that compress logs on rollover.
    """

    def emit(self, record) -> None:
        if len(record.args):
            record.msg = terrariumUtils.clean_log_line(record.msg % record.args)
            record.args = ()
        else:
            record.msg = terrariumUtils.clean_log_line(record.msg)

        super().emit(record)

    def doRollover(self) -> None:
        """
        do a rollover; in this case, a date/time stamp is appended to the filename
        when the rollover happens.  However, you want the file to be named for the
        start of the interval, not the current time.  If there is a backup count,
        then we have to get a list of matching filenames, sort them and remove
        the one with the oldest suffix.
        """

        def zipAction() -> None:
            # get the time that this sequence started at and make it a TimeTuple
            t = self.rolloverAt - self.interval
            timeTuple = time.localtime(t)
            dfn = self.baseFilename + "." + time.strftime(self.suffix, timeTuple)
            if os.path.exists(dfn):
                os.remove(dfn)

            self.stream.close()

            shutil.move(os.path.realpath(self.baseFilename), os.path.abspath(dfn))
            # Empty source file for new day
            open(self.baseFilename, "w").close()

            if self.encoding:
                self.stream = codecs.open(self.baseFilename, "w", "utf-8")
            else:
                self.stream = open(self.baseFilename, "w")

            self.rolloverAt = self.rolloverAt + self.interval

            if self.backupCount > 0:
                # find the oldest log file and delete it
                s = glob.glob(self.baseFilename + ".20*")
                if len(s) > self.backupCount:
                    s.sort()
                    os.remove(s[0])

            if os.path.exists(dfn + ".zip"):
                os.remove(dfn + ".zip")

            with zipfile.ZipFile(dfn + ".zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as log_archive:
                log_archive.write(dfn, os.path.basename(dfn))

            os.remove(dfn)

        compress = threading.Thread(target=zipAction)
        compress.start()


# Loggers whose lines are never turned into notification messages: terrariumNotification (it would loop),
# terrariumLogging (this handler reporting its own failure) and terrariumWeatherRetry (repeat failures of a
# retry whose first failure was already sent).
NOT_NOTIFIED = ("terrariumNotification", "terrariumLogging", "terrariumWeatherRetry")


class NotificationLogger(logging.StreamHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._reported_failures = set()

        # The notification will later on get a reference to the terrariumEngine for version and profile information
        self.notification = terrariumNotification()

    def emit(self, record) -> None:
        # Do not send messages from terrariumNotification logging, as that will trigger a recursing error.
        if record.name not in NOT_NOTIFIED and str(record.levelname.lower()) in ["warning", "error"]:
            try:
                self.notification.message(f"system_{record.levelname.lower()}", {"message": record.getMessage()})
            except Exception as ex:
                # A log call must never stop the program. Before the database mapping exists (start-up),
                # the notification lookup fails. Say so in the engine log, once per kind of failure, on a
                # logger this handler skips, so it cannot loop. The lost message itself is in the log already.
                failure = f"{type(ex).__name__}: {ex}"
                if failure not in self._reported_failures:
                    self._reported_failures.add(failure)
                    logging.getLogger("terrariumLogging").warning(
                        f"A {record.levelname.lower()} line was not sent as a notification ({failure})."
                    )


if os.path.isfile("log/logging.custom.cfg"):
    logging.config.fileConfig("log/logging.custom.cfg")
else:
    logging.config.fileConfig("logging.cfg")
