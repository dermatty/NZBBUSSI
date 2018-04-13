#!/home/stephan/.virtualenvs/nntp/bin/python

import threading
from threading import Thread
import time
import sys
import os
import queue
from os.path import expanduser
import configparser
import signal
import glob
import xml.etree.ElementTree as ET
import nntplib
import ssl
import yenc
import multiprocessing as mp
import logging
import logging.handlers
import psutil
import re
import hashlib
import pymongo
import lib

userhome = expanduser("~")
maindir = userhome + "/.ginzibix/"
dirs = {
    "userhome": userhome,
    "main": maindir,
    "config": maindir + "config/",
    "nzb": maindir + "nzb/",
    "complete": maindir + "complete/",
    "incomplete": maindir + "incomplete/",
    "logs": maindir + "logs/"
}
subdirs = {
    "download": "_downloaded0",
    "renamed": "_renamed0",
    "unpacked": "_unpack0",
    "verififiedrar": "_verifiedrars0"
}
_ftypes = ["etc", "rar", "sfv", "par2", "par2vol"]

# init logger
logger = logging.getLogger("ginzibix")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(dirs["logs"] + "ginzibix.log", mode="w")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
logger.addHandler(fh)


# -------------------- Classes --------------------

# captures SIGINT / SIGTERM and closes down everything
class SigHandler():

    def __init__(self, servers, threads, pwdb, mpp_nzbparser, mpp_decoder, logger):
        self.logger = logger
        self.servers = servers
        self.threads = threads
        self.mpp_nzbparser = mpp_nzbparser
        self.mpp_decoder = mpp_decoder
        self.pwdb = pwdb
        self.signal = False

    def shutdown(self):
        # stop nzb_parser
        self.logger.warning("signalhandler: terminating nzb_parser")
        self.mpp_nzbparser.terminate()
        self.mpp_nzbparser.join()
        # stop article decoder
        self.logger.warning("signalhandler: terminating article_decoder")
        self.mpp_decoder.terminate()
        self.mpp_decoder.join()
        # stop pwdb
        self.logger.warning("signalhandler: closing pewee.db")
        self.pwdb.db_close()
        # threads + servers
        self.logger.warning("signalhandler: stopping download threads")
        for t, _ in self.threads:
            t.stop()
            t.join()
        self.logger.warning("signalhandler: closing all server connections")
        self.servers.close_all_connections()
        self.logger.warning("signalhandler: exiting")
        sys.exit()

    def signalhandler(self, signal, frame):
        self.shutdown()


