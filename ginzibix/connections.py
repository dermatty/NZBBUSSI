import nntplib
import time
from .server import Servers
from threading import Thread
import socket
import queue
from .mplogging import whoami
import copy


# This is the thread worker per connection to NNTP server
class ConnectionWorker(Thread):
    def __init__(self, connection, articlequeue, resultqueue, servers, cfg, logger):
        Thread.__init__(self)
        self.daemon = True
        self.logger = logger
        self.connection = connection
        self.articlequeue = articlequeue
        self.resultqueue = resultqueue
        self.servers = servers
        self.nntpobj = None
        self.running = True
        self.name, self.conn_nr = self.connection
        self.idn = self.name + " #" + str(self.conn_nr)
        self.bytesdownloaded = 0
        self.last_timestamp = 0
        self.mode = "download"
        self.download_done = True
        self.bandwidth_bytes = 0
        self.last_downloaded_ts = None
        self.paused = False
        self.tt_pause_started = None
        # 0 ... not running
        # 1 ... running ok
        # -1 ... connection problem
        self.connectionstate = 0
        try:
            self.connection_idle_time = int(cfg["OPTIONS"]["CONNECTION_IDLE_TIMEOUT"])
        except Exception:
            self.connection_idle_time = 45

    def stop(self):
        self.running = False

    # return status, info
    #        status = 1:  ok
    #                 0:  article not found
    #                -1:  retention not sufficient
    #                -2:  server connection error
    def download_article(self, article_name, article_age):
        bytesdownloaded = 0
        info0 = None
        if self.mode == "sanitycheck":
            try:
                resp, number, message_id = self.nntpobj.stat(article_name)
                if article_name != message_id:
                    status = -1
                else:
                    status = 1
            except Exception as e:
                self.logger.error(whoami() + str(e) + self.idn + " for article " + article_name)
                status = -1
            return status, 0, 0
        if self.server_retention < article_age * 0.95:
            self.logger.warning(whoami() + "Retention on " + self.server_name + " not sufficient for article " + article_name)
            return -1, 0, None
        try:
            resp, info = self.nntpobj.body(article_name)
            if resp.startswith("222"):
                status = 1
                info0 = [inf + b"\r\n" if not inf.endswith(b"\r\n") else inf for inf in info.lines]
                bytesdownloaded = sum(len(i) for i in info0)
            else:
                self.logger.warning(whoami() + resp + ": could not find " + article_name + " on " + self.idn)
                status = 0
        # nntpError 4xx - Command was syntactically correct but failed for some reason
        except nntplib.NNTPTemporaryError as e:
            errcode = e.response.strip()[:3]
            if errcode == "400":
                # server quits, new connection has to be established
                status = -2
            else:
                status = 0
            self.logger.warning(whoami() + e.response + ": could not find " + article_name + " on " + self.idn)
        # nntpError 5xx - Command unknown error
        except nntplib.NNTPPermanentError as e:
            errcode = e.response.strip()[:3]
            if errcode in ["503", "502"]:
                # timeout, closing connection
                status = -2
            else:
                status = 0
            self.logger.warning(whoami() + e.response + ": could not find " + article_name + " on " + self.idn)
            status = 0
        except nntplib.NNTPError as e:
            status = 0
            self.logger.warning(whoami() + e.response + ": could not find " + article_name + " on " + self.idn)
        except KeyboardInterrupt:
            status = -3
        except socket.timeout:
            status = -2
            self.logger.warning(whoami() + "socket.timeout on " + self.idn)
        except AttributeError as e:
            status = 0
            self.logger.warning(whoami() + str(e) + ": " + article_name + " on " + self.idn)
        except Exception as e:
            status = 0
            self.logger.warning(whoami() + str(e) + ": " + article_name + " on " + self.idn)
        self.bandwidth_bytes += bytesdownloaded
        return status, bytesdownloaded, info0

    def wait_running(self, sec):
        tt0 = time.time()
        while time.time() - tt0 < sec and self.running:
            time.sleep(0.1)
        return

    def retry_connect(self):
        idx = 0
        self.logger.debug(whoami() + "Server " + self.idn + " connecting ...")
        while idx < 5 and self.running:
            self.nntpobj = self.servers.open_connection(self.name, self.conn_nr)
            if self.nntpobj:
                self.logger.debug(whoami() + "Server " + self.idn + " connected!")
                self.last_timestamp = time.time()
                self.connectionstate = 1
                self.server_name, self.server_url, self.server_user, self.server_password, self.server_port,\
                    self.server_usessl, self.server_level, self.server_connections, self.server_retention,\
                    self.useserver = self.servers.get_single_server_config(self.connection[0])
                self.wait_running(1)
                return
            self.logger.warning(whoami() + "Could not connect to server " + self.idn + ", will retry in 5 sec.")
            self.wait_running(2)
            if not self.running:
                break
            idx += 1
        if not self.running:
            self.logger.warning(whoami() + "No connection retries anymore due to exiting")
        else:
            self.logger.error(whoami() + "Connect retries to " + self.idn + " failed!")
            self.connectionstate = -1

    def remove_from_remaining_servers(self, name, remaining_servers):
        next_servers = []
        for s in remaining_servers:
            addserver = s[:]
            try:
                addserver.remove(name)
            except Exception:
                pass
            if addserver:
                next_servers.append(addserver)
        return next_servers

    def is_download_done(self):
        return self.download_done

    def run(self):
        self.logger.info(whoami() + self.idn + " thread starting !")
        timeout = 2
        self.tt_pause_started = None
        while self.running:
            self.download_done = True
            if self.paused:
                if not self.tt_pause_started:
                    self.tt_pause_started = time.time()
                elif time.time() - self.tt_pause_started > self.connection_idle_time and self.nntpobj:
                    if self.servers.close_connection(self.name, self.conn_nr):
                        self.logger.info(whoami() + self.idn + " connection idle, closed!")
                        self.nntpobj = None
                        self.connectionstate = -1
                    else:
                        self.logger.info(whoami() + self.idn + " connection non existent, closed!")
                        self.nntpobj = None
                        self.connectionstate = -1
                time.sleep(0.25)
                continue
            else:
                self.tt_pause_started = None
            if not self.running:
                break
            # articlequeue = (filename, age, filetype, nr_articles, art_nr, art_name, level_servers)
            try:
                article = self.articlequeue.pop()
            except (queue.Empty, EOFError, IndexError):
                time.sleep(0.1)
                continue
            except Exception as e:
                self.logger.warning(whoami() + str(e) + ": problem in clearing article queue")
                time.sleep(0.1)
                continue
            # avoid ctrl-c to interrup downloading itself
            self.download_done = False
            if not self.nntpobj:
                self.retry_connect()
            filename, age, filetype, nr_articles, art_nr, art_name, remaining_servers1 = article
            if self.name not in remaining_servers1[0] or not self.nntpobj:
                self.articlequeue.append((filename, age, filetype, nr_articles, art_nr, art_name, remaining_servers1))
                time.sleep(0.1)
                continue
            if not remaining_servers1:
                self.resultqueue.append(article + (None,))
                continue
            if not self.nntpobj:
                self.wait_running(3)
                continue
            status, bytesdownloaded, info = self.download_article(art_name, age)
            # if ctrl-c - exit thread
            if status == -3 or not self.running:
                break
            # if download successfull - put to resultqueue
            elif status == 1:
                self.last_downloaded_ts = time.time()
                # self.logger.debug(whoami() + "Downloaded article " + art_name + " on server " + self.idn)
                timeout = 2
                self.bytesdownloaded += bytesdownloaded
                self.resultqueue.append((filename, age, filetype, nr_articles, art_nr, art_name, self.name, info, True))
                # self.articlequeue.task_done()
            # if 400 error
            elif status == -2:
                # disconnect
                self.logger.warning(whoami() + self.idn + " server connection error, reconnecting ...")
                self.connectionstate = -1
                try:
                    name, conn_nr = self.connection
                    if self.servers.close_connection(name, conn_nr):
                        self.nntpobj = None
                except Exception:
                    pass
                self.nntpobj = None
                # take next server
                next_servers = self.remove_from_remaining_servers(self.name, remaining_servers1)
                next_servers.append([self.name])    # add current server to end of list
                self.logger.debug(whoami() + "Requeuing " + art_name + " on server " + self.idn)
                # requeue
                self.articlequeue.append((filename, age, filetype, nr_articles, art_nr, art_name, next_servers))
                self.wait_running(timeout)
                timeout *= 2
                if timeout > 30:
                    timeout = 2
                continue
            # if article could not be found on server / retention not good enough - requeue to other server
            elif status in [0, -1]:
                timeout = 2
                next_servers = self.remove_from_remaining_servers(self.name, remaining_servers1)
                if not next_servers:
                    self.logger.error(whoami() + "Download finally failed on server " + self.idn + ": for article " + art_name + " " + str(next_servers))
                    self.resultqueue.append((filename, age, filetype, nr_articles, art_nr, art_name, [], "failed", True))
                else:
                    self.logger.debug(whoami() + "Download failed on server " + self.idn + ": for article " + art_name + ", queueing: "
                                      + str(next_servers))
                    self.articlequeue.append((filename, age, filetype, nr_articles, art_nr, art_name, next_servers))
        self.logger.info(whoami() + self.idn + " exited!")


