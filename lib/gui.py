import gi
import matplotlib
import queue
import sys
import threading
import time
import datetime
import pandas as pd
from gi.repository import Gtk, Gio, GdkPixbuf, GLib, Pango
from .aux import get_cut_nzbname, get_cut_msg, get_bg_color, GUI_Poller, get_status_name_and_color, get_server_config
from .mplogging import whoami, setup_logger
from matplotlib.backends.backend_gtk3agg import (
    FigureCanvasGTK3Agg as FigureCanvas)
from matplotlib.figure import Figure
from matplotlib.dates import DayLocator, MinuteLocator, HourLocator, SecondLocator, DateFormatter
from pandas.plotting import register_matplotlib_converters

matplotlib.rcParams['toolbar'] = 'None'
matplotlib.use('GTK3Agg')
register_matplotlib_converters()


gi.require_version("Gtk", "3.0")

__appname__ = "ginzibix"
__version__ = "0.01 pre-alpha"
__author__ = "dermatty"

ICONFILE = "lib/gzbx1.png"
GLADEFILE = "lib/ginzibix.glade"

GIBDIVISOR = (1024 * 1024 * 1024)
CLOSEALL_TIMEOUT = 8
MAX_NZB_LEN = 50
MAX_MSG_LEN = 120

MENU_XML = """
<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <menu id="app-menu">
    <section>
      <item>
        <attribute name="action">app.settings</attribute>
        <attribute name="label" translatable="yes">_Settings</attribute>
      </item>
      <item>
        <attribute name="action">app.about</attribute>
        <attribute name="label" translatable="yes">_About</attribute>
      </item>
      <item>
        <attribute name="action">app.quit</attribute>
        <attribute name="label" translatable="yes">_Quit</attribute>
        <attribute name="accel">&lt;Primary&gt;q</attribute>
    </item>
    </section>
  </menu>
</interface>
"""

# black blue red green orange magenta darkgreen steelblue brown darkred
COLORCODES = ["#000000", "#0000FF", "#FF0000", "#008000", "#FFA500", "#FF00FF", "#006400", "#4682B4", "#A52A2A", "#8B0000"] * 2
LINESTYLES = ["-", "--", "-.", ":"] * 5
MARKERS = ["D", "o", ".", "h", "^"] * 4

FREQ_DIC = {"per second": "dlsec",
            "per minute": "dlmin",
            "per hour": "dlh",
            "per day": "dld"}

UNIT_DIC = {"MBit": "mbit", "KB": "kb", "MB": "mb", "GB": "gb"}


