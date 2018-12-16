import os
import glob
import queue
import shutil
from .par2lib import calc_file_md5hash
import subprocess
import sys
import time
import pexpect
import inotify_simple
import signal
from .aux import PWDBSender
import inspect

lpref = __name__.split("lib.")[-1] + " - "


def whoami():
    outer_func_name = str(inspect.getouterframes(inspect.currentframe())[1].function)
    outer_func_linenr = str(inspect.currentframe().f_back.f_lineno)
    lpref = __name__.split("lib.")[-1] + " - "
    return lpref + outer_func_name + " / #" + outer_func_linenr + ": "


TERMINATED = False


class SigHandler_Verifier:
    def __init__(self, logger):
        self.logger = logger

    def sighandler_verifier(self, a, b):
        global TERMINATED
        self.logger.info(whoami() + "terminating ...")
        TERMINATED = True


def par_verifier(child_pipe, renamed_dir, verifiedrar_dir, main_dir, logger, nzbname, pvmode, event_idle, cfg):
    logger.debug(whoami() + "starting ...")
    sh = SigHandler_Verifier(logger)
    signal.signal(signal.SIGINT, sh.sighandler_verifier)
    signal.signal(signal.SIGTERM, sh.sighandler_verifier)

    pwdb = PWDBSender()
    event_idle.clear()

    if pvmode == "verify":
        # p2 = pwdb.get_renamed_p2(renamed_dir, nzbname)
        try:
            p2 = pwdb.exc("get_renamed_p2", [renamed_dir, nzbname], {})
        except Exception as e:
            logger.warning(whoami() + str(e))

    # pwdb.db_nzb_update_verify_status(nzbname, 1)
    pwdb.exc("db_nzb_update_verify_status", [nzbname, 1], {})

    # a: verify all unverified files in "renamed"
    unverified_rarfiles = None
    try:
        # unverified_rarfiles = pwdb.get_all_renamed_rar_files(nzbname)
        unverified_rarfiles = pwdb.exc("get_all_renamed_rar_files", [nzbname], {})
    except Exception as e:
        logger.debug(whoami() + str(e) + ": no unverified rarfiles met in first run, skipping!")
    doloadpar2vols = False
    if pvmode == "verify" and not p2:
        logger.debug(whoami() + "no par2 file found")
    if pvmode == "verify" and unverified_rarfiles and p2:
        logger.debug(whoami() + "verifying all unchecked rarfiles")
        for filename, f_origname in unverified_rarfiles:
            f_short = filename.split("/")[-1]
            md5 = calc_file_md5hash(renamed_dir + filename)
            md5match = [(pmd5 == md5) for pname, pmd5 in p2.filenames() if pname == filename]
            if False in md5match:
                logger.warning(whoami() + " error in md5 hash match for file " + f_short)
                # pwdb.db_file_update_parstatus(f_origname, -1)
                pwdb.exc("db_file_update_parstatus", [f_origname, -1], {})
                # pwdb.db_msg_insert(nzbname, "error in md5 hash match for file " + f_short, "warning")
                pwdb.exc("db_msg_insert", [nzbname, "error in md5 hash match for file " + f_short, "warning"], {})
                doloadpar2vols = True
            else:
                logger.info(whoami() + f_short + "md5 hash match ok, copying to verified_rar dir")
                shutil.copy(renamed_dir + filename, verifiedrar_dir)
                # pwdb.db_file_update_parstatus(f_origname, 1)
                pwdb.exc("db_file_update_parstatus", [f_origname, 1], {})
    if pvmode == "copy":
        logger.info(whoami() + "copying all rarfiles")
        for filename, f_origname in unverified_rarfiles:
            f_short = filename.split("/")[-1]
            logger.debug(whoami() + "copying " + f_short + " to verified_rar dir")
            shutil.copy(renamed_dir + filename, verifiedrar_dir)
            # pwdb.db_file_update_parstatus(f_origname, 1)
            pwdb.exc("db_file_update_parstatus", [f_origname, 1], {})
    if doloadpar2vols:
        child_pipe.send(doloadpar2vols)
        # mp_outqueue.put(doloadpar2vols)

    # b: inotify renamed_dir
    inotify = inotify_simple.INotify()
    watch_flags = inotify_simple.flags.CREATE | inotify_simple.flags.DELETE | inotify_simple.flags.MODIFY | inotify_simple.flags.DELETE_SELF
    inotify.add_watch(renamed_dir, watch_flags)

    while not TERMINATED:
        # allparstatus = pwdb.db_file_getallparstatus(0)
        allparstatus = pwdb.exc("db_file_getallparstatus", [0], {})
        if 0 not in allparstatus:
            event_idle.clear()
            logger.info(whoami() + "all renamed rars checked, exiting par_verifier")
            break
        events = get_inotify_events(inotify)
        event_idle.set()
        if events or 0 in allparstatus:
            event_idle.clear()
            if pvmode == "verify" and not p2:
                try:
                    # p2 = pwdb.get_renamed_p2(renamed_dir, nzbname)
                    p2 = pwdb.exc("get_renamed_p2", [renamed_dir, nzbname], {})
                except Exception as e:
                    print("p2 " + str(e))
            if pvmode == "verify" and p2:
                for rar in glob.glob(renamed_dir + "*"):
                    rar0 = rar.split("/")[-1]
                    # f0 = pwdb.db_file_get_renamed(rar0)
                    f0 = pwdb.exc("db_file_get_renamed", [rar0], {})
                    # print(f0.renamed_name, f0.ftype)
                    if not f0:
                        continue
                    f0_origname, f0_renamedname, f0_ftype = f0
                    if not f0_ftype == "rar":
                        continue
                    # if pwdb.db_file_getparstatus(rar0) == 0 and f0_renamed_name != "N/A":
                    if pwdb.exc("db_file_getparstatus", [rar0], {}) == 0 and f0_renamedname != "N/A":
                        f_short = f0_renamedname.split("/")[-1]
                        md5 = calc_file_md5hash(renamed_dir + rar0)
                        md5match = [(pmd5 == md5) for pname, pmd5 in p2.filenames() if pname == f0_renamedname]
                        if False in md5match:
                            logger.warning(whoami() + "error in md5 hash match for file " + f_short)
                            # pwdb.db_msg_insert(nzbname, "error in md5 hash match for file " + f_short, "warning")
                            pwdb.exc("db_msg_insert", [nzbname, "error in md5 hash match for file " + f_short, "warning"], {})
                            # pwdb.db_file_update_parstatus(f0_origname, -1)
                            pwdb.exc("db_file_update_parstatus", [f0_origname, -1], {})
                            if not doloadpar2vols:
                                doloadpar2vols = True
                                child_pipe.send(doloadpar2vols)
                                # mp_outqueue.put(doloadpar2vols)
                        else:
                            logger.info(whoami() + f_short + "md5 hash match ok, copying to verified_rar dir")
                            shutil.copy(renamed_dir + f0_renamedname, verifiedrar_dir)
                            # pwdb.db_file_update_parstatus(f0_origname, 1)
                            pwdb.exc("db_file_update_parstatus", [f0_origname, 1], {})
            if pvmode == "copy":
                for rar in glob.glob(renamed_dir + "*.rar"):
                    rar0 = rar.split("/")[-1]
                    # f0 = pwdb.db_file_get_renamed(rar0)
                    f0 = pwdb.exc("db_file_get_renamed", [rar0], {})
                    if not f0:
                        continue
                    f0_origname, f0_renamedname, f0_ftype = f0
                    # if pwdb.db_file_getparstatus(rar0) == 0 and f0_renamedname != "N/A":
                    if pwdb.exc("db_file_getparstatus", [rar0], {}) == 0 and f0_renamedname != "N/A":
                        logger.debug(whoami() + "copying " + f0_renamedname.split("/")[-1] + " to verified_rar dir")
                        shutil.copy(renamed_dir + f0_renamedname, verifiedrar_dir)
                        # pwdb.db_file_update_parstatus(f0_origname, 1)
                        pwdb.exc("db_file_update_parstatus", [f0_origname, 1], {})
        # allrarsverified, rvlist = pwdb.db_only_verified_rars(nzbname)
        allrarsverified, rvlist = pwdb.exc("db_only_verified_rars", [nzbname], {})
        if allrarsverified:
            break
        time.sleep(1)

    if TERMINATED:
        logger.info(whoami() + "terminated!")
        return

    logger.debug(whoami() + "all rars are verified")
    # par2name = pwdb.db_get_renamed_par2(nzbname)
    par2name = pwdb.exc("db_get_renamed_par2", [nzbname], {})
    # corruptrars = pwdb.get_all_corrupt_rar_files(nzbname)
    corruptrars = pwdb.exc("get_all_corrupt_rar_files", [nzbname], {})
    if not corruptrars:
        logger.debug(whoami() + "rar files ok, no repair needed, exiting par_verifier")
        # pwdb.db_nzb_update_verify_status(nzbname, 2)
        pwdb.exc("db_nzb_update_verify_status", [nzbname, 2], {})
    elif par2name and corruptrars:
        # pwdb.db_msg_insert(nzbname, "repairing rar files", "info")
        pwdb.exc("db_msg_insert", [nzbname, "repairing rar files", "info"], {})
        logger.info(whoami() + "par2vol files present, repairing ...")
        res0 = multipartrar_repair(renamed_dir, par2name, pwdb, nzbname, logger)
        if res0 == 1:
            logger.info(whoami() + "repair success")
            # pwdb.db_msg_insert(nzbname, "rar file repair success!", "info")
            pwdb.exc("db_msg_insert", [nzbname, "rar file repair success!", "info"], {})
            # pwdb.db_nzb_update_verify_status(nzbname, 2)
            pwdb.exc("db_nzb_update_verify_status", [nzbname, 2], {})
            # copy all no yet copied rars to verifiedrar_dir
            for c_origname, c_renamedname in corruptrars:
                logger.info(whoami() + "copying " + c_renamedname + " to verifiedrar_dir")
                pwdb.exc("db_file_update_parstatus", [c_origname, 1], {})
                pwdb.exc("db_file_update_status", [c_origname, 2], {})
                shutil.copy(renamed_dir + c_renamedname, verifiedrar_dir)
        else:
            logger.error(whoami() + "repair failed!")
            # pwdb.db_msg_insert(nzbname, "rar file repair failed", "error")
            pwdb.exc("db_msg_insert", [nzbname, "rar file repair failed", "error"], {})
            # pwdb.db_nzb_update_verify_status(nzbname, -1)
            pwdb.exc("db_nzb_update_verify_status", [nzbname, -1], {})
            for _, c_origname in corruptrars:
                # pwdb.db_file_update_parstatus(c_origname, -2)
                pwdb.exc("db_file_update_parstatus", [c_origname, -2], {})
    else:
        # pwdb.db_msg_insert(nzbname, "rar file repair failed, no par files available", "error")
        pwdb.exc("db_msg_insert", ["nzbname", "rar file repair failed, no par files available", "error"], {})
        logger.warning(whoami() + "some rars are corrupt but cannot repair (no par2 files)")
        pwdb.exc("db_nzb_update_verify_status", [nzbname, -2], {})
        # pwdb.db_nzb_update_verify_status(nzbname, 2)
    logger.info(whoami() + "terminated!")
    sys.exit()


