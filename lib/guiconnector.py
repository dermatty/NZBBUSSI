from threading import Thread
from .aux import PWDBSender
from statistics import mean
import inspect
import zmq
import os
import time
import re
import shutil


lpref = __name__.split("lib.")[-1] + " - "


def whoami():
    outer_func_name = str(inspect.getouterframes(inspect.currentframe())[1].function)
    outer_func_linenr = str(inspect.currentframe().f_back.f_lineno)
    lpref = __name__.split("lib.")[-1] + " - "
    return lpref + outer_func_name + " / #" + outer_func_linenr + ": "


def remove_nzb_files_and_db(deleted_nzb_name0, dirs, pwdb, logger):
    nzbdirname = re.sub(r"[.]nzb$", "", deleted_nzb_name0, flags=re.IGNORECASE) + "/"
    # delete nzb from .ginzibix/nzb0
    try:
        os.remove(dirs["nzb"] + deleted_nzb_name0)
        logger.debug(whoami() + ": deleted NZB " + deleted_nzb_name0 + " from NZB dir")
    except Exception as e:
        logger.debug(whoami() + str(e))
    # remove from db
    pwdb.exc("db_nzb_delete", [deleted_nzb_name0], {})
    # pwdb.db_nzb_delete(deleted_nzb_name0)
    # remove incomplete/$nzb_name
    try:
        shutil.rmtree(dirs["incomplete"] + nzbdirname)
        logger.debug(whoami() + ": deleted incomplete dir for " + deleted_nzb_name0)
    except Exception as e:
        logger.debug(whoami() + str(e))