# ******************************************************************************************************
# *** main gui itself
class ApplicationGui(Gtk.Application):

    def __init__(self, dirs, cfg, mp_loggerqueue):

        # init app
        self.app = Gtk.Application.new("org.dermatty.ginzibix", Gio.ApplicationFlags(0), )
        self.app.connect("startup", self.on_app_startup)
        self.app.connect("activate", self.on_app_activate)
        self.app.connect("shutdown", self.on_app_shutdown)

        # settings from cfg
        self.settings_servers = None
        self.port = None
        self.host = None

        # data
        self.logger = setup_logger(mp_loggerqueue, __file__)
        self.dirs = dirs
        self.lock = threading.Lock()
        self.liststore = None
        self.liststore_s = None
        self.liststore_nzbhistory = None
        self.mbitlabel2 = None
        self.single_selected = None
        self.cfg = cfg
        self.appdata = AppData(self.lock)
        self.read_config()
        self.dl_running = True
        self.nzb_status_string = ""
        self.guiqueue = queue.Queue()
        self.lock = threading.Lock()
        self.activestack = None
        self.restartall = False

    def run(self, argv):
        self.app.run(argv)
        try:
            self.window.close()
        except Exception:
            pass
        if self.appdata.settings_changed and self.restartall:
            return 3
        else:
            return 0

    def on_app_activate(self, app):
        self.builder = Gtk.Builder()
        self.builder.add_from_file(GLADEFILE)
        self.builder.add_from_string(MENU_XML)
        app.set_app_menu(self.builder.get_object("app-menu"))

        self.obj = self.builder.get_object
        self.window = self.obj("main_window")
        self.levelbar = self.obj("levelbar")
        self.mbitlabel2 = self.obj("mbitlabel2")
        self.no_queued_label = self.obj("no_queued")
        self.overall_eta_label = self.obj("overalleta")
        self.servergraph_sw = self.obj("servercanvas_sw")
        self.server_edit_button = self.obj("server_edit")
        self.server_delete_button = self.obj("server_delete")
        self.server_apply_button = self.obj("apply_server")

        self.comboboxtext = self.obj("frequencycombotext")
        self.comboboxtext.set_active(0)
        self.builder.connect_signals(Handler(self))
        self.window.set_application(self.app)

        # set icon & app name
        try:
            self.window.set_icon_from_file(ICONFILE)
        except GLib.GError as e:
            self.logger.error(whoami() + str(e) + "cannot find icon file!" + ICONFILE)
        self.window.set_wmclass(__appname__, __appname__)

        # set app position
        screen = self.window.get_screen()
        self.max_width = screen.get_width()
        self.max_height = screen.get_height()
        self.n_monitors = screen.get_n_monitors()
        self.window.set_size_request(int(self.max_width/(2 * self.n_monitors)), self.max_height)

        # setup servergraph w/ pyplot
        self.servergraph_figure = Figure(figsize=(5, 4), dpi=100)
        self.servergraph_canvas = FigureCanvas(self.servergraph_figure)
        self.servergraph_sw.add_with_viewport(self.servergraph_canvas)
        self.setup_pyplot()

        # add non-standard actions
        self.add_simple_action("about", self.on_action_about_activated)
        self.add_simple_action("message", self.on_action_message_activated)
        self.add_simple_action("quit", self.on_action_quit_activated)
        self.add_simple_action("settings", self.on_action_settings_activated)

        # set liststores / treeviews
        self.setup_frame_nzblist()
        self.setup_frame_logs()
        self.setup_frame_nzbhistory()
        self.setup_frame_serverlist()

        self.gridbuttonlist = [self.obj("button_top"), self.obj("button_up"), self.obj("button_down"),
                               self.obj("button_bottom"), self.obj("button_interrupt"), self.obj("button_delete")]
        self.historybuttonlist = [self.obj("button_hist_process_start"), self.obj("button_hist_process_last"),
                                  self.obj("button_hist_delete")]

        self.window.show_all()

        self.guipoller = GUI_Poller(self, delay=self.update_delay, host=self.host, port=self.port)
        self.guipoller.start()

        # call main thread from here!!

    def on_app_startup(self, app):
        pass

    def on_app_shutdown(self, app):
        self.appdata.closeall = True
        if self.appdata.settings_changed:
            closeval = self.cfg
        else:
            closeval = None
        self.guiqueue.put(("closeall", closeval))
        t0 = time.time()
        while self.appdata.closeall and time.time() - t0 < CLOSEALL_TIMEOUT:
            time.sleep(0.1)
        self.guipoller.stop()

    # ----------------------------------------------------------------------------------------------------
    # -- Application menu actions

    def add_simple_action(self, name, callback):
        action = Gio.SimpleAction.new(name)
        action.connect("activate", callback)
        self.app.add_action(action)

    def on_action_settings_activated(self, action, param):
        pass

    def on_action_about_activated(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.window, modal=True)
        about_dialog.set_program_name(__appname__)
        about_dialog.set_version(__version__)
        about_dialog.set_copyright("Copyright \xa9 2018 dermatty")
        about_dialog.set_comments("A binary newsreader for the Gnome desktop")
        about_dialog.set_website("https://github.com/dermatty/GINZIBIX")
        about_dialog.set_website_label('Ginzibix on GitHub')
        try:
            about_dialog.set_logo(GdkPixbuf.Pixbuf.new_from_file_at_size(ICONFILE, 64, 64))
        except GLib.GError as e:
            self.logger.error(whoami() + str(e) + ": cannot find icon file!")

        about_dialog.set_license_type(Gtk.License.GPL_3_0)
        about_dialog.present()

    def on_action_message_activated(self, actiong, param):
        pass

    def on_action_quit_activated(self, action, param):
        self.app.quit()

    # ----------------------------------------------------------------------------------------------------
    #  -- liststores / treeviews

    def setup_frame_serverlist(self):
        self.serverlist_liststore = Gtk.ListStore(str, bool)
        self.treeview_serverlist = Gtk.TreeView(model=self.serverlist_liststore)

        self.treeview_serverlist.set_reorderable(False)
        sel = self.treeview_serverlist.get_selection()
        sel.set_mode(Gtk.SelectionMode.SINGLE)
        sel.connect("changed", self.on_treeviewserverlist_selection_changed)

        self.update_serverlist_liststore(init=True)

        # 0st column: server name
        renderer_text0 = Gtk.CellRendererText(width_chars=75, ellipsize=Pango.EllipsizeMode.END)
        column_text0 = Gtk.TreeViewColumn("Server name", renderer_text0, text=0)
        column_text0.set_expand(True)
        self.treeview_serverlist.append_column(column_text0)

        # 1th server toggled
        renderer_toggle0 = Gtk.CellRendererToggle()
        column_toggle0 = Gtk.TreeViewColumn("Active", renderer_toggle0, active=1)
        renderer_toggle0.connect("toggled", self.on_activated_toggled_serverlist)
        column_toggle0.set_alignment(0.5)
        self.treeview_serverlist.append_column(column_toggle0)

        self.server_delete_button.set_sensitive(False)
        self.server_edit_button.set_sensitive(False)
        self.server_apply_button.set_sensitive(False)

        self.obj("serverlist_sw").add(self.treeview_serverlist)

    # liststore/treeview for logs in stack DOWNLOADING
    def setup_frame_logs(self):
        self.logs_liststore = Gtk.ListStore(str, str, str, str, str)
        self.treeview_loglist = Gtk.TreeView(model=self.logs_liststore)

        renderer_log4 = Gtk.CellRendererText()
        column_log4 = Gtk.TreeViewColumn("Time", renderer_log4, text=2, background=3, foreground=4)
        column_log4.set_min_width(80)
        self.treeview_loglist.append_column(column_log4)

        renderer_log3 = Gtk.CellRendererText()
        column_log3 = Gtk.TreeViewColumn("Level", renderer_log3, text=1, background=3, foreground=4)
        column_log3.set_min_width(80)
        self.treeview_loglist.append_column(column_log3)

        renderer_log2 = Gtk.CellRendererText()
        column_log2 = Gtk.TreeViewColumn("Message", renderer_log2, text=0, background=3, foreground=4)
        column_log2.set_expand(True)
        column_log2.set_min_width(520)
        self.treeview_loglist.append_column(column_log2)

        self.obj("scrolled_window_logs").add(self.treeview_loglist)

    # liststore/treeview for nzbhistory
    def setup_frame_nzbhistory(self):
        # (selected, nzbname, statusstring, size, downloaded, downloaded%, color_string)
        self.nzbhistory_liststore = Gtk.ListStore(bool, str, str, float, float, float, str)
        self.update_nzbhistory_liststore()
        self.treeview_history = Gtk.TreeView(model=self.nzbhistory_liststore)
        self.treeview_history.set_reorderable(False)
        sel = self.treeview_history.get_selection()
        sel.set_mode(Gtk.SelectionMode.NONE)
        # 0th selection toggled
        historyrenderer_toggle = Gtk.CellRendererToggle()
        historycolumn_toggle = Gtk.TreeViewColumn("Select", historyrenderer_toggle, active=0)
        historyrenderer_toggle.connect("toggled", self.on_inverted_toggled_nzbhistory)
        self.treeview_history.append_column(historycolumn_toggle)
        # 1st column: NZB name
        historyrenderer_text0 = Gtk.CellRendererText()
        historycolumn_text0 = Gtk.TreeViewColumn("NZB name", historyrenderer_text0, text=1)
        historycolumn_text0.set_expand(True)
        historycolumn_text0.set_min_width(320)
        self.treeview_history.append_column(historycolumn_text0)
        # 2nd column status
        historyrenderer_text1 = Gtk.CellRendererText()
        historycolumn_text1 = Gtk.TreeViewColumn("Status", historyrenderer_text1, text=2, background=6)
        historycolumn_text1.set_min_width(80)
        self.treeview_history.append_column(historycolumn_text1)
        # 4th overall GiB
        historyrenderer_text2 = Gtk.CellRendererText()
        historycolumn_text2 = Gtk.TreeViewColumn("Size", historyrenderer_text2, text=3)
        historycolumn_text2.set_cell_data_func(historyrenderer_text2, lambda col, cell, model, iter, unused:
                                               cell.set_property("text", "{0:.2f}".format(model.get(iter, 3)[0]) + " GiB"))
        historycolumn_text2.set_min_width(80)
        self.treeview_history.append_column(historycolumn_text2)
        # 5th downloaded in %
        historyrenderer_text3 = Gtk.CellRendererText()
        historycolumn_text3 = Gtk.TreeViewColumn("downloaded %", historyrenderer_text3, text=5)
        historycolumn_text3.set_cell_data_func(historyrenderer_text3, lambda col, cell, model, iter, unused:
                                               cell.set_property("text", "{0:.0f}".format(min(model.get(iter, 5)[0] * 100, 100)) + "%"))
        self.treeview_history.append_column(historycolumn_text3)

        self.obj("detailsscrolled_window").add(self.treeview_history)

    # liststore/treeview for nzblist in stack DOWNLOADING
    def setup_frame_nzblist(self):
        self.nzblist_liststore = Gtk.ListStore(str, int, float, float, str, str, bool, str, str)
        self.update_liststore()
        # set treeview + actions
        self.treeview_nzblist = Gtk.TreeView(model=self.nzblist_liststore)
        self.treeview_nzblist.set_reorderable(False)
        sel = self.treeview_nzblist.get_selection()
        sel.set_mode(Gtk.SelectionMode.NONE)
        # 0th selection toggled
        renderer_toggle = Gtk.CellRendererToggle()
        column_toggle = Gtk.TreeViewColumn("Select", renderer_toggle, active=6)
        renderer_toggle.connect("toggled", self.on_inverted_toggled)
        self.treeview_nzblist.append_column(column_toggle)
        # 1st column: NZB name
        renderer_text0 = Gtk.CellRendererText()
        column_text0 = Gtk.TreeViewColumn("NZB name", renderer_text0, text=0)
        column_text0.set_expand(True)
        column_text0.set_min_width(320)
        self.treeview_nzblist.append_column(column_text0)
        # 2nd: progressbar
        renderer_progress = Gtk.CellRendererProgress()
        column_progress = Gtk.TreeViewColumn("Progress", renderer_progress, value=1, text=5)
        column_progress.set_min_width(260)
        column_progress.set_expand(True)
        self.treeview_nzblist.append_column(column_progress)
        # 3rd downloaded GiN
        renderer_text1 = Gtk.CellRendererText()
        column_text1 = Gtk.TreeViewColumn("Downloaded", renderer_text1, text=2)
        column_text1.set_cell_data_func(renderer_text1, lambda col, cell, model, iter, unused:
                                        cell.set_property("text", "{0:.2f}".format(model.get(iter, 2)[0]) + " GiB"))
        self.treeview_nzblist.append_column(column_text1)
        # 4th overall GiB
        renderer_text2 = Gtk.CellRendererText()
        column_text2 = Gtk.TreeViewColumn("Overall", renderer_text2, text=3)
        column_text2.set_cell_data_func(renderer_text2, lambda col, cell, model, iter, unused:
                                        cell.set_property("text", "{0:.2f}".format(model.get(iter, 3)[0]) + " GiB"))
        column_text2.set_min_width(80)
        self.treeview_nzblist.append_column(column_text2)
        # 5th Eta
        renderer_text3 = Gtk.CellRendererText()
        column_text3 = Gtk.TreeViewColumn("Eta", renderer_text3, text=4)
        column_text3.set_min_width(80)
        self.treeview_nzblist.append_column(column_text3)
        # 7th status
        renderer_text7 = Gtk.CellRendererText()
        column_text7 = Gtk.TreeViewColumn("Status", renderer_text7, text=7, background=8)
        column_text7.set_min_width(80)
        self.treeview_nzblist.append_column(column_text7)
        self.obj("scrolled_window_nzblist").add(self.treeview_nzblist)

    def update_servergraph(self):
        '''FREQ_DIC = {"Speed MiB / sec": "mbsec",
            "Download - seconds": "dlsec",
            "Download - minutes": "dlmin",
            "Download - hours": "dlh",
            "Download - days": "dld"}'''
        for a in self.annotations:
                a.remove()
        self.annotations[:] = []
        for i, s0 in enumerate(self.appdata.active_servers):
            # for i, s in enumerate(self.appdata.server_ts_diff):
            s, _, _, _, _, _, _, _, _, _, _ = s0
            try:
                tsd = self.appdata.server_ts_diff[s]["sec"]
            except Exception:
                continue
            ax0 = self.ax[i]
            if self.appdata.servergraph_cumulative:
                if self.appdata.servergraph_freq == "dlsec":
                    tsd = self.appdata.server_ts[s]["sec"].fillna(0)
                elif self.appdata.servergraph_freq == "dlmin":
                    tsd = self.appdata.server_ts[s]["minute"].fillna(0)
                elif self.appdata.servergraph_freq == "dlh":
                    tsd = self.appdata.server_ts[s]["hour"].fillna(0)
                elif self.appdata.servergraph_freq == "dld":
                    tsd = self.appdata.server_ts[s]["day"].fillna(0)
            else:
                if self.appdata.servergraph_freq == "dlsec":
                    tsd = self.appdata.server_ts_diff[s]["sec"]
                elif self.appdata.servergraph_freq == "dlmin":
                    tsd = self.appdata.server_ts_diff[s]["minute"] = self.appdata.server_ts[s]["minute"].diff().fillna(0)
                elif self.appdata.servergraph_freq == "dlh":
                    tsd = self.appdata.server_ts_diff[s]["hour"] = self.appdata.server_ts[s]["hour"].diff().fillna(0)
                elif self.appdata.servergraph_freq == "dld":
                    tsd = self.appdata.server_ts_diff[s]["day"] = self.appdata.server_ts[s]["day"].diff().fillna(0)
            # data is stored in MB
            if self.appdata.servergraph_unit == "mbit":
                ydata = [int(tsd[i] * 8) for i in range(len(tsd))]
            elif self.appdata.servergraph_unit == "mb":
                ydata = [int(tsd[i]) for i in range(len(tsd))]
            elif self.appdata.servergraph_unit == "kb":
                ydata = [int(tsd[i] * 1000) for i in range(len(tsd))]
            elif self.appdata.servergraph_unit == "gb":
                ydata = [float("{0:.2f}".format(tsd[i]/1000)) for i in range(len(tsd))]
            xdata = [pd.to_datetime(tsd.index[i]) for i in range(len(tsd))]
            lines0 = self.lines[i]
            lines0.set_xdata(xdata)
            lines0.set_ydata(ydata)
            ax0.relim()
            ax0.autoscale_view()
            for i, j in zip(xdata, ydata):
                self.annotations.append(ax0.annotate("%s" % j, xy=(i, j), xytext=(-1, 6), xycoords="data", textcoords="offset points",
                                                     fontsize=8))
        self.servergraph_figure.canvas.draw()
        self.servergraph_figure.canvas.flush_events()

    # ----------------------------------------------------------------------------------------------------
    # all the gui update functions

    def update_mainwindow(self, data, server_config, dl_running, nzb_status_string, sortednzblist0,
                          sortednzbhistorylist0, article_health, connection_health, dlconfig, fulldata,
                          gb_downloaded, server_ts):

        # calc current speed + differences time series
        netstat_mbitcur = 0
        self.appdata.server_ts_diff = {}
        self.appdata.server_ts = server_ts
        for serversetting in self.settings_servers:
            server_name, _, _, _, _, _, _, _, _, _, useserver = serversetting
            if useserver:
                self.appdata.server_ts_diff[server_name] = {}
                try:
                    self.appdata.server_ts_diff[server_name]["sec"] = server_ts[server_name]["sec"].diff().fillna(0)
                except Exception:
                    pass
        try:
            sts_window = min(3, len(self.appdata.server_ts_diff["-ALL SERVERS-"]["sec"]))
            netstat_mbitcur = int(self.appdata.server_ts_diff["-ALL SERVERS-"]["sec"].rolling(window=sts_window).mean()[-1]) * 8
        except Exception:
            netstat_mbitcur = 0

        # ### stack "SERVERGRAPHS" ###
        if self.activestack == "servergraphs":
            self.update_servergraph()

        # fulldata: contains messages
        if fulldata and self.appdata.fulldata != fulldata:
            self.appdata.fulldata = fulldata
            try:
                self.appdata.nzbname = fulldata["all#"][0]
            except Exception:
                self.appdata.nzbname = None
            self.update_logstore()

        # ### stack "HISTORY" ###
        if self.activestack == "history":
            if sortednzbhistorylist0 and sortednzbhistorylist0 != self.appdata.sortednzbhistorylist:
                if sortednzbhistorylist0 == [-1]:
                    sortednzbhistorylist = []
                else:
                    sortednzbhistorylist = sortednzbhistorylist0
                nzbs_copy = self.appdata.nzbs_history.copy()
                self.appdata.nzbs_history = []
                for idx1, (n_name, n_prio, n_updatedate, n_status, n_size, n_downloaded) in enumerate(sortednzbhistorylist):
                    n_downloaded_gb = n_downloaded / GIBDIVISOR
                    n_size_gb = n_size / GIBDIVISOR
                    n_perc_downloaded = n_downloaded_gb / n_size_gb
                    selected = False
                    for n_selected0, n_name0, n_status0, n_size0, n_downloaded0, _, _ in nzbs_copy:
                        if n_name0 == n_name:
                            selected = n_selected0
                    self.appdata.nzbs_history.append((selected, n_name, n_status, n_size_gb, n_downloaded_gb, n_perc_downloaded, "white"))
                if nzbs_copy != self.appdata.nzbs_history:
                    self.update_nzbhistory_liststore()
                self.appdata.sortednzbhistorylist = sortednzbhistorylist0[:]

        # downloading nzbs
        if (sortednzblist0 and sortednzblist0 != self.appdata.sortednzblist):    # or (sortednzblist0 == [-1] and self.appdata.sortednzblist):
            # sort again just to make sure
            if sortednzblist0 == [-1]:
                sortednzblist = []
            else:
                sortednzblist = sorted(sortednzblist0, key=lambda prio: prio[1])
            nzbs_copy = self.appdata.nzbs.copy()
            self.appdata.nzbs = []
            for idx1, (n_name, n_prio, n_ts, n_status, n_siz, n_downloaded) in enumerate(sortednzblist):
                try:
                    n_perc = min(int((n_downloaded/n_siz) * 100), 100)
                except ZeroDivisionError:
                    n_perc = 0
                n_dl = n_downloaded / GIBDIVISOR
                n_size = n_siz / GIBDIVISOR
                if self.appdata.mbitsec > 0 and self.dl_running:
                    eta0 = (((n_size - n_dl) * 1024) / (self.appdata.mbitsec / 8))
                    if eta0 < 0:
                        eta0 = 0
                    try:
                        etastr = str(datetime.timedelta(seconds=int(eta0)))
                    except Exception:
                        etastr = "-"
                else:
                    etastr = "-"
                selected = False
                for n_name0, n_perc0, n_dl0, n_size0, etastr0, n_percstr0, selected0, status0 in nzbs_copy:
                    if n_name0 == n_name:
                        selected = selected0
                self.appdata.nzbs.append((n_name, n_perc, n_dl, n_size, etastr, str(n_perc) + "%", selected, n_status))
            if nzbs_copy != self.appdata.nzbs:
                self.update_liststore()
            self.appdata.sortednzblist = sortednzblist0[:]

        if not self.appdata.nzbs:
            self.no_queued_label.set_text("Queued: -")

        if data and None not in data:   # and (data != self.appdata.dldata or netstat_mbitcur != self.appdata.netstat_mbitcur):
            bytescount00, availmem00, avgmiblist00, filetypecounter00, nzbname, article_health, overall_size, already_downloaded_size = data
            try:
                firstsortednzb = sortednzblist0[0][0]
            except Exception:
                firstsortednzb = None
            if nzbname is not None and nzbname == firstsortednzb:
                mbitseccurr = 0
                gbdown0 = gb_downloaded
                mbitseccurr = netstat_mbitcur
                if not dl_running:
                    self.dl_running = False
                else:
                    self.dl_running = True
                self.nzb_status_string = nzb_status_string
                with self.lock:
                    if mbitseccurr > self.appdata.max_mbitsec:
                        self.appdata.max_mbitsec = mbitseccurr
                        self.levelbar.set_tooltip_text("Max = " + str(int(self.appdata.max_mbitsec)))
                    self.appdata.nzbname = nzbname
                    if nzb_status_string == "postprocessing" or nzb_status_string == "success":
                        self.appdata.overall_size = overall_size
                        self.appdata.gbdown = overall_size
                        self.appdata.mbitsec = 0
                    else:
                        self.appdata.overall_size = overall_size
                        self.appdata.gbdown = gbdown0
                        self.appdata.mbitsec = mbitseccurr
                    self.update_liststore_dldata()
                    self.update_liststore(only_eta=True)
                    self.appdata.dldata = data
        return False

    def update_first_appdata_nzb(self):
        if self.appdata.nzbs:
            _, _, n_dl, n_size, _, _, _, _ = self.appdata.nzbs[0]
            self.appdata.gbdown = n_dl
            self.appdata.overall_size = n_size

    # set up pyplot for servergraph
    def setup_pyplot(self):
        self.ax = []
        self.lines = []
        try:
            for a in self.annotations:
                a.remove()
        except Exception:
            self.annotations = []
        self.appdata.active_servers = [(server_name, server_url, user, password, port, usessl, level, connections, retention, use_server, plstyle)
                                       for server_name, server_url, user, password, port, usessl, level, connections, retention, use_server, plstyle
                                       in self.settings_servers if use_server]
        no_servers = len(self.appdata.active_servers)
        self.servergraph_canvas.set_size_request(600, self.max_height * int((no_servers / 2.5)))
        for i, serversetting in enumerate(self.appdata.active_servers):
            server_name, server_url, user, password, port, usessl, level, connections, retention, useserver, plstyle = serversetting

            color, marker, linestyle = plstyle
            ax0 = self.servergraph_figure.add_subplot(no_servers, 1, i+1)
            lines0, = ax0.plot([], [], color=color, marker=marker, linestyle=linestyle, label=server_name)
            ax0.legend()
            ax0.set_autoscaley_on(True)
            ax0.xaxis.set_major_locator(SecondLocator(interval=5))
            ax0.xaxis.set_minor_locator(SecondLocator(interval=1))
            ax0.xaxis.set_major_formatter(DateFormatter('%S'))
            ax0.xaxis_date()
            ax0.fmt_xdata = DateFormatter('%S')
            self.ax.append(ax0)
            self.lines.append(lines0)

    def read_config(self):
        # read serversettings and assign colors
        settings_servers = get_server_config(self.cfg)
        i = 1
        for j, serversetting in enumerate(settings_servers):
            if j == 0:
                self.settings_servers = [("-ALL SERVERS-", None, None, None, None, None, None, None, None, True, (COLORCODES[i], MARKERS[i], LINESTYLES[i]))]
                i += 1
            server_name, server_url, user, password, port, usessl, level, connections, retention, useserver = serversetting
            self.settings_servers.append((server_name, server_url, user, password, port, usessl, level, connections,
                                          retention, useserver, (COLORCODES[i], MARKERS[i], LINESTYLES[i])))
            i += 1
            if (i+1) % 20 == 1:
                i = 0

        # update_delay
        try:
            self.update_delay = float(self.cfg["GTKGUI"]["UPDATE_DELAY"])
        except Exception as e:
            self.logger.warning(whoami() + str(e) + ", setting update_delay to default 0.5")
            self.update_delay = 0.5
        # host ip
        try:
            self.host = self.cfg["OPTIONS"]["HOST"]
        except Exception as e:
            self.logger.warning(whoami() + str(e) + ", setting host to default 127.0.0.1")
            self.host = "127.0.0.1"
        # host port
        try:
            self.port = self.cfg["OPTIONS"]["PORT"]
            assert(int(self.port) > 1024 and int(self.port) <= 65535)
        except Exception as e:
            self.logger.warning(whoami() + str(e) + ", setting port to default 36603")
            self.port = "36603"

    def toggle_buttons_history(self):
        one_is_selected = False
        for ls in range(len(self.nzbhistory_liststore)):
            path0 = Gtk.TreePath(ls)
            if self.nzbhistory_liststore[path0][0]:
                one_is_selected = True
                break
        for b in self.historybuttonlist:
            if one_is_selected:
                b.set_sensitive(True)
            else:
                b.set_sensitive(False)
        return False    # because of Glib.idle_add

    def toggle_buttons(self):
        one_is_selected = False
        for ls in range(len(self.nzblist_liststore)):
            path0 = Gtk.TreePath(ls)
            if self.nzblist_liststore[path0][6]:
                one_is_selected = True
                break
        for b in self.gridbuttonlist:
            if one_is_selected:
                b.set_sensitive(True)
            else:
                b.set_sensitive(False)
        return False    # because of Glib.idle_add

    def set_buttons_insensitive(self):
        for b in self.gridbuttonlist:
            b.set_sensitive(False)

    def set_historybuttons_insensitive(self):
        for b in self.historybuttonlist:
            b.set_sensitive(False)

    def remove_selected_from_list(self):
        old_first_nzb = self.appdata.nzbs[0]
        newnzbs = []
        for i, ro in enumerate(self.nzblist_liststore):
            if not ro[6]:
                newnzbs.append(self.appdata.nzbs[i])
        self.appdata.nzbs = newnzbs[:]
        if self.appdata.nzbs:
            if self.appdata.nzbs[0] != old_first_nzb:
                self.update_first_appdata_nzb()

    def get_selected_from_history(self):
        selected_list = [ro[1] for ro in self.nzbhistory_liststore if ro[0]]
        return selected_list

    def on_inverted_toggled_nzbhistory(self, widget, path):
        with self.lock:
            self.nzbhistory_liststore[path][0] = not self.nzbhistory_liststore[path][0]
            i = int(path)
            newnzb = list(self.appdata.nzbs_history[i])
            newnzb[0] = self.nzbhistory_liststore[path][0]
            self.appdata.nzbs_history[i] = tuple(newnzb)
            self.toggle_buttons_history()

    def on_inverted_toggled(self, widget, path):
        with self.lock:
            self.nzblist_liststore[path][6] = not self.nzblist_liststore[path][6]
            i = int(path)
            newnzb = list(self.appdata.nzbs[i])
            newnzb[6] = self.nzblist_liststore[path][6]
            self.appdata.nzbs[i] = tuple(newnzb)
            self.toggle_buttons()

    def on_activated_toggled_serverlist(self, widget, path):
        with self.lock:
            self.serverlist_liststore[path][1] = not self.serverlist_liststore[path][1]
            self.appdata.settings_changed = True
            # insert into cfg
            useserver = self.serverlist_liststore[path][1]
            servername = self.serverlist_liststore[path][0]
            snr = 1
            idx = 0
            serverfound = False
            while idx < 10:
                snrstr = "SERVER" + str(snr)
                if self.cfg[snrstr]["server_name"] == servername:
                    self.cfg[snrstr]["use_server"] = "yes" if useserver else "no"
                    serverfound = True
                    break
                snr += 1
                idx += 1
            if serverfound:
                self.server_apply_button.set_sensitive(True)

    def on_treeviewserverlist_selection_changed(self, selection):
        model, treeiter = selection.get_selected()
        if treeiter is not None:
            self.servergraph_selectedserver = model[treeiter][0]
            self.server_delete_button.set_sensitive(True)
            self.server_edit_button.set_sensitive(True)
        else:
            self.servergraph_selectedserver = None
            self.server_delete_button.set_sensitive(False)
            self.server_edit_button.set_sensitive(False)

    def update_logstore(self):
        # only show msgs for current nzb
        self.logs_liststore.clear()
        if not self.appdata.nzbname:
            return
        try:
            loglist = self.appdata.fulldata[self.appdata.nzbname]["msg"][:]
        except Exception:
            return
        for msg0, ts0, level0 in loglist:
            log_as_list = []
            # msg, level, tt, bg, fg
            log_as_list.append(get_cut_msg(msg0, MAX_MSG_LEN))
            log_as_list.append(level0)
            if level0 == 0:
                log_as_list.append("")
            else:
                log_as_list.append(str(datetime.datetime.fromtimestamp(ts0).strftime('%Y-%m-%d %H:%M:%S')))
            fg = "black"
            if log_as_list[1] == "info":
                bg = "royal Blue"
                fg = "white"
            elif log_as_list[1] == "warning":
                bg = "orange"
                fg = "white"
            elif log_as_list[1] == "error":
                bg = "red"
                fg = "white"
            elif log_as_list[1] == "success":
                bg = "green"
                fg = "white"
            else:
                bg = "white"
            log_as_list.append(bg)
            log_as_list.append(fg)
            self.logs_liststore.append(log_as_list)

    def update_serverlist_liststore(self, init=False):
        self.serverlist_liststore.clear()
        # server_name, server_url, user, password, port, usessl, level, connections, retention, useserver, plstyle
        for server_name, _, _, _, _, _, _, connections, retention, useserver, _ in self.settings_servers:
            servers_as_list = []
            if server_name != "-ALL SERVERS-":
                servers_as_list.append(server_name)
                servers_as_list.append(useserver)
                self.serverlist_liststore.append(servers_as_list)

    def update_nzbhistory_liststore(self):
        # appdata.nzbs_history = selected, n_name, n_status, n_siz, n_downloaded, color
        self.nzbhistory_liststore.clear()
        for i, nzb in enumerate(self.appdata.nzbs_history):
            nzb_as_list = list(nzb)
            nzb_as_list[1] = get_cut_nzbname(nzb_as_list[1], MAX_NZB_LEN)
            nzb_as_list[2], nzb_as_list[-1] = get_status_name_and_color(nzb[2])
            self.nzbhistory_liststore.append(nzb_as_list)

    def update_liststore(self, only_eta=False):
        # n_name, n_perc, n_dl, n_size, etastr, str(n_perc) + "%", selected, n_status))
        if only_eta:
            self.appdata.overall_eta = 0
            for i, nzb in enumerate(self.appdata.nzbs):
                # skip first one as it will be updated anyway
                if i == 0:
                    continue
                try:
                    path = Gtk.TreePath(i)
                    iter = self.nzblist_liststore.get_iter(path)
                except Exception as e:
                    self.logger.debug(whoami() + str(e))
                    continue
                eta0 = 0
                if self.appdata.mbitsec > 0 and self.dl_running:
                    overall_size = nzb[3]
                    gbdown = nzb[2]
                    eta0 = (((overall_size - gbdown) * 1024) / (self.appdata.mbitsec / 8))
                    if eta0 < 0:
                        eta0 = 0
                    try:
                        etastr = str(datetime.timedelta(seconds=int(eta0)))
                    except Exception:
                        etastr = "-"
                else:
                    etastr = "-"
                self.appdata.overall_eta += eta0
                self.nzblist_liststore.set_value(iter, 4, etastr)
            return

        self.nzblist_liststore.clear()
        for i, nzb in enumerate(self.appdata.nzbs):
            nzb_as_list = list(nzb)
            nzb_as_list[0] = get_cut_nzbname(nzb_as_list[0], MAX_NZB_LEN)
            n_status = nzb_as_list[7]
            if n_status == 0:
                n_status_s = "preprocessing"
            elif n_status == 1:
                n_status_s = "queued"
            elif n_status == 2:
                n_status_s = "downloading"
            elif n_status == 3:
                n_status_s = "postprocessing"
            elif n_status == 4:
                n_status_s = "success"
            elif n_status < 0:
                n_status_s = "failed"
            else:
                n_status_s = "unknown"
            # only set bgcolor for first row
            if i != 0:
                n_status_s = "idle(" + n_status_s + ")"
                bgcolor = "white"
            else:
                if not self.appdata.dl_running:
                    n_status_s = "paused"
                    bgcolor = "white"
                else:
                    bgcolor = get_bg_color(n_status_s)
            nzb_as_list[7] = n_status_s
            nzb_as_list.append(bgcolor)
            self.nzblist_liststore.append(nzb_as_list)

    def update_liststore_dldata(self):
        if len(self.nzblist_liststore) == 0:
            self.levelbar.set_value(0)
            self.mbitlabel2.set_text("- Mbit/s")
            self.no_queued_label.set_text("Queued: -")
            self.overall_eta_label.set_text("ETA: -")
            return
        path = Gtk.TreePath(0)
        iter = self.nzblist_liststore.get_iter(path)

        if self.appdata.overall_size > 0:
            n_perc = min(int((self.appdata.gbdown / self.appdata.overall_size) * 100), 100)
        else:
            n_perc = 0
        n_dl = self.appdata.gbdown
        n_size = self.appdata.overall_size

        if not self.appdata.dl_running:
            self.nzb_status_string = "paused"
            n_bgcolor = "white"
        else:
            n_bgcolor = get_bg_color(self.nzb_status_string)

        self.nzblist_liststore.set_value(iter, 1, n_perc)
        self.nzblist_liststore.set_value(iter, 2, n_dl)
        self.nzblist_liststore.set_value(iter, 3, n_size)
        self.nzblist_liststore.set_value(iter, 5, str(n_perc) + "%")
        self.nzblist_liststore.set_value(iter, 7, self.nzb_status_string)
        self.nzblist_liststore.set_value(iter, 8, n_bgcolor)
        etastr = "-"
        try:
            overall_etastr = str(datetime.timedelta(seconds=int(self.appdata.overall_eta)))
        except Exception:
            overall_etastr = "-"
        try:
            if self.appdata.mbitsec > 0 and self.dl_running:
                eta0 = (((self.appdata.overall_size - self.appdata.gbdown) * 1024) / (self.appdata.mbitsec / 8))
                if eta0 < 0:
                    eta0 = 0
                self.appdata.overall_eta += eta0
                overall_etastr = str(datetime.timedelta(seconds=int(self.appdata.overall_eta)))
                etastr = str(datetime.timedelta(seconds=int(eta0)))
        except Exception:
            pass
        self.nzblist_liststore.set_value(iter, 4, etastr)
        if len(self.appdata.nzbs) > 0:
            newnzb = (self.appdata.nzbs[0][0], n_perc, n_dl, n_size, etastr, str(n_perc) + "%", self.appdata.nzbs[0][6], self.appdata.nzbs[0][7])
            self.no_queued_label.set_text("Queued: " + str(len(self.appdata.nzbs)))
            self.appdata.nzbs[0] = newnzb
            if self.appdata.mbitsec > 0 and self.appdata.dl_running:
                self.levelbar.set_value(self.appdata.mbitsec / self.appdata.max_mbitsec)
                mbitsecstr = str(int(self.appdata.mbitsec)) + " MBit/s"
                self.mbitlabel2.set_text(mbitsecstr.rjust(11))
                self.overall_eta_label.set_text("ETA: " + overall_etastr)
            else:
                self.levelbar.set_value(0)
                self.mbitlabel2.set_text("- MBit/s")
                self.overall_eta_label.set_text("ETA: -")
        else:
            self.levelbar.set_value(0)
            self.mbitlabel2.set_text("")
            self.no_queued_label.set_text("Queued: -")
            self.overall_eta_label.set_text("ETA: -")