def get_inotify_events(inotify):
    events = []
    for event in inotify.read(timeout=1):
        is_created_file = False
        str0 = event.name
        flgs0 = []
        for flg in inotify_simple.flags.from_mask(event.mask):
            if "flags.CREATE" in str(flg) and "flags.ISDIR" not in str(flg):
                flgs0.append(str(flg))
                is_created_file = True
        if not is_created_file:
            continue
        else:
            events.append((str0, flgs0))
    return events


def multipartrar_test(directory, rarname0, logger):
    rarnames = []
    sortedrarnames = []
    cwd0 = os.getcwd()
    os.chdir(directory)
    for r in glob.glob("*.rar"):
        rarnames.append(r)
    for r in rarnames:
        rarnr = r.split(".part")[-1].split(".rar")[0]
        sortedrarnames.append((int(rarnr), r))
    sortedrarnames = sorted(sortedrarnames, key=lambda nr: nr[0])
    rar0_nr, rar0_nm = [(nr, rarn) for (nr, rarn) in sortedrarnames if rarn == rarname0][0]
    ok_sorted = True
    for i, (nr, rarnr) in enumerate(sortedrarnames):
        if i + 1 == rar0_nr:
            break
        if i + 1 != nr:
            ok_sorted = False
            break
    if not ok_sorted:
        # print(-1)
        return -1              # -1 cannot check, rar in between is missing
    # ok sorted, unrar t
    cmd = "unrar t " + rarname0
    child = pexpect.spawn(cmd)
    str0 = []
    str00 = ""
    status = 1

    while True:
        try:
            a = child.read_nonblocking().decode("utf-8")
            if a == "\n":
                if str00:
                    str0.append(str00)
                    str00 = ""
            if ord(a) < 32:
                continue
            str00 += a
        except pexpect.exceptions.EOF:
            break
    # logger.info("MULTIPARTRAR_TEST > " + str(str0))
    for i, s in enumerate(str0):
        if rarname0 in s:
            try:
                if "- checksum error" in str0[i + 2]:
                    status = -2
                if "Cannot find" in s:
                    status = -1
            except Exception as e:
                logger.info("MULTIPARTRAR_TEST > " + str(e))
                status = -1

    os.chdir(cwd0)
    return status


