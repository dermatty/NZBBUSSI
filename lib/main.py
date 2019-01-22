#!/home/stephan/.virtualenvs/nntp/bin/python

import zmq
import time
import os
import queue
import signal
import multiprocessing as mp
import re
import threading
import shutil
from .renamer import renamer
from .nzb_parser import ParseNZB
from .connections import ConnectionThreads
from .aux import PWDBSender, mpp_is_alive
#from .guiconnector import GUI_Connector
from .postprocessor import postprocess_nzb, postproc_pause, postproc_resume
from .mplogging import setup_logger, whoami
from .downloader import Downloader
from setproctitle import setproctitle
from statistics import mean
from collections import deque


class SigHandler_Main:

    def __init__(self, event_stopped, logger):
        self.logger = logger
        self.event_stopped = event_stopped

    def sighandler(self, a, b):
        self.event_stopped.set()
        self.logger.debug(whoami() + "set event_stopped = True")


def make_allfilelist_wait(pwdb, dirs, logger, timeout0):
    # immediatley get allfileslist
    try:
        nzbname = pwdb.exc("make_allfilelist", [dirs["incomplete"], dirs["nzb"]], {})
        if nzbname:
            logger.debug(whoami() + "no timeout, got nzb " + nzbname + " immediately!")
            return nzbname
        elif timeout0 and timeout0 <= -1:
            return None
    except Exception as e:
        logger.warning(whoami() + str(e))
        return None
    # setup inotify
    logger.debug(whoami() + "waiting for new nzb with timeout=" + str(timeout0))
    t0 = time.time()
    if not timeout0:
        delay0 = 5
    else:
        delay0 = 1
    while True:
        try:
            nzbname = pwdb.exc("make_allfilelist", [dirs["incomplete"], dirs["nzb"]], {})
        except Exception as e:
            logger.warning(whoami() + str(e))
        if nzbname:
            logger.debug(whoami() + "new nzb found in db, queuing ...")
            return nzbname
        if timeout0:
            if time.time() - t0 > timeout0 / 1000:
                break
        time.sleep(delay0)
    return None


def clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, ct, logger, stopall=False, onlyarticlequeue=True):
    # 1. join & clear all queues
    if dl:
        dl.clear_queues_and_pipes(onlyarticlequeue)
        logger.info(whoami() + "articlequeue cleared!")
    # 2. stop article_decoder
    try:
        if mpp_is_alive(mpp, "decoder"):
            mpid = mpp["decoder"].pid
            logger.debug(whoami() + "terminating decoder")
            os.kill(mpp["decoder"].pid, signal.SIGTERM)
            mpp["decoder"].join()
            mpp["decoder"] = None
            logger.info(whoami() + "decoder terminated!")
    except Exception as e:
        logger.debug(whoami() + str(e))
    # 4. clear mp_work_queue
    logger.debug(whoami() + "clearing mp_work_queue")
    while True:
        try:
            mp_work_queue.get_nowait()
        except (queue.Empty, EOFError):
            break
    logger.info(whoami() + "mp_work_queue cleared!")
    try:
        if mpp_is_alive(mpp, "unrarer"):
            mpid = mpp["unrarer"].pid
            logger.debug("terminating unrarer")
            os.kill(mpid, signal.SIGTERM)
            mpp["unrarer"].join()
            mpp["unrarer"] = None
            logger.info(whoami() + "unrarer terminated!")
    except Exception as e:
        logger.debug(whoami() + str(e))
    # 7. stop rar_verifier
    try:
        if mpp_is_alive(mpp, "verifier"):
            mpid = mpp["verifier"].pid
            logger.debug(whoami() + "terminating par_verifier")
            os.kill(mpp["verifier"].pid, signal.SIGTERM)
            mpp["verifier"].join()
            mpp["verifier"] = None
            logger.info(whoami() + "verifier terminated!")
    except Exception as e:
        logger.debug(whoami() + str(e))
    # 8. stop renamer only if stopall otherwise just pause
    if stopall:
        try:
            if mpp_is_alive(mpp, "renamer"):
                mpid = mpp["renamer"].pid
                logger.debug(whoami() + "stopall: terminating renamer")
                os.kill(mpp["renamer"].pid, signal.SIGTERM)
                mpp["renamer"].join()
                mpp["renamer"] = None
                logger.info(whoami() + "renamer terminated!")
        except Exception as e:
            logger.debug(whoami() + str(e))
    # just pause
    elif pipes:
        try:
            logger.debug(whoami() + "pausing renamer")
            pipes["renamer"][0].send(("pause", None, None))
        except Exception as e:
            logger.warning(whoami() + str(e))
    # 9. stop post-proc
    try:
        if mpp_is_alive(mpp, "post"):
            mpid = mpp["post"].pid
            logger.debug(whoami() + "terminating postprocesspr")
            os.kill(mpid, signal.SIGTERM)
            mpp["post"].join()
            mpp["post"] = None
            logger.info(whoami() + "postprocessor terminated!")
    except Exception as e:
        logger.debug(whoami() + str(e))
    # 10. stop nzbparser
    if stopall:
        try:
            if mpp_is_alive(mpp, "nzbparser"):
                mpid = mpp["nzbparser"].pid
                logger.debug(whoami() + "terminating nzb_parser")
                os.kill(mpp["nzbparser"].pid, signal.SIGTERM)
                mpp["nzbparser"].join()
                mpp["nzbparser"] = None
                logger.info(whoami() + "postprocessor terminated!")
        except Exception as e:
            logger.debug(whoami() + str(e))
    # 11. threads + servers
    if stopall:
        logger.debug(whoami() + "checking termination of connection threads")
        ct.stop_threads()

    logger.info(whoami() + "clearing finished")
    return