# Does all the server stuff (open, close connctions) and contains all relevant
# data
class Servers():

    def __init__(self, cfg):
        self.cfg = cfg
        # server_config = [(server_name, server_url, user, password, port, usessl, level, connections, retention)]
        self.server_config = self.get_server_config(self.cfg)
        # all_connections = [(server_name, conn#, retention, nntp_obj)]
        self.all_connections = self.get_all_connections()
        # level_servers0 = {"0": ["EWEKA", "BULK"], "1": ["TWEAK"], "2": ["NEWS", "BALD"]}
        self.level_servers = self.get_level_servers()

    def __bool__(self):
        if not self.server_config:
            return False
        return True

    def get_single_server_config(self, server_name0):
        for server_name, server_url, user, password, port, usessl, level, connections, retention in self.server_config:
            if server_name == server_name0:
                return server_name, server_url, user, password, port, usessl, level, connections, retention
        return None

    def get_all_connections(self):
        conn = []
        for s_name, _, _, _, _, _, _, s_connections, s_retention in self.server_config:
            for c in range(s_connections):
                conn.append((s_name, c + 1, s_retention, None))
        return conn

    def get_level_servers(self):
        s_tuples = []
        for s_name, _, _, _, _, _, s_level, _, _ in self.server_config:
            s_tuples.append((s_name, s_level))
        sorted_s_tuples = sorted(s_tuples, key=lambda server: server[1])
        ls = {}
        for s_name, s_level in sorted_s_tuples:
            if str(s_level) not in ls:
                ls[str(s_level)] = []
            ls[str(s_level)].append(s_name)
        return ls

    def open_connection(self, server_name0, conn_nr):
        result = None
        for idx, (sn, cn, rt, nobj) in enumerate(self.all_connections):
            if sn == server_name0 and cn == conn_nr:
                if nobj:
                    return nobj
                else:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS)
                    sc = self.get_single_server_config(server_name0)
                    if sc:
                        server_name, server_url, user, password, port, usessl, level, connections, retention = self.get_single_server_config(server_name0)
                        try:
                            logger.info("Opening connection # " + str(conn_nr) + "to server " + server_name)
                            if usessl:
                                nntpobj = nntplib.NNTP_SSL(server_url, user=user, password=password, ssl_context=context, port=port, readermode=True, timeout=5)
                            else:
                                nntpobj = nntplib.NNTP(server_url, user=user, password=password, ssl_context=context, port=port, readermode=True, timeout=5)
                            logger.info("Opened Connection #" + str(conn_nr) + " on server " + server_name0)
                            result = nntpobj
                            self.all_connections[idx] = (sn, cn, rt, nntpobj)
                            break
                        except Exception as e:
                            logger.error("Server " + server_name0 + " connect error: " + str(e))
                            self.all_connections[idx] = (sn, cn, rt, None)
                            break
                    else:
                        logger.error("Cannot get server config for server: " + server_name0)
                        self.all_connections[idx] = (sn, cn, rt, None)
                        break
        return result

    def close_all_connections(self):
        for (sn, cn, _, nobj) in self.all_connections:
            if nobj:
                try:
                    nobj.quit()
                    logger.warning("Closed connection #" + str(cn) + " on " + sn)
                except Exception as e:
                    logger.warning("Cannot quit server " + sn + ": " + str(e))

    def get_server_config(self, cfg):
        # get servers from config
        snr = 0
        sconf = []
        while True:
            try:
                snr += 1
                snrstr = "SERVER" + str(snr)
                server_name = cfg[snrstr]["SERVER_NAME"]
                server_url = cfg[snrstr]["SERVER_URL"]
                user = cfg[snrstr]["USER"]
                password = cfg[snrstr]["PASSWORD"]
                port = int(cfg[snrstr]["PORT"])
                usessl = True if cfg[snrstr]["SSL"].lower() == "yes" else False
                level = int(cfg[snrstr]["LEVEL"])
                connections = int(cfg[snrstr]["CONNECTIONS"])
            except Exception as e:
                snr -= 1
                break
            try:
                retention = int(cfg[snrstr]["RETENTION"])
                sconf.append((server_name, server_url, user, password, port, usessl, level, connections, retention))
            except Exception as e:
                sconf.append((server_name, server_url, user, password, port, usessl, level, connections, 999999))
        if not sconf:
            return None
        return sconf