def multipartrar_repair(directory, parvolname, pwdb, nzbname, logger):
    cwd0 = os.getcwd()
    os.chdir(directory)
    logger.info(whoami() + "checking if repair possible")
    pwdb.exc("db_msg_insert", [nzbname, "checking if repair is possible", "info"], {})
    ssh = subprocess.Popen(['par2verify', parvolname], shell=False, stdout=subprocess.PIPE, stderr=subprocess. PIPE)
    sshres = ssh.stdout.readlines()
    repair_is_required = False
    repair_is_possible = False
    exitstatus = 0
    for ss in sshres:
        ss0 = ss.decode("utf-8")
        if "Repair is required" in ss0:
            repair_is_required = True
        if "Repair is possible" in ss0:
            repair_is_possible = True
    if repair_is_possible and repair_is_required:
        logger.info(whoami() + "repair is required and possible, performing par2repair")
        pwdb.exc("db_msg_insert", [nzbname, "repair is required and possible, performing par2repair", "info"], {})
        # repair
        ssh = subprocess.Popen(['par2repair', parvolname], shell=False, stdout=subprocess.PIPE, stderr=subprocess. PIPE)
        sshres = ssh.stdout.readlines()
        repair_complete = False
        for ss in sshres:
            ss0 = ss.decode("utf-8")
            if "Repair complete" in ss0:
                repair_complete = True
        if not repair_complete:
            exitstatus = -1
            logger.error(whoami() + "could not repair")
        else:
            logger.info(whoami() + "repair success!!")
            exitstatus = 1
    elif repair_is_required and not repair_is_possible:
        logger.error(whoami() + "repair is required but not possible!")
        pwdb.exc("db_msg_insert", [nzbname, "repair is required but not possible!", "error"], {})
        exitstatus = -1
    elif not repair_is_required and not repair_is_possible:
        logger.error(whoami() + "repair is not required - all OK!")
        exitstatus = 1
    os.chdir(cwd0)
    return exitstatus

