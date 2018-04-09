import os
import shutil
import glob
from .par2lib import Par2File, calc_file_md5hash_16k, check_for_par_filetype
from random import randint
import inotify_simple


def get_not_yet_renamed_files(source_dir):
    nrf = []
    for fn in glob.glob(source_dir + "*"):
        fn0 = fn.split("/")[-1]
        if not fn0.endswith(".renamed"):
            nrf.append((fn0, calc_file_md5hash_16k(fn0)))
    return nrf


def scan_renamed_dir(renamed_dir, p2obj):
    # get all files in renamed
    rn = []
    p2obj0 = p2obj
    p2basename0 = None
    for fn in glob.glob(renamed_dir + "*"):
        fn0 = fn.split("/")[-1]
        if not p2obj0:
            ptype0 = check_for_par_filetype(fn)
            if ptype0 == 1:
                p2obj0 = Par2File(fn)
                p2basename0 = fn.split(".par2")[0]
                print("Found .par2 in _renamed0: " + fn0)
        rn.append(fn0)
    return rn, p2obj0, p2basename0


def scan_for_par2(notrenamedfiles):
    p2obj0 = None
    p2basename0 = None
    for fn, _ in notrenamedfiles:
        ptype0 = check_for_par_filetype(fn)
        if ptype0 == 1:
            p2obj0 = Par2File(fn)
            p2basename0 = fn.split(".par2")[0]
            print("Found .par2 in _downloaded0: " + fn.split("/")[-1])
            break
    return p2obj0, p2basename0


def renamer_process_par2s(source_dir, dest_dir, p2obj, p2basename, notrenamedfiles):
    # search for not yet renamed par2/vol files
    p2obj0 = p2obj
    p2basename0 = p2basename
    not_renamed_par2list = []
    for pname, phash in notrenamedfiles:
        ptype0 = check_for_par_filetype(pname)
        if ptype0 == 1:
            ptype = "par2"
            p2obj0 = Par2File(pname)
            p2basename0 = pname.split(".par2")[0]
        elif ptype0 == 2:
            ptype = "par2vol"
        else:
            continue
        not_renamed_par2list.append((pname, ptype, phash))
    # print(not_renamed_par2list)
    if not_renamed_par2list:
        for pname, ptype, phash in not_renamed_par2list:
            pp = (pname, phash)
            if ptype == "par2":
                shutil.copyfile(source_dir + pname, dest_dir + pname)
                os.rename(source_dir + pname, source_dir + pname + ".renamed")
                notrenamedfiles.remove(pp)
            elif ptype == "par2vol" and p2basename0:
                # todo: if not p2basename ??
                volpart1 = randint(1, 99)
                volpart2 = randint(1, 99)
                shutil.copyfile(source_dir + pname, dest_dir + p2basename0 + ".vol" + str(volpart1).zfill(3) +
                                "+" + str(volpart2).zfill(3) + ".PAR2")
                os.rename(source_dir + pname, source_dir + pname + ".renamed")
                notrenamedfiles.remove(pp)
    return p2obj0, p2basename0


def rename_and_move_rarandremainingfiles(p2obj, notrenamedfiles, source_dir, dest_dir):
    if not p2obj:
        return
    rarfileslist = p2obj.md5_16khash()
    notrenamedfiles0 = notrenamedfiles[:]
    for a_name, a_md5 in notrenamedfiles0:
        pp = (a_name, a_md5)
        try:
            r_name = [fn for fn, r_md5 in rarfileslist if r_md5 == a_md5][0]
            if r_name != a_name:
                shutil.copyfile(source_dir + a_name, dest_dir + r_name)
            else:
                shutil.copyfile(source_dir + a_name, dest_dir + a_name)
            os.rename(source_dir + a_name, source_dir + a_name + ".renamed")
            notrenamedfiles.remove(pp)
        except IndexError:
            pass
        except Exception as e:
            print(str(e))
    for a_name, a_md5 in notrenamedfiles:
        shutil.copyfile(source_dir + a_name, dest_dir + a_name)
        os.rename(source_dir + a_name, source_dir + a_name + ".renamed")


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


# renamer with inotify
def renamer(source_dir, dest_dir, logger):
    if source_dir[-1] != "/":
        source_dir += "/"
    if dest_dir[-1] != "/":
        dest_dir += "/"
    cwd0 = os.getcwd()

    try:
        os.chdir(dest_dir)
    except FileNotFoundError:
        os.mkdir(dest_dir)

    # logger.debug("Starting renamer ...")
    os.chdir(source_dir)

    # init inotify
    inotify = inotify_simple.INotify()
    watch_flags = inotify_simple.flags.CREATE | inotify_simple.flags.DELETE | inotify_simple.flags.MODIFY | inotify_simple.flags.DELETE_SELF
    inotify.add_watch(source_dir, watch_flags)

    p2obj = None
    p2basename = None

    # eventslist = []
    isfirstrun = True
    islastrun = False

    while True:
        events = get_inotify_events(inotify)
        if isfirstrun or events:  # and events not in eventslist):
            print("Events: " + str(events))
            # eventslist.append(events)
            # get all files not yet .renamed
            print("Reading not yet downloaded files in _downloaded0")
            notrenamedfiles = get_not_yet_renamed_files(source_dir)
            # get all renames filed & trying to get .par2 file
            print("Reading files in _renamed0 & trying to get .par2 file")
            renamedfiles, p2obj, p2basename = scan_renamed_dir(dest_dir, p2obj)
            # if no par2 in _renamed, check _downloaded0
            if not p2obj:
                print("No p2obj yet found, looking in _downloaded0")
                p2obj, p2basename = scan_for_par2(notrenamedfiles)
                if p2obj:
                    print("p2obj found: " + p2basename)
            # rename par2 and move them
            p2obj, p2objname = renamer_process_par2s(source_dir, dest_dir, p2obj, p2basename, notrenamedfiles)
            # rename & move rar + remaining files
            rename_and_move_rarandremainingfiles(p2obj, notrenamedfiles, source_dir, dest_dir)
            isfirstrun = False
            print("-" * 60)
            # get_nowait
            
    os.chdir(cwd0)


# maindir = "st502304a4df4c023adf43c1462a.nfo"

# renamer("/home/stephan/.ginzibix/incomplete/" + maindir + "/_downloaded0",
#        "/home/stephan/.ginzibix/incomplete/" + maindir + "/_renamed0", None)