class GUI_Connector(Thread):
    def __init__(self, lock, dirs, logger, cfg):
        Thread.__init__(self)
        self.daemon = True
        self.dirs = dirs
        self.pwdb = PWDBSender()
        self.cfg = cfg
        try:
            self.port = self.cfg["OPTIONS"]["PORT"]
            assert(int(self.port) > 1024 and int(self.port) <= 65535)
        except Exception as e:
            self.logger.debug(whoami() + str(e) + ", setting port to default 36603")
            self.port = "36603"
        self.data = None
        self.nzbname = None
        self.pwdb_msg = (None, None)
        self.logger = logger
        self.lock = lock
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind("tcp://*:" + self.port)
        # self.socket.setsockopt(zmq.RCVTIMEO, 2000)
        self.threads = []
        self.server_config = None
        self.dl_running = True
        self.status = "idle"
        self.first_has_changed = False
        self.deleted_nzb_name = None
        self.old_t = 0
        self.oldbytes0 = 0
        self.send_data = True
        self.sorted_nzbs = None
        self.sorted_nzbshistory = None
        self.dlconfig = None
        self.netstatlist = []
        self.last_update_for_gui = 0
        self.closeall = False

    def set_health(self, article_health, connection_health):
        with self.lock:
            self.article_health = article_health
            self.connection_health = connection_health

    def set_data(self, data, threads, server_config, status, dlconfig):
        with self.lock:
            if data:
                bytescount00, availmem00, avgmiblist00, filetypecounter00, nzbname, article_health, overall_size, already_downloaded_size = data
                self.data = data
                self.nzbname = nzbname
                # self.pwdb_msg = self.pwdb.db_msg_get(nzbname)
                self.pwdb_msg = self.pwdb.exc("db_msg_get", [nzbname], {})
                self.server_config = server_config
                self.status = status
                self.dlconfig = dlconfig
                self.threads = []
                for k, (t, last_timestamp) in enumerate(threads):
                    append_tuple = (t.bytesdownloaded, t.last_timestamp, t.idn, t.bandwidth_bytes)
                    self.threads.append(append_tuple)
                self.send_data = True
            else:
                self.send_data = False

    def get_netstat(self):
        pid = os.getpid()
        with open("/proc/" + str(pid) + "/net/netstat", "r") as f:
            bytes0 = None
            for line in f:
                if line.startswith("IpExt"):
                    line0 = line.split(" ")
                    try:
                        bytes0 = int(line0[7])
                        break
                    except Exception:
                        pass
        if bytes0:
            dt = time.time() - self.old_t
            if dt == 0:
                dt = 0.001
            self.old_t = time.time()
            mbitcurr = ((bytes0 - self.oldbytes0) / dt) / (1024 * 1024) * 8
            self.oldbytes0 = bytes0
            self.netstatlist.append(mbitcurr)
            if len(self.netstatlist) > 4:
                del self.netstatlist[0]
            return mean(self.netstatlist)
        else:
            return 0

    def get_data(self):
        ret0 = (None, None, None, None, None, None, None, None, None, None, None, None, None)
        with self.lock:
            lastt = self.pwdb.exc("get_last_update_for_gui", [], {})
        if lastt > self.last_update_for_gui:
            self.send_data = True
            with self.lock:
                full_data_for_gui = self.pwdb.exc("get_all_data_for_gui", [], {})
            self.last_update_for_gui = lastt
        else:
            full_data_for_gui = None
        with self.lock:
            self.sorted_nzbs, self.sorted_nzbshistory = self.pwdb.exc("get_stored_sorted_nzbs", [], {})
        if self.send_data:
            with self.lock:
                try:
                    ret0 = (self.data, self.pwdb_msg, self.server_config, self.threads, self.dl_running, self.status,
                            self.get_netstat(), self.sorted_nzbs, self.sorted_nzbshistory, self.article_health, self.connection_health,
                            self.dlconfig, full_data_for_gui)
                except Exception as e:
                    self.logger.warning(whoami() + str(e))
        return ret0

    def has_first_nzb_changed(self):
        res = self.first_has_changed
        self.first_has_changed = False
        return res

    def has_nzb_been_deleted(self, delete=False):
        res = self.deleted_nzb_name
        if delete:
            self.deleted_nzb_name = None
        return res

    def run(self):
        while True:
            try:
                msg, datarec = self.socket.recv_pyobj()
            except Exception as e:
                self.logger.error(whoami() + str(e))
                try:
                    self.socket.send_pyobj(("NOOK", None))
                except Exception as e:
                    self.logger.error(whoami() + str(e))
            if msg == "REQ":
                getdata = self.get_data()
                gd1, _, _, _, _, _, _, sortednzbs, _, _, _, _, _ = getdata
                if gd1:
                    sendtuple = ("DL_DATA", getdata)
                else:
                    sendtuple = ("NOOK", getdata)
                try:
                    self.socket.send_pyobj(sendtuple)
                except Exception as e:
                    self.logger.error(whoami() + str(e))
            elif msg == "SET_CLOSEALL":
                try:
                    self.socket.send_pyobj(("SET_CLOSE_OK", None))
                    with self.lock:
                        self.closeall = True
                except Exception as e:
                    self.logger.error(whoami() + str(e))
                continue
            elif msg == "SET_PAUSE":     # pause downloads
                try:
                    self.socket.send_pyobj(("SET_PAUSE_OK", None))
                    with self.lock:
                        self.dl_running = False
                except Exception as e:
                    self.logger.error(whoami() + str(e))
                continue
            elif msg == "SET_RESUME":    # resume downloads
                try:
                    self.socket.send_pyobj(("SET_RESUME_OK", None))
                    self.dl_running = True
                except Exception as e:
                    self.logger.error(whoami() + str(e))
                continue
            elif msg == "SET_DELETE":
                try:
                    self.socket.send_pyobj(("SET_DELETE_OK", None))
                    # first_has_changed0, deleted_nzb_name0 = self.pwdb.set_nzbs_prios(datarec, delete=True)
                    with self.lock:
                        first_has_changed0, deleted_nzb_name0 = self.pwdb.exc("set_nzbs_prios", [datarec], {"delete": True})
                    if deleted_nzb_name0 and not first_has_changed0:
                        with self.lock:
                            remove_nzb_files_and_db(deleted_nzb_name0, self.dirs, self.pwdb, self.logger)
                except Exception as e:
                    self.logger.error(whoami() + str(e))
                if first_has_changed0:
                    self.first_has_changed = first_has_changed0
                    self.deleted_nzb_name = deleted_nzb_name0
                continue
            elif msg == "SET_NZB_ORDER":
                try:
                    self.socket.send_pyobj(("SET_NZBORDER_OK", None))
                    with self.lock:
                        self.first_has_changed, _ = self.pwdb.exc("set_nzbs_prios", [datarec], {"delete": False})
                    # self.first_has_changed, _ = self.pwdb.set_nzbs_prios(datarec, delete=False)
                except Exception as e:
                    self.logger.error(whoami() + str(e))
                continue
            else:
                try:
                    self.socket.send_pyobj(("NOOK", None))
                except Exception as e:
                    self.logger.debug(whoami() + str(e) + ", received msg: " + str(msg))
                continue