def connection_thread_health(threads):
        nothreads = len([t for t, _ in threads])
        nodownthreads = len([t for t, _ in threads if t.connectionstate == -1])
        if nothreads == 0:
            return 0
        return 1 - nodownthreads / (nothreads)


def set_guiconnector_data(guiconnector, results, ct, dl, statusmsg, logger):
    try:
        nzbname, downloaddata, return_reason, maindir = results
        bytescount0, availmem0, avgmiblist, filetypecounter, _, article_health, overall_size, already_downloaded_size, p2,\
            overall_size_wparvol, allfileslist = downloaddata
        downloaddata_gc = bytescount0, availmem0, avgmiblist, filetypecounter, nzbname, article_health, overall_size, already_downloaded_size
        # no ct.servers object if connection idle
        if not ct.servers:
            serverconfig = None
        else:
            serverconfig = ct.servers.server_config
        guiconnector.set_data(downloaddata_gc, ct.threads, serverconfig, statusmsg, dl.serverhealth())
    except Exception as e:
        logger.debug(whoami() + str(e) + ": cannot interpret gui-data from downloader")
    return article_health


def remove_nzbdirs(deleted_nzbs, dirs, pwdb, logger):
    for deleted_nzb in deleted_nzbs:
        nzbdirname = re.sub(r"[.]nzb$", "", deleted_nzb, flags=re.IGNORECASE) + "/"
        # delete nzb from .ginzibix/nzb
        try:
            os.remove(dirs["nzb"] + deleted_nzb)
            logger.debug(whoami() + ": deleted NZB " + deleted_nzb + " from NZB dir")
        except Exception as e:
            logger.debug(whoami() + str(e))
            # remove incomplete/$nzb_name
            try:
                shutil.rmtree(dirs["incomplete"] + nzbdirname)
                logger.debug(whoami() + ": deleted incomplete dir for " + deleted_nzb)
            except Exception as e:
                logger.debug(whoami() + str(e))