# This is the thread worker per connection to NNTP server
class ConnectionWorker(Thread):
    def __init__(self, lock, connection, articlequeue, resultqueue, servers):
        Thread.__init__(self)
        # self.daemon = True
        self.connection = connection
        self.articlequeue = articlequeue
        self.resultqueue = resultqueue
        self.lock = lock
        self.servers = servers
        self.nntpobj = None
        self.running = True
        self.name, self.conn_nr = self.connection
        self.idn = self.name + " #" + str(self.conn_nr)
        self.bytesdownloaded = 0
        self.last_timestamp = 0

    def stop(self):
        self.running = False

    # return status, info
    #        status = 1:  ok
    #                 0:  article not found
    #                -1:  retention not sufficient
    #                -2:  server connection error
    def download_article(self, article_name, article_age):
        sn, _ = self.connection
        bytesdownloaded = 0
        server_name, server_url, user, password, port, usessl, level, connections, retention = self.servers.get_single_server_config(sn)
        if retention < article_age * 0.95:
            logger.warning("Retention on " + server_name + " not sufficient for article " + article_name + ", return status = -1")
            return -1, None
        try:
            # resp_h, info_h = self.nntpobj.head(article_name)
            resp, info = self.nntpobj.body(article_name)
            if resp[:3] != "222":
                # if resp_h[:3] != "221" or resp[:3] != "222":
                logger.warning("Could not find " + article_name + "on " + self.idn + ", return status = 0")
                status = 0
                info0 = None
            else:
                status = 1
                info0 = [inf for inf in info.lines]
                bytesdownloaded = sum(len(i) for i in info0)
        except Exception as e:
            logger.error(str(e) + self.idn + " for article " + article_name + ", return status = -2")
            status = -2
            info0 = None
        return status, bytesdownloaded, info0

    def retry_connect(self):
        if self.nntpobj or not self.running:
            return
        idx = 0
        name, conn_nr = self.connection
        idn = name + " #" + str(conn_nr)
        logger.info("Server " + idn + " connecting ...")
        while idx < 5 and self.running:
            self.nntpobj = self.servers.open_connection(name, conn_nr)
            if self.nntpobj:
                logger.info("Server " + idn + " connected!")
                self.last_timestamp = time.time()
                return
            logger.warning("Could not connect to server " + idn + ", will retry in 5 sec.")
            time.sleep(5)
            idx += 1
        if not self.running:
            logger.warning("No connection retries anymore due to exiting")
        else:
            logger.error("Connect retries to " + idn + " failed!")

    def remove_from_remaining_servers(self, name, remaining_servers):
        next_servers = []
        for s in remaining_servers:
            addserver = s[:]
            try:
                addserver.remove(name)
            except Exception as e:
                pass
            if addserver:
                next_servers.append(addserver)
        return next_servers

    def run(self):
        logger.info(self.idn + " thread starting !")
        while True and self.running:
            self.retry_connect()
            if not self.nntpobj:
                time.sleep(5)
                continue
            self.lock.acquire()
            artlist = list(self.articlequeue.queue)
            try:
                test_article = artlist[-1]
            except IndexError:
                self.lock.release()
                time.sleep(0.1)
                continue
            if not test_article:
                article = self.articlequeue.get()
                self.articlequeue.task_done()
                self.lock.release()
                logger.warning(self.idn + ": got poison pill!")
                break
            _, _, _, _, _, _, remaining_servers = test_article
            # no servers left
            # articlequeue = (filename, age, filetype, nr_articles, art_nr, art_name, level_servers)
            if not remaining_servers:
                article = self.articlequeue.get()
                self.lock.release()
                self.resultqueue.put(article + (None,))
                continue
            if self.name not in remaining_servers[0]:
                self.lock.release()
                time.sleep(0.1)
                continue
            article = self.articlequeue.get()
            self.lock.release()
            filename, age, filetype, nr_articles, art_nr, art_name, remaining_servers1 = article
            # print("Downloading on server " + idn + ": + for article #" + str(art_nr), filename)
            status, bytesdownloaded, info = self.download_article(art_name, age)
            # if server connection error - disconnect
            if status == -2:
                # disconnect
                logger.warning("Stopping server " + self.idn)
                try:
                    self.nntpobj.quit()
                except Exception as e:
                    pass
                self.nntpobj = None
                # take next server
                next_servers = self.remove_from_remaining_servers(self.name, remaining_servers)
                next_servers.append([self.name])    # add current server to end of list
                logger.warning("Requeuing " + art_name + " on server " + self.idn)
                # requeue
                self.articlequeue.task_done()
                self.articlequeue.put((filename, age, filetype, nr_articles, art_nr, art_name, next_servers))
                time.sleep(10)
                continue
            # if download successfull - put to resultqueue
            if status == 1:
                self.bytesdownloaded += bytesdownloaded
                # print("Download success on server " + idn + ": for article #" + str(art_nr), filename)
                self.resultqueue.put((filename, age, filetype, nr_articles, art_nr, art_name, self.name, info))
                self.articlequeue.task_done()
            # if article could not be found on server / retention not good enough - requeue to other server
            if status in [0, -1]:
                next_servers = self.remove_from_remaining_servers(self.name, remaining_servers)
                self.articlequeue.task_done()
                if not next_servers:
                    logger.error("Download finally failed on server " + self.idn + ": for article #" + str(art_nr), next_servers)
                    self.resultqueue.put((filename, age, filetype, nr_articles, art_nr, art_name, [], "failed"))
                else:
                    logger.warning("Download failed on server " + self.idn + ": for article #" + str(art_nr) + ", queueing: ", next_servers)
                    self.articlequeue.put((filename, age, filetype, nr_articles, art_nr, art_name, next_servers))
        logger.info(self.idn + " exited!")


