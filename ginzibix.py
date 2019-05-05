#!/home/stephan/.virtualenvs/gzbx/bin/python

import configparser
import signal
import multiprocessing as mp
import logging
import logging.handlers
import lib
import gi
import sys
import os
from lib import is_port_in_use
from setproctitle import setproctitle
gi.require_version('Gtk', '3.0')


__version__ = "0.1-alpha"


# Signal handler
class SigHandler_Ginzibix:
    def __init__(self, pwdb, mpp_main, mpp_wrapper, mp_loggerqueue, mp_loglistener, logger):
        self.mpp_main = mpp_main
        self.mpp_wrapper = mpp_wrapper
        self.logger = logger
        self.pwdb = pwdb
        self.mp_loggerqueue = mp_loggerqueue
        self.mp_loglistener = mp_loglistener

    def sighandler_ginzibix(self, a, b):
        self.shutdown()

    def shutdown(self):
        # wait until main is joined
        if self.mpp_main:
            if self.mpp_main.pid:
                try:
                    # os.kill(self.mpp_main.pid, signal.SIGTERM)
                    self.mpp_main.join(timeout=5)
                    os.kill(self.mpp_main.pid, signal.SIGTERM)
                except Exception as e:
                    logger.warning(lib.whoami() + str(e))
        self.pwdb.exc("set_exit_goodbye_from_main", [], {})
        try:
            self.mpp_wrapper.join(timeout=5)
            os.kill(self.mpp_wrapper.pid, signal.SIGTERM)
        except Exception as e:
            logger.warning(lib.whoami() + str(e))
        lib.stop_logging_listener(self.mp_loggerqueue, self.mp_loglistener)
        try:
            self.mp_loglistener.join(timeout=5)
            os.kill(self.mp_loglistener.pid, signal.SIGTERM)
        except Exception as e:
            logger.warning(lib.whoami() + str(e))


# -------------------- main --------------------
if __name__ == '__main__':
    setproctitle("gzbx." + os.path.basename(__file__))

    guiport = 36703
    while is_port_in_use(guiport):
        guiport += 1

    exit_status = 3

    while exit_status == 3:

        # dirs
        userhome, maindir, dirs, subdirs = lib.make_dirs()

        # init config
        try:
            cfg_file = dirs["config"] + "/ginzibix.config"
            cfg = configparser.ConfigParser()
            cfg.read(cfg_file)
        except Exception as e:
            print(str(e) + ": config file syntax error, exiting")
            sys.exit()

        # get log level
        try:
            loglevel_str = cfg["OPTIONS"]["debuglevel"].lower()
            if loglevel_str == "info":
                loglevel = logging.INFO
            elif loglevel_str == "debug":
                loglevel = logging.DEBUG
            elif loglevel_str == "warning":
                loglevel = logging.WARNING
            elif loglevel_str == "error":
                loglevel = logging.ERROR
            else:
                loglevel = logging.INFO
                loglevel_str = "info"
        except Exception:
            loglevel = logging.INFO
            loglevel_str = "info"

        # init logger
        mp_loggerqueue, mp_loglistener = lib.start_logging_listener("/home/stephan/.ginzibix/logs/ginzibix.log", maxlevel=loglevel)
        logger = lib.setup_logger(mp_loggerqueue, __file__)

        logger.debug(lib.whoami() + "starting with loglevel '" + loglevel_str + "'")

        progstr = "ginzibix 0.1-alpha, client"
        logger.debug(lib.whoami() + "Welcome to GINZIBIX " + __version__)

        # start DB Thread
        mpp_wrapper = mp.Process(target=lib.wrapper_main, args=(cfg, dirs, mp_loggerqueue, ))
        mpp_wrapper.start()

        pwdb = lib.PWDBSender()

        # start main mp
        mpp_main = None
        mpp_main = mp.Process(target=lib.ginzi_main, args=(cfg_file, cfg, dirs, subdirs, guiport, mp_loggerqueue, ))
        mpp_main.start()

        # init sighandler
        sh = SigHandler_Ginzibix(pwdb, mpp_main, mpp_wrapper, mp_loggerqueue, mp_loglistener, logger)
        signal.signal(signal.SIGINT, sh.sighandler_ginzibix)
        signal.signal(signal.SIGTERM, sh.sighandler_ginzibix)

        app = lib.ApplicationGui(dirs, cfg, guiport, mp_loggerqueue)
        exit_status = app.run(sys.argv)

        sh.shutdown()

    sys.exit()