# main loop for ginzibix downloader
def ginzi_main(cfg, dirs, subdirs, mp_loggerqueue):

    setproctitle("gzbx." + os.path.basename(__file__))

    logger = setup_logger(mp_loggerqueue, __file__)
    logger.debug(whoami() + "starting ...")

    pwdb = PWDBSender()

    # multiprocessing events
    mp_events = {}
    mp_events["unrarer"] = mp.Event()
    mp_events["verifier"] = mp.Event()
    mp_events["post"] = mp.Event()

    # threading events
    event_stopped = threading.Event()

    articlequeue = deque()
    resultqueue = deque()
    mp_work_queue = mp.Queue()
    renamer_result_queue = mp.Queue()

    renamer_parent_pipe, renamer_child_pipe = mp.Pipe()
    unrarer_parent_pipe, unrarer_child_pipe = mp.Pipe()
    verifier_parent_pipe, verifier_child_pipe = mp.Pipe()
    pipes = {"renamer": [renamer_parent_pipe, renamer_child_pipe],
             "unrarer": [unrarer_parent_pipe, unrarer_child_pipe],
             "verifier": [verifier_parent_pipe, verifier_child_pipe]}

    ct = ConnectionThreads(cfg, articlequeue, resultqueue, logger)

    # update delay
    try:
        update_delay = float(cfg["GTKGUI"]["UPDATE_DELAY"])
    except Exception as e:
        logger.warning(whoami() + str(e) + ", setting update_delay to default 0.5")
        update_delay = 0.5

    # init tcp with gtkgui.py
    try:
        port = cfg["OPTIONS"]["PORT"]
        assert(int(port) > 1024 and int(port) <= 65535)
    except Exception as e:
        logger.debug(whoami() + str(e) + ", setting port to default 36603")
        port = "36603"
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind("tcp://*:" + port)
    socket.setsockopt(zmq.RCVTIMEO, int(update_delay * 1000))

    # init sighandler
    logger.debug(whoami() + "initializing sighandler")
    mpp = {"nzbparser": None, "decoder": None, "unrarer": None, "renamer": None, "verifier": None, "post": None}
    sh = SigHandler_Main(event_stopped, logger)
    signal.signal(signal.SIGINT, sh.sighandler)
    signal.signal(signal.SIGTERM, sh.sighandler)

    # start nzb parser mpp
    logger.info(whoami() + "starting nzbparser process ...")
    mpp_nzbparser = mp.Process(target=ParseNZB, args=(cfg, dirs, mp_loggerqueue, ))
    mpp_nzbparser.start()
    mpp["nzbparser"] = mpp_nzbparser

    # start renamer
    logger.info(whoami() + "starting renamer process ...")
    mpp_renamer = mp.Process(target=renamer, args=(renamer_child_pipe, renamer_result_queue, mp_loggerqueue, ))
    mpp_renamer.start()
    mpp["renamer"] = mpp_renamer

    dl = None
    nzbname = None
    paused = False
    # guiconnector.set_health(0, 0)
    article_health = 0
    connection_health = 0

    old_t = 0
    oldbytes0 = 0
    netstatlist = []
    dl_running = True
    # main looooooooooooooooooooooooooooooooooooooooooooooooooooop

    DEBUGPRINT = False

    try:
        while not event_stopped.is_set():
            # set connection health
            if dl:
                stat0 = pwdb.exc("db_nzb_getstatus", [nzbname], {})
                if stat0 == 2:
                    statusmsg = "downloading"
                elif stat0 == 3:
                    statusmsg = "postprocessing"
                elif stat0 == 4:
                    statusmsg = "success"
                elif stat0 == -4:
                    statusmsg = "failed"
                # send data to gui
                connection_health = connection_thread_health(ct.threads)
            else:
                article_health = 0
                connection_health = 0
                statusmsg = ""

            msg = None
            datarec = None
            try:
                msg, datarec = socket.recv_pyobj()
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    msg = None
                    pass
            except Exception as e:
                logger.error(whoami() + str(e))
                try:
                    socket.send_pyobj(("NOOK", None))
                except Exception as e:
                    logger.error(whoami() + str(e))
            if msg:
                if DEBUGPRINT:
                    print("-" * 10, "received", msg)
            if msg == "REQ":
                try:
                    if DEBUGPRINT:
                        print(">>>> #0 main:", time.time(), msg)
                    if not ct.servers:
                        serverconfig = None
                    else:
                        serverconfig = ct.servers.server_config
                    full_data_for_gui = pwdb.exc("get_all_data_for_gui", [], {})
                    sorted_nzbs, sorted_nzbshistory = pwdb.exc("get_stored_sorted_nzbs", [], {})
                    ct_threads = []
                    if dl:
                        dl_results = dl.results
                    else:
                        dl_results = None
                    getdata = None
                    downloaddata_gc = None
                    if dl_results:
                        nzbname, downloaddata, _, _ = dl_results
                        if DEBUGPRINT:
                            print(">>>> #0a main:", time.time(), msg)
                        bytescount0, availmem0, avgmiblist, filetypecounter, _, article_health, overall_size,\
                            already_downloaded_size, p2, overall_size_wparvol, allfileslist = downloaddata
                        if DEBUGPRINT:
                            print(">>>> #0b main:", time.time(), msg)
                        downloaddata_gc = bytescount0, availmem0, avgmiblist, filetypecounter, nzbname, article_health,\
                            overall_size, already_downloaded_size
                        # netstat
                        bytes0 = 0
                        for t, last_timestamp in ct.threads:
                            append_tuple = (t.bytesdownloaded, t.last_timestamp, t.idn, t.bandwidth_bytes)
                            ct_threads.append(append_tuple)
                            bytes0 += t.bytesdownloaded
                        if DEBUGPRINT:
                            print(">>>> #3 main:", time.time(), msg)
                        if bytes0 > 0:
                            dt = time.time() - old_t
                            if dt == 0:
                                dt = 0.001
                            mbitcurr = ((bytes0 - oldbytes0) / dt) / (1024 * 1024) * 8
                            oldbytes0 = bytes0
                            old_t = time.time()
                            netstatlist = [(mbit, t) for mbit, t in netstatlist if time.time() - t <= 2.0] + [(mbitcurr, old_t)]
                            mean_netstat = mean([mbit for mbit, _ in netstatlist])
                        else:
                            mean_netstat = 0
                        if DEBUGPRINT:
                            print(">>>> #4 main:", time.time(), msg)
                        getdata = downloaddata_gc, serverconfig, ct_threads, dl_running, statusmsg, mean_netstat,\
                            sorted_nzbs, sorted_nzbshistory, article_health, connection_health, dl.serverhealth(),\
                            full_data_for_gui
                    else:
                        downloaddata_gc = None, None, None, None, None, None, None, None
                        getdata = downloaddata_gc, serverconfig, ct_threads, dl_running, statusmsg, 0,\
                            sorted_nzbs, sorted_nzbshistory, 0, 0, None, full_data_for_gui
                        # if one element in getdata != None - send:
                    if getdata.count(None) != len(getdata) or downloaddata_gc.count(None) != len(downloaddata_gc):
                        sendtuple = ("DL_DATA", getdata)
                    else:
                        sendtuple = ("NOOK", None)
                except Exception as e:
                    logger.error(whoami() + str(e))
                    sendtuple = ("NOOK", None)
                try:
                    socket.send_pyobj(sendtuple)
                except Exception as e:
                    logger.error(whoami() + str(e))
                    if DEBUGPRINT:
                        print(str(e))
            elif msg == "SET_CLOSEALL":
                try:
                    socket.send_pyobj(("SET_CLOSE_OK", None))
                    event_stopped.set()
                    continue
                except Exception as e:
                    logger.error(whoami() + str(e))
            elif msg == "SET_PAUSE":     # pause downloads
                try:
                    if not paused:
                        paused = True
                        logger.info(whoami() + "download paused for NZB " + nzbname)
                        ct.pause_threads()
                        if dl:
                            dl.pause()
                            # dl.ct.reset_timestamps_bdl()
                        postproc_pause()
                    socket.send_pyobj(("SET_PAUSE_OK", None))
                    dl_running = False
                except Exception as e:
                    logger.error(whoami() + str(e))
            elif msg == "SET_RESUME":    # resume downloads
                try:
                    if paused:
                        logger.info(whoami() + "download resumed for NZB " + nzbname)
                        paused = False
                        ct.resume_threads()
                        if dl:
                            dl.resume()
                        postproc_resume()
                    socket.send_pyobj(("SET_RESUME_OK", None))
                    dl_running = True
                except Exception as e:
                    logger.error(whoami() + str(e))
                continue
            elif msg == "SET_NZB_ORDER":
                try:
                    logger.info(whoami() + "NZBs have been reordered/deleted")
                    # just get info if first has changed etc.
                    first_has_changed, deleted_nzbs = pwdb.exc("reorder_nzb_list", [datarec], {"delete_and_resetprios": False})
                    if deleted_nzbs:
                        pwdb.exc("db_msg_insert", [nzbname, "NZB(s) deleted", "warning"], {})
                    if first_has_changed:
                        logger.info(whoami() + "first NZB has changed")
                        if dl:
                            clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, ct, logger, stopall=False)
                            dl.stop()
                            dl.join()
                        first_has_changed, deleted_nzbs = pwdb.exc("reorder_nzb_list", [datarec], {"delete_and_resetprios": True})
                        remove_nzbdirs(deleted_nzbs, dirs, pwdb, logger)
                        nzbname = None
                        if dl:
                            # article_health = set_guiconnector_data(guiconnector, dl.results, ct, dl, statusmsg, logger)
                            del dl
                            dl = None
                    else:    # if current nzb didnt change just update, but do not restart
                        first_has_changed, deleted_nzbs = pwdb.exc("reorder_nzb_list", [datarec], {"delete_and_resetprios": True})
                        remove_nzbdirs(deleted_nzbs, dirs, pwdb, logger)
                    pwdb.exc("store_sorted_nzbs", [], {})
                    # release gtkgui from block
                    socket.send_pyobj(("SET_DELETE_REORDER_OK", None))
                except Exception as e:
                    logger.error(whoami() + str(e))
            elif msg:
                try:
                    socket.send_pyobj(("NOOK", None))
                except Exception as e:
                    if DEBUGPRINT:
                        print(str(e))
                    logger.debug(whoami() + str(e) + ", received msg: " + str(msg))
                continue

            # if not downloading
            if not dl:
                nzbname = make_allfilelist_wait(pwdb, dirs, logger, -1)
                if nzbname:
                    ct.reset_timestamps_bdl()
                    logger.info(whoami() + "got next NZB: " + str(nzbname))
                    dl = Downloader(cfg, dirs, ct, mp_work_queue, articlequeue, resultqueue, mpp, pipes,
                                    renamer_result_queue, mp_events, nzbname, mp_loggerqueue, logger)
                    if not paused:
                        ct.resume_threads()
                    if paused:
                        dl.pause()
                    dl.start()
                    old_t = 0
                    oldbytes0 = 0
                    netstatlist = []
            else:
                # if download ok -> postprocess
                if stat0 == 3 and not mpp_is_alive(mpp, "post"):
                    article_health = 0
                    connection_health = 0
                    logger.info(whoami() + "download success, postprocessing NZB " + nzbname)
                    mpp_post = mp.Process(target=postprocess_nzb, args=(nzbname, articlequeue, resultqueue, mp_work_queue, pipes, mpp, mp_events, cfg,
                                                                        dl.verifiedrar_dir, dl.unpack_dir, dl.nzbdir, dl.rename_dir, dl.main_dir,
                                                                        dl.download_dir, dl.dirs, dl.pw_file, mp_events["post"], mp_loggerqueue, ))
                    mpp_post.start()
                    mpp["post"] = mpp_post
                # if download failed
                elif stat0 == -2:
                    logger.info(whoami() + "download failed for NZB " + nzbname)
                    ct.pause_threads()
                    clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, ct, logger, stopall=False)
                    dl.stop()
                    dl.join()
                    # set 'flags' for getting next nzb
                    del dl
                    dl = None
                    nzbname = None
                    pwdb.exc("store_sorted_nzbs", [], {})
                # if postproc ok
                elif stat0 == 4:
                    logger.info(whoami() + "postprocessor success for NZB " + nzbname)
                    ct.pause_threads()
                    clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, ct, logger, stopall=False)
                    dl.stop()
                    dl.join()
                    if mpp_is_alive(mpp, "post"):
                        mpp["post"].join()
                        mpp["post"] = None
                    # article_health = set_guiconnector_data(guiconnector, dl.results, ct, dl, "success", logger)
                    pwdb.exc("db_msg_insert", [nzbname, "downloaded and postprocessed successfully!", "success"], {})
                    # set 'flags' for getting next nzb
                    del dl
                    dl = None
                    nzbname = None
                    pwdb.exc("store_sorted_nzbs", [], {})
                # if postproc failed
                elif stat0 == -4:
                    logger.error(whoami() + "postprocessor failed for NZB " + nzbname)
                    ct.pause_threads()
                    clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, logger, stopall=False)
                    dl.stop()
                    dl.join()
                    if mpp_is_alive(mpp, "post"):
                        mpp["post"].join()
                    # article_health = set_guiconnector_data(guiconnector, dl.results, ct, dl, "failed", logger)
                    pwdb.exc("db_msg_insert", [nzbname, "downloaded and/or postprocessing failed!", "error"], {})
                    mpp["post"] = None
                    # set 'flags' for getting next nzb
                    del dl
                    dl = None
                    nzbname = None
                    pwdb.exc("store_sorted_nzbs", [], {})
    except Exception as e:
        if DEBUGPRINT:
            print(str(e))
        else:
            pass
    # shutdown
    logger.info(whoami() + "closeall: starting shutdown sequence")
    ct.pause_threads()
    logger.debug(whoami() + "closeall: connection threads paused")
    if dl:
        dl.stop()
        dl.join()
    logger.debug(whoami() + "closeall: downloader joined")
    clear_download(nzbname, pwdb, articlequeue, resultqueue, mp_work_queue, dl, dirs, pipes, mpp, ct, logger, stopall=True, onlyarticlequeue=False)
    dl = None
    logger.debug(whoami() + "closing: closing gtkgui-socket")
    try:
        socket.close()
        context.term()
    except Exception as e:
        logger.warning(whoami())
    logger.debug(whoami() + "closeall: all cleared")
    logger.info(whoami() + "exited!")