# Handles download of a NZB file
class Downloader():
    def __init__(self, servers, dirs, pwdb, logger):
        self.pwdb = pwdb
        self.servers = servers
        self.lock = threading.Lock()
        self.level_servers = self.servers.level_servers
        self.all_connections = self.servers.all_connections
        self.articlequeue = queue.LifoQueue()
        self.resultqueue = queue.Queue()
        self.mp_work_queue = mp.Queue()
        self.mp_result_queue = mp.Queue()
        self.mp_parverify_outqueue = mp.Queue()
        self.mp_parverify_inqueue = mp.Queue()
        self.mp_unrarqueue = mp.Queue()
        self.mp_nzbparser_outqueue = mp.Queue()
        self.mp_nzbparser_inqueue = mp.Queue()
        self.threads = []
        self.dirs = dirs
        self.logger = logger

        try:
            if not os.path.isdir(self.main_dir):
                os.mkdir(self.main_dir)
            if not os.path.isdir(self.unpack_dir):
                os.mkdir(self.unpack_dir)
            if not os.path.isdir(self.verifiedrar_dir):
                os.mkdir(self.verifiedrar_dir)
            if not os.path.isdir(self.download_dir):
                os.mkdir(self.download_dir)
            if not os.path.isdir(self.rename_dir):
                os.mkdir(self.rename_dir)
        except Exception as e:
            logger.error(str(e) + " in creating dirs ...")

    def make_dirs(self, nzb):
        self.nzb = nzb
        self.nzbdir = re.sub(r"[.]nzb$", "", self.nzb, flags=re.IGNORECASE) + "/"
        self.download_dir = dirs["incomplete"] + self.nzbdir + "_downloaded0/"
        self.verifiedrar_dir = dirs["incomplete"] + self.nzbdir + "_verifiedrars0/"
        self.unpack_dir = dirs["incomplete"] + self.nzbdir + "_unpack0/"
        self.main_dir = dirs["incomplete"] + self.nzbdir
        self.rename_dir = dirs["incomplete"] + self.nzbdir + "_renamed0/"

    def article_producer(self, articles, articlequeue):
        for article in articles:
            articlequeue.put(article)

    def getbytescount(self, filelist):
        # generate all articles and files
        bytescount0 = 0
        for file_articles in filelist:
            # iterate over all articles in file
            for i, art0 in enumerate(file_articles):
                if i == 0:
                    continue
                _, _, art_bytescount = art0
                bytescount0 += art_bytescount
        bytescount0 = bytescount0 / (1024 * 1024 * 1024)
        return bytescount0

    def inject_articles(self, ftypes, filelist, files0, infolist0, bytescount0_0):
        # generate all articles and files
        files = files0
        infolist = infolist0
        bytescount0 = bytescount0_0
        for j, file_articles in enumerate(reversed(filelist)):
            # iterate over all articles in file
            filename, age, filetype, nr_articles = file_articles[0]
            # check if file already exists in "incomplete"
            if filetype in ftypes:
                level_servers = self.get_level_servers(age)
                files[filename] = (nr_articles, age, filetype, False, True)
                infolist[filename] = [None] * nr_articles
                for i, art0 in enumerate(file_articles):
                    if i == 0:
                        continue
                    art_nr, art_name, art_bytescount = art0
                    bytescount0 += art_bytescount
                    q = (filename, age, filetype, nr_articles, art_nr, art_name, level_servers)
                    self.articlequeue.put(q)
        bytescount0 = bytescount0 / (1024 * 1024 * 1024)
        return files, infolist, bytescount0

    def process_resultqueue(self, avgmiblist00, infolist00, files00):
        # read resultqueue + distribute to files
        newresult = False
        avgmiblist = avgmiblist00
        infolist = infolist00
        files = files00
        while True:
            try:
                resultarticle = self.resultqueue.get_nowait()
                self.resultqueue.task_done()
                filename, age, filetype, nr_articles, art_nr, art_name, download_server, inf0 = resultarticle
                bytesdownloaded = sum(len(i) for i in inf0)
                avgmiblist.append((time.time(), bytesdownloaded, download_server))
                try:
                    infolist[filename][art_nr - 1] = inf0
                    newresult = True
                except TypeError:
                    continue
                # check if file is completed and put to mp_queue/decode in case
                (f_nr_articles, f_age, f_filetype, f_done, f_failed) = files[filename]
                if not f_done and len([inf for inf in infolist[filename] if inf]) == f_nr_articles:        # check for failed!! todo!!
                    failed0 = False
                    if "failed" in infolist[filename]:
                        logger.error(filename + "failed!!")
                        failed0 = True
                    inflist0 = infolist[filename][:]
                    self.mp_work_queue.put((inflist0, self.download_dir, filename, filetype))
                    files[filename] = (f_nr_articles, f_age, f_filetype, True, failed0)
                    infolist[filename] = None
                    logger.info("All articles for " + filename + " downloaded, calling mp.decode ...")
            except KeyError:
                pass
            except queue.Empty:
                break
        return newresult, avgmiblist, infolist, files

    # main download routine
    def download_and_process(self):
        # directories
        #    incomplete/_downloaded0:   all files go straight to here after download
        #    incomplete/_verifiedrar0:  downloaded & verfied rars, ready to unrar
        #    incomplete/_unpack0:       unpacked verified rars
        #    incomplete:                final directory before moving to complete

        
        availmem0 = psutil.virtual_memory()[0] - psutil.virtual_memory()[1]

        # start decoder mpp
        self.mpp_decoder = mp.Process(target=lib.decode_articles, args=(self.mp_work_queue, self.mp_result_queue, self.logger, ))
        self.mpp_decoder.start()

        # start nzb parser mpp
        self.mpp_nzbparser = mp.Process(target=lib.ParseNZB, args=(self.pwdb, self.mp_nzbparser_outqueue, self.mp_nzbparser_inqueue,
                                                                   self.dirs["nzb"], self.logger, ))
        self.mpp_nzbparser.start()

        sighandler = SigHandler(self.servers, self.threads, self.pwdb, self.mpp_nzbparser, self.mpp_decoder, self.logger)
        signal.signal(signal.SIGINT, sighandler.signalhandler)
        signal.signal(signal.SIGTERM, sighandler.signalhandler)

        # wait for first nzb
        nzbname = None
        while not nzbname:
            allfileslist, filetypecounter, nzbname = self.pwdb.make_allfilelist(self.dirs["incomplete"])
            print(nzbname)
        self.make_dirs(nzbname)

        logger.info("Downloading articles for: " + self.nzb)

        # overall GB
        bytescount0 = self.getbytescount(allfileslist)

        avgmiblist = []
        status = 0        # 0 = running, 1 = exited successfull, -1 = exited cannot unrar
        inject_set0 = ["par2", "rar", "sfv", "nfo", "etc"]
        files = {}
        infolist = {}
        files, infolist, bytescount0 = self.inject_articles(inject_set0, allfileslist, files, infolist, bytescount0)
        loadpar2vols = False
        p2 = None

        # if files in queue, start connection worker threads
        if allfileslist:
            for sn, scon, _, _ in self.all_connections:
                t = ConnectionWorker(self.lock, (sn, scon), self.articlequeue, self.resultqueue, self.servers)
                self.threads.append((t, time.time()))
                t.start()

        sighandler.shutdown()

        # main download & processing loop
        while True and not self.sighandler.signal:

            # get mp_result_queue
            while True:
                try:
                    filename, full_filename, filetype, status, statusmsg, md5 = self.mp_result_queue.get_nowait()
                    filetypecounter[filetype]["counter"] += 1
                    filetypecounter[filetype]["loadedfiles"].append((filename, full_filename, md5))
                    if (filetype == "par2" or filetype == "par2vol") and not p2:
                        p2 = lib.Par2File(full_filename)
                        logger.info("Sending " + filename + "-p2 object to parverify_queue")
                        # self.mp_parverify_outqueue.put(p2)
                except queue.Empty:
                    break

            # if all downloaded postprocess
            dobreak = True
            for filetype, item in filetypecounter.items():
                if filetype == "par2vol" and not loadpar2vols:
                    continue
                if filetypecounter[filetype]["counter"] < filetypecounter[filetype]["max"]:
                    dobreak = False
                    break

            # read resultqueue + decode via mp
            newresult, avgmiblist, infolist, files = self.process_resultqueue(avgmiblist, infolist, files)

            if dobreak:
                break

        sighandler.shutdown()

        '''# update threads timestamps (if alive)
            # if thread is dead longer than 120 sec - kill it
            for k, (t, last_timestamp) in enumerate(self.threads):
                if t.isAlive() and t.nntpobj:
                        last_timestamp = time.time()
                        self.threads[k] = (t, last_timestamp)
                if t.isAlive() and time.time() - last_timestamp > 120:
                    t.stop()
                    t.join()
                    try:
                        t.nntopj.quit()
                        t.nntpobj = None
                    except Exception as e:
                        logger.error(str(e))

            # if all servers down (= no WAN)
            if len([t for t, _ in self.threads if t.isAlive()]) == 0:
                status = -1
                break
            time.sleep(0.1)

        # get final par2 repair status
        parverify_status = 0
        logger.info("Waiting for receiving ok from par_verifier")
        while True:
            try:
                endcode, parverify_status = self.mp_parverify_inqueue.get_nowait()
                if endcode == -9999:
                    break
            except queue.Empty:
                pass
        if parverify_status == 1:
            logger.info("All rar files ok / repaired!")
        elif parverify_status == -1:
            logger.error("Some rar files were corrupt/could not be repaired!")
        else:
            logger.error("Got no reasonable final state from par_verifier ...")

        # get final unrar status
        unrar_status = 0
        logger.info("Waiting for receiving ok from unrar")
        while True:
            try:
                unrar_status, statmsg = self.mp_unrarqueue.get_nowait()
                break
            except queue.Empty:
                pass
        if unrar_status == 0:
            logger.info("Unrar - " + statmsg)
        else:
            logger.error("Unrar - " + statmsg)

        # todo:
        #   clean directories and move to final directory

        # clean up
        logger.info("cleaning up ...")
        self.mp_work_queue.put(None)
        self.resultqueue.join()
        self.articlequeue.join()
        for t, _ in self.threads:
            t.stop()
            t.join()
        self.servers.close_all_connections()
        return status'''

    def get_level_servers(self, retention):
        le_serv0 = []
        for level, serverlist in self.level_servers.items():
            level_servers = serverlist
            le_dic = {}
            for le in level_servers:
                _, _, _, _, _, _, _, _, age = self.servers.get_single_server_config(le)
                le_dic[le] = age
            les = [le for le in level_servers if le_dic[le] > retention * 0.9]
            le_serv0.append(les)
        return le_serv0


def main():
    pwdb = lib.PWDB(logger)

    cfg = configparser.ConfigParser()
    cfg.read(dirs["config"] + "/ginzibix.config")

    # get servers
    servers = Servers(cfg)
    if not servers:
        logger.error("At least one server has to be provided, exiting!")
        sys.exit()

    dl = Downloader(servers, dirs, pwdb, logger)
    dl.download_and_process()


# -------------------- main --------------------

if __name__ == '__main__':

    print("Welcome to ginzibix 0.1-alpha, binary usenet downloader")
    print("-" * 60)

    main()
    sys.exit()

    logger.warning("### EXITED GINZIBIX ###")