# ******************************************************************************************************
# *** Handler for buttons

class Handler:

    def __init__(self, gui):
        self.gui = gui

    def on_server_add_clicked(self, button):
        print("Add Server clicked")

    def on_apply_server_clicked(self, button):
        self.gui.restartall = True
        self.gui.app.quit()

    def on_cumulative_cb_toggled(self, a):
        self.gui.appdata.servergraph_cumulative = not self.gui.appdata.servergraph_cumulative

    def on_unitscombotext_changed(self, combo):
        text = combo.get_active_text()
        with self.gui.lock:
            try:
                self.gui.appdata.servergraph_unit = UNIT_DIC[text]
            except Exception:
                pass

    def on_frequencycombotext_changed(self, combo):
        text = combo.get_active_text()
        with self.gui.lock:
            try:
                self.gui.appdata.servergraph_freq = FREQ_DIC[text]
                for i, s in enumerate(self.gui.appdata.server_ts_diff):
                    ax0 = self.gui.ax[i]
                    if self.gui.appdata.servergraph_freq == "dlsec":
                        ax0.xaxis.set_major_locator(SecondLocator(interval=5))
                        ax0.xaxis.set_minor_locator(SecondLocator(interval=1))
                        ax0.xaxis.set_major_formatter(DateFormatter('%S'))
                        ax0.fmt_xdata = DateFormatter('%S')
                    elif self.gui.appdata.servergraph_freq == "dlmin":
                        ax0.xaxis.set_major_locator(MinuteLocator(interval=5))
                        ax0.xaxis.set_minor_locator(MinuteLocator(interval=1))
                        ax0.xaxis.set_major_formatter(DateFormatter('%H:%Mh'))
                        ax0.fmt_xdata = DateFormatter('%H:%Mh')
                    elif self.gui.appdata.servergraph_freq == "dlh":
                        ax0.xaxis.set_major_locator(HourLocator(interval=5))
                        ax0.xaxis.set_minor_locator(HourLocator(interval=1))
                        ax0.xaxis.set_major_formatter(DateFormatter('%d-%m %Hh'))
                        ax0.fmt_xdata = DateFormatter('%d-%m %Hh')
                    elif self.gui.appdata.servergraph_freq == "dld":
                        ax0.xaxis.set_major_locator(DayLocator(interval=5))
                        ax0.xaxis.set_minor_locator(DayLocator(interval=1))
                        ax0.xaxis.set_major_formatter(DateFormatter('%d-%m'))
                        ax0.fmt_xdata = DateFormatter('%d-%m')
            except Exception:
                pass

    def on_stack_done_draw(self, a, b):
        with self.gui.lock:
            self.gui.activestack = "history"

    def on_stack_servers_draw(self, a, b):
        with self.gui.lock:
            self.gui.activestack = "servergraphs"

    def on_stack_downloading_draw(self, a, b):
        with self.gui.lock:
            self.gui.activestack = "downloading"

    def on_stack_settings_draw(self, a, b):
        with self.gui.lock:
            self.gui.activestack = "settings"

    def on_button_pause_resume_clicked(self, button):
        with self.gui.lock:
            self.gui.appdata.dl_running = not self.gui.appdata.dl_running
        self.gui.guiqueue.put(("dl_running", self.gui.appdata.dl_running))
        if self.gui.appdata.dl_running:
            icon = Gio.ThemedIcon(name="media-playback-pause")
            image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button.set_image(image)
        else:
            icon = Gio.ThemedIcon(name="media-playback-start")
            image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button.set_image(image)
        self.gui.update_liststore_dldata()
        self.gui.update_liststore()

    def on_button_settings_clicked(self, button):
        print("settings clicked")

    def on_button_top_clicked(self, button):
        with self.gui.lock:
            old_first_nzb = self.gui.appdata.nzbs[0]
            newnzbs = []
            for i, ro in enumerate(self.gui.nzblist_liststore):
                if ro[6]:
                    newnzbs.append(self.gui.appdata.nzbs[i])
            for i, ro in enumerate(self.gui.nzblist_liststore):
                if not ro[6]:
                    newnzbs.append(self.gui.appdata.nzbs[i])
            self.gui.appdata.nzbs = newnzbs[:]
            if self.gui.appdata.nzbs[0] != old_first_nzb:
                self.gui.update_first_appdata_nzb()
            self.gui.update_liststore()
            self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("order_changed", None))

    def on_button_bottom_clicked(self, button):
        with self.gui.lock:
            old_first_nzb = self.gui.appdata.nzbs[0]
            newnzbs = []
            for i, ro in enumerate(self.gui.nzblist_liststore):
                if not ro[6]:
                    newnzbs.append(self.gui.appdata.nzbs[i])
            for i, ro in enumerate(self.gui.nzblist_liststore):
                if ro[6]:
                    newnzbs.append(self.gui.appdata.nzbs[i])
            self.gui.appdata.nzbs = newnzbs[:]
            if self.gui.appdata.nzbs[0] != old_first_nzb:
                self.gui.update_first_appdata_nzb()
            self.gui.update_liststore()
            self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("order_changed", None))

    def on_button_up_clicked(self, button):
        do_update_dldata = False
        with self.gui.lock:
            old_first_nzb = self.gui.appdata.nzbs[0]
            ros = [(i, self.gui.appdata.nzbs[i]) for i, ro in enumerate(self.gui.nzblist_liststore) if ro[6]]
            for i, r in ros:
                if i == 1:
                    do_update_dldata = True
                if i == 0:
                    break
                oldval = self.gui.appdata.nzbs[i - 1]
                self.gui.appdata.nzbs[i - 1] = r
                self.gui.appdata.nzbs[i] = oldval
            if self.gui.appdata.nzbs[0] != old_first_nzb:
                self.gui.update_first_appdata_nzb()
            self.gui.update_liststore()
            if do_update_dldata:
                self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("order_changed", None))

    def on_button_down_clicked(self, button):
        do_update_dldata = False
        with self.gui.lock:
            old_first_nzb = self.gui.appdata.nzbs[0]
            ros = [(i, self.gui.appdata.nzbs[i]) for i, ro in enumerate(self.gui.nzblist_liststore) if ro[6]]
            for i, r in reversed(ros):
                if i == 0:
                    do_update_dldata = True
                if i == len(self.gui.appdata.nzbs) - 1:
                    break
                oldval = self.gui.appdata.nzbs[i + 1]
                self.gui.appdata.nzbs[i + 1] = r
                self.gui.appdata.nzbs[i] = oldval
            if self.gui.appdata.nzbs[0] != old_first_nzb:
                self.gui.update_first_appdata_nzb()
            self.gui.update_liststore()
            if do_update_dldata:
                self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("order_changed", None))

    def on_button_nzbadd_clicked(self, button):
        dialog = Gtk.FileChooserDialog("Choose NZB file(s)", self.gui.window, Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        Gtk.FileChooser.set_select_multiple(dialog, True)
        self.add_nzb_filters(dialog)
        nzb_selected = None
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            button.set_sensitive(False)
            nzb_selected = dialog.get_filenames()
            self.gui.guiqueue.put(("nzb_added", (nzb_selected, button)))
        elif response == Gtk.ResponseType.CANCEL:
            pass
        dialog.destroy()

    def add_nzb_filters(self, dialog):
        filter_text = Gtk.FileFilter()
        filter_text.set_name("NZB files")
        filter_text.add_mime_type("application/x-nzb")
        dialog.add_filter(filter_text)

        filter_any = Gtk.FileFilter()
        filter_any.set_name("Any files")
        filter_any.add_pattern("*")
        dialog.add_filter(filter_any)

    def on_button_interrupt_clicked(self, button):
        dialog = ConfirmDialog(self.gui.window, "Interrupt Download", "Do you really want to move these NZBs to history?")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.CANCEL:
            return
        # here the same as delete
        with self.gui.lock:
            self.gui.remove_selected_from_list()
            self.gui.update_liststore()
            self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("interrupted", None))
        # msg an main: params = newnbzlist, moved_nzbs
        # dann dort: wie "SET_NZB_ORDER", nur ohne delete sondern status change

    def on_button_delete_clicked(self, button):
        # todo: appdata.nzbs -> update_liststore
        dialog = ConfirmDialog(self.gui.window, "Delete NZB(s)", "Do you really want to delete these NZBs ?")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.CANCEL:
            return
        with self.gui.lock:
            self.gui.remove_selected_from_list()
            self.gui.update_liststore()
            self.gui.update_liststore_dldata()
            self.gui.set_buttons_insensitive()
            self.gui.guiqueue.put(("order_changed", None))

    # button: reprocess from last
    def on_button_hist_process_last_clicked(self, button):
        with self.gui.lock:
            selected_nzbs = self.gui.get_selected_from_history()
            self.gui.set_historybuttons_insensitive()
            self.gui.guiqueue.put(("reprocess_from_last", selected_nzbs))

    def on_button_hist_delete_clicked(self, button):
        dialog = ConfirmDialog(self.gui.window, "Delete NZB(s)", "Do you really want to delete these NZBs form history?")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.CANCEL:
            return
        with self.gui.lock:
            selected_nzbs = self.gui.get_selected_from_history()
            self.gui.set_historybuttons_insensitive()
            self.gui.guiqueue.put(("deleted_from_history", selected_nzbs))

    # button: reprocess from start
    def on_button_hist_process_start_clicked(self, button):
        with self.gui.lock:
            selected_nzbs = self.gui.get_selected_from_history()
            self.gui.set_historybuttons_insensitive()
            self.gui.guiqueue.put(("reprocess_from_start", selected_nzbs))

    def on_win_destroy(self, *args):
        pass