# this class deals on a meta-level with usenet connections
class ConnectionThreads:
    def __init__(self, cfg, articlequeue, resultqueue, server_ts, logger):
        self.cfg = cfg
        self.logger = logger
        self.threads = []
        self.articlequeue = articlequeue
        self.resultqueue = resultqueue
        self.servers = None
        self.bdl_results = {}
        for s in server_ts:
            try:
                self.bdl_results[s] = server_ts[s]["sec"].max() * (1024 * 1024)
            except Exception:
                self.bdl_results[s] = 0

    def init_servers(self):
        self.servers = Servers(self.cfg, self.logger)
        self.level_servers = self.servers.level_servers
        self.all_connections = self.servers.all_connections

    def get_downloaded_per_server(self):
        result = {}
        try:
            result["-ALL SERVERS-"] = self.bdl_results["-ALL SERVERS-"]
        except Exception:
            result["-ALL SERVERS-"] = 0
        if not self.servers:
            return result
        for servername, _, _, _, _, _, _, _, _, useserver in self.servers.server_config:
            if useserver:
                bdl = sum([t.bytesdownloaded for t, _ in self.threads if t.name == servername])
                result["-ALL SERVERS-"] += bdl
                try:
                    result[servername] = self.bdl_results[servername] + bdl
                except Exception:
                    result[servername] = bdl
        return result

    def start_threads(self):
        if not self.threads:
            self.logger.debug(whoami() + "starting download threads")
            self.init_servers()
            for sn, scon, _, _ in self.all_connections:
                t = ConnectionWorker((sn, scon), self.articlequeue, self.resultqueue, self.servers,
                                     self.cfg, self.logger)
                self.threads.append((t, time.time()))
                t.start()
        else:
            self.logger.debug(whoami() + "threads already started")

    def pause_threads(self):
        if self.threads:
            for t, _ in self.threads:
                t.paused = True
            # wait until all threads are really in pause loop
            while True:
                all_paused = True
                for t, _ in self.threads:
                    if not t.tt_pause_started:
                        all_paused = False
                        break
                if all_paused:
                    break
                time.sleep(0.1)

    def resume_threads(self):
        if self.threads:
            self.logger.debug(whoami() + "Resuming threads")
            for t, _ in self.threads:
                t.paused = False
        else:
            self.logger.debug(whoami() + "Starting threads")
            self.start_threads()

    def stop_threads(self):
        if not self.threads:
            self.logger.debug(whoami() + "no threads running, exiting ...")
        try:
            self.logger.debug(whoami() + "stopping download threads + servers")
            for t, _ in self.threads:
                t.stop()
                t.last_downloaded_ts = None
            for t, _ in self.threads:
                t.join()
            del self.threads
            self.threads = []
            if self.servers:
                self.servers.close_all_connections()
                del self.servers
                self.servers = None
        except Exception as e:
            self.logger.warning(whoami() + str(e))

    def reset_timestamps(self):
        for t, _ in self.threads:
            t.last_timestamp = time.time()

    def reset_timestamps_bdl(self):
        if self.threads:
            for t, _ in self.threads:
                t.bytesdownloaded = 0
                t.last_timestamp = 0
                t.bandwidth_bytes = 0
                t.bandwidth_lasttt = 0