# ******************************************************************************************************
# *** Addtl stuff

class ConfirmDialog(Gtk.Dialog):
    def __init__(self, parent, title, txt):
        Gtk.Dialog.__init__(self, title, parent, 9, (Gtk.STOCK_OK, Gtk.ResponseType.OK,
                                                     Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL))
        self.set_default_size(150, 100)
        self.set_border_width(10)
        self.set_modal(True)
        # self.set_property("button-spacing", 10)
        label = Gtk.Label(txt)
        box = self.get_content_area()
        box.add(label)
        self.show_all()


class AppData:
    def __init__(self, lock):
        self.lock = lock
        self.mbitsec = 0
        self.nzbs = []
        self.overall_size = 0
        self.gbdown = 0
        self.dl_running = True
        self.max_mbitsec = 0
        # crit_art_health is taken from server
        self.crit_art_health = 0.95
        self.crit_conn_health = 0.5
        self.sortednzblist = None
        self.sortednzbhistorylist = None
        self.dldata = None
        self.logdata = None
        self.article_health = 0
        self.connection_health = 0
        self.fulldata = None
        self.closeall = False
        self.nzbs_history = []
        self.nzbs_history = [(False, "Test.nzb", 3, 1.5, 1.4, 0.9, "white")]
        self.overall_eta = 0
        self.server_ts_diff = {}
        self.server_ts = {}
        self.servergraph_freq = list(FREQ_DIC.items())[0][1]
        self.servergraph_unit = list(UNIT_DIC.items())[0][1]
        self.servergraph_cumulative = False
        self.servergraph_selectedserver = None
        self.settings_changed = False
