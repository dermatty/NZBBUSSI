import inotify_simple
import os
import signal
import gi
import threading
import datetime
import zmq
import time
import inspect
from threading import Thread
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gio, Gdk, GdkPixbuf, GLib, Pango

lpref = __name__.split("lib.")[-1] + " - "

__appname__ = "Ginzibix"
__version__ = "0.01 pre-alpha"
__author__ = "dermatty"

GBXICON = "lib/gzbx1.png"

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


def whoami():
    return str(inspect.getouterframes(inspect.currentframe())[1].function)


def get_bg_color(n_status_s):
    bgcol = "white"
    if n_status_s == "preprocessing":
        bgcol = "beige"
    elif n_status_s == "queued":
        bgcol = "khaki"
    elif n_status_s == "downloading":
        bgcol = "yellow"
    elif n_status_s == "postprocessing":
        bgcol = "yellow green"
    elif n_status_s == "success":
        bgcol = "lime green"
    elif n_status_s == "failed" or n_status_s == "unknown":
        bgcol = "red"
    else:
        bgcol = "white"
    return bgcol


class ConfirmDialog(Gtk.Dialog):
    def __init__(self, parent, txt):
        Gtk.Dialog.__init__(self, "My Dialog", parent, 9, (Gtk.STOCK_OK, Gtk.ResponseType.OK,
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
        self.nzbname = None
        self.overall_size = 0
        self.gbdown = 0
        self.servers = [("EWEKA", 40), ("BUCKETNEWS", 15), ("TWEAK", 0)]
        self.dl_running = True
        self.order_changed = False
        self.mbit_min = 0
        self.mbit_max = 150
        self.old_mbitdown = 0
        self.old_mbitdown_tt = 0
        self.mbitdownlist = []


class AppWindow(Gtk.ApplicationWindow):

    def __init__(self, app, mpp_main, dirs, logger):
        # data
        self.logger = logger
        self.dirs = dirs
        self.lock = threading.Lock()
        self.liststore = None
        self.liststore_s = None
        self.mbitlabel2 = None
        self.single_selected = None
        self.mpp_main = mpp_main
        self.appdata = AppData(self.lock)
        self.dl_running = True
        self.nzb_status_string = ""

        self.win = Gtk.Window.__init__(self, title=__appname__, application=app)

        self.connect("destroy", self.closeall)

        try:
            self.set_icon_from_file(GBXICON)
        except GLib.GError as e:
            print("Cannot find icon file!" + GBXICON)

        self.lock = threading.Lock()
        self.guipoller = GUI_Poller(self.lock, self.appdata, self.update_mainwindow_dl, self.update_mainwindow_sortednzbs, self.logger, port="36603")
        self.guipoller.start()

        # self.filepoller = File_Poller(self.lock, self.appdata, self.update_mainwindow, self.logger)
        # self.filepoller.start()

        # init main window
        self.set_border_width(10)
        # self.set_default_size(600, 200)
        self.set_wmclass(__appname__, __appname__)
        self.header_bar()
        box_main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(box_main)

        # stack
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(200)

        self.stacknzb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        stack.add_titled(self.stacknzb_box, "nzbs", "NZBs")
        self.show_nzb_stack(self.stacknzb_box)
        self.stackdetails_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=32)
        stack.add_titled(self.stackdetails_box, "database", "DataBase")
        self.stacklogs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=32)
        stack.add_titled(self.stacklogs_box, "logs", "Logs")
        self.stacksearch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=32)
        stack.add_titled(self.stacksearch_box, "search", "Search")

        stack_switcher = Gtk.StackSwitcher()
        stack_switcher.set_stack(stack)
        stack_switcher.set_property("halign", Gtk.Align.CENTER)
        stack_switcher.set_property("valign", Gtk.Align.START)
        box_main.pack_start(stack_switcher, False, False, 0)

        self.box_levelbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.box_levelbar.set_property("margin-left", 20)
        self.box_levelbar.set_property("margin-right", 20)
        box_main.pack_start(self.box_levelbar, False, False, 10)

        self.levelbar = Gtk.LevelBar()
        self.levelbar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self.levelbar.set_min_value(self.appdata.mbit_min)
        self.levelbar.set_max_value(self.appdata.mbit_max)
        self.levelbar.set_value(0)
        self.box_levelbar.pack_start(self.levelbar, True, True, 0)
        self.mbitlabel2 = Gtk.Label(None)
        if self.appdata.mbitsec > 0:
            mbitstr = str(int(self.appdata.mbitsec)) + " MBit/s"
            self.mbitlabel2.set_text(mbitstr.rjust(11))
        else:
            self.mbitlabel2.set_text("")
        self.box_levelbar.pack_start(self.mbitlabel2, False, False, 0)

        box_main.pack_start(stack, True, True, 0)

    def show_nzb_stack(self, stacknzb_box):
        # scrolled window
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_border_width(10)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_property("min-content-height", 380)
        stacknzb_box.pack_start(scrolled_window, True, True, 0)
        # listbox
        listbox = Gtk.ListBox()
        row = Gtk.ListBoxRow()
        # populate liststore
        self.liststore = Gtk.ListStore(str, int, float, float, str, str, bool, str, str)
        self.update_liststore()
        # set treeview + actions
        treeview = Gtk.TreeView(model=self.liststore)
        treeview.set_reorderable(True)
        treeview.get_selection().connect("changed", self.on_selection_changed)
        # 0th selection toggled
        renderer_toggle = Gtk.CellRendererToggle()
        renderer_toggle.connect("toggled", self.on_inverted_toggled)
        column_toggle = Gtk.TreeViewColumn("Select", renderer_toggle, active=6)
        treeview.append_column(column_toggle)
        # 1st column: NZB name
        renderer_text0 = Gtk.CellRendererText()
        column_text0 = Gtk.TreeViewColumn("NZB name", renderer_text0, text=0)
        column_text0.set_expand(True)
        treeview.append_column(column_text0)
        # 2nd: progressbar
        renderer_progress = Gtk.CellRendererProgress()
        column_progress = Gtk.TreeViewColumn("Progress", renderer_progress, value=1, text=5)
        column_progress.set_min_width(260)
        column_progress.set_expand(True)
        treeview.append_column(column_progress)
        # 3rd downloaded GiN
        renderer_text1 = Gtk.CellRendererText()
        column_text1 = Gtk.TreeViewColumn("Downloaded", renderer_text1, text=2)
        column_text1.set_cell_data_func(renderer_text1, lambda col, cell, model, iter, unused:
                                        cell.set_property("text", "{0:.2f}".format(model.get(iter, 2)[0]) + " GiB"))
        treeview.append_column(column_text1)
        # 4th overall GiB
        renderer_text2 = Gtk.CellRendererText()
        column_text2 = Gtk.TreeViewColumn("Overall", renderer_text2, text=3)
        column_text2.set_cell_data_func(renderer_text2, lambda col, cell, model, iter, unused:
                                        cell.set_property("text", "{0:.2f}".format(model.get(iter, 3)[0]) + " GiB"))
        column_text2.set_min_width(80)
        treeview.append_column(column_text2)
        # 5th Eta
        renderer_text3 = Gtk.CellRendererText()
        column_text3 = Gtk.TreeViewColumn("Eta", renderer_text3, text=4)
        column_text3.set_min_width(80)
        treeview.append_column(column_text3)
        # 7th status
        renderer_text7 = Gtk.CellRendererText()
        column_text7 = Gtk.TreeViewColumn("Status", renderer_text7, text=7, background=8)
        column_text7.set_min_width(80)
        treeview.append_column(column_text7)
        
        # final
        row.add(treeview)
        listbox.add(row)
        scrolled_window.add(listbox)
        
        # box for record/stop/.. selected
        box_media = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box_media.set_property("margin-left", 8)
        box_media.set_property("margin-right", 8)
        box_media_expand = False
        box_media_fill = False
        box_media_padd = 1
        stacknzb_box.pack_start(box_media, box_media_expand, box_media_fill, box_media_padd)
        self.gridbuttonlist = []
        # button full up
        button_full_up = Gtk.Button(sensitive=False)
        button_full_up.set_size_request(50, 20)
        icon1 = Gio.ThemedIcon(name="arrow-up-double")
        image1 = Gtk.Image.new_from_gicon(icon1, Gtk.IconSize.BUTTON)
        button_full_up.add(image1)
        button_full_up.connect("clicked", self.on_buttonfullup_clicked)
        box_media.pack_start(button_full_up, box_media_expand, box_media_fill, box_media_padd)
        button_full_up.set_tooltip_text("Move NZB(s) to top")
        self.gridbuttonlist.append(button_full_up)
        # button up
        button_up = Gtk.Button(sensitive=False)
        icon4 = Gio.ThemedIcon(name="arrow-up")
        image4 = Gtk.Image.new_from_gicon(icon4, Gtk.IconSize.BUTTON)
        button_up.add(image4)
        button_up.connect("clicked", self.on_buttonup_clicked)
        box_media.pack_start(button_up, box_media_expand, box_media_fill, box_media_padd)
        button_up.set_tooltip_text("Move NZB(s) 1 up")
        self.gridbuttonlist.append(button_up)
        # button down
        button_down = Gtk.Button(sensitive=False)
        icon3 = Gio.ThemedIcon(name="arrow-down")
        image3 = Gtk.Image.new_from_gicon(icon3, Gtk.IconSize.BUTTON)
        button_down.add(image3)
        button_down.connect("clicked", self.on_buttondown_clicked)
        box_media.pack_start(button_down, box_media_expand, box_media_fill, box_media_padd)
        button_down.set_tooltip_text("Move NZB(s) 1 down")
        self.gridbuttonlist.append(button_down)
        # button full down
        button_full_down = Gtk.Button(sensitive=False)
        button_full_down.set_size_request(50, 20)
        icon2 = Gio.ThemedIcon(name="arrow-down-double")
        image2 = Gtk.Image.new_from_gicon(icon2, Gtk.IconSize.BUTTON)
        button_full_down.add(image2)
        button_full_down.connect("clicked", self.on_buttonfulldown_clicked)
        box_media.pack_start(button_full_down, box_media_expand, box_media_fill, box_media_padd)
        button_full_down.set_tooltip_text("Move NZB(s) to bottom")
        self.gridbuttonlist.append(button_full_down)
        # delete
        button_delete = Gtk.Button(sensitive=False)
        icon6 = Gio.ThemedIcon(name="gtk-delete")
        image6 = Gtk.Image.new_from_gicon(icon6, Gtk.IconSize.BUTTON)
        button_delete.add(image6)
        button_delete.connect("clicked", self.on_buttondelete_clicked)
        box_media.pack_end(button_delete, box_media_expand, box_media_fill, box_media_padd)
        button_delete.set_tooltip_text("Delete NZB(s)")
        self.gridbuttonlist.append(button_delete)
        # add
        button_add = Gtk.Button(sensitive=True)
        icon7 = Gio.ThemedIcon(name="list-add")
        image7 = Gtk.Image.new_from_gicon(icon7, Gtk.IconSize.BUTTON)
        button_add.add(image7)
        button_add.set_tooltip_text("Add NZB from File")
        box_media.pack_end(button_add, box_media_expand, box_media_fill, box_media_padd)

        '''# listbox / treeview for server speed
        scrolled_window_s = Gtk.ScrolledWindow()
        scrolled_window_s.set_border_width(2)
        scrolled_window_s.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # scrolled_window_s.set_property("min-content-height", 30)
        grid.attach(scrolled_window_s, 22, 0, 80, 3)

        # listbox for server speeds
        listbox_s = Gtk.ListBox()
        row_s = Gtk.ListBoxRow()
        self.liststore_s = Gtk.ListStore(str, int)
        for i, server in enumerate(self.appdata.servers):
            if i == 0:
                self.current_iter = self.liststore_s.append(list(server))
            else:
                self.liststore_s.append(list(server))
        treeview_s = Gtk.TreeView(model=self.liststore_s)
        renderer_text_s = Gtk.CellRendererText()
        column_text_s = Gtk.TreeViewColumn(None, renderer_text_s, text=0)
        custom_header = Gtk.Label('Server Name')
        column_text_s.set_widget(custom_header)
        column_text_s.get_widget().override_font(Pango.FontDescription.from_string('10'))
        column_text_s.get_widget().show_all()

        column_text_s.set_cell_data_func(renderer_text_s, lambda col, cell, model, iter, unused:
                                         cell.set_property("scale", 0.8))
        column_text_s.set_expand(True)
        treeview_s.append_column(column_text_s)

        renderer_text_s2 = Gtk.CellRendererText()
        column_text_s2 = Gtk.TreeViewColumn("Speed Mbit/s", renderer_text_s2, text=1)
        custom_header1 = Gtk.Label('Speed Mbit/s')
        column_text_s2.set_widget(custom_header1)
        column_text_s2.get_widget().override_font(Pango.FontDescription.from_string('10'))
        column_text_s2.get_widget().show_all()
        column_text_s2.set_cell_data_func(renderer_text_s2, lambda col, cell, model, iter, unused:
                                          cell.set_property("scale", 0.8))
        column_text_s2.set_expand(True)
        treeview_s.append_column(column_text_s2)

        row_s.add(treeview_s)
        listbox_s.add(row_s)
        scrolled_window_s.add(listbox_s)'''

    def on_buttondelete_clicked(self, button):
        # todo: appdata.nzbs -> update_liststore
        dialog = ConfirmDialog(self, "Do you really want to delete these NZBs ?")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.CANCEL:
            return
        with self.lock:
            newnzbs = []
            for i, ro in enumerate(self.liststore):
                if not ro[6]:
                    newnzbs.append(self.appdata.nzbs[i])
            self.appdata.nzbs = newnzbs[:]
            self.update_liststore()
            self.toggle_buttons()
            self.appdata.order_changed = True

    def on_buttonup_clicked(self, button):
        with self.lock:
            ros = [(i, self.appdata.nzbs[i]) for i, ro in enumerate(self.liststore) if ro[6]]
            for i, r in ros:
                if i == 0:
                    break
                oldval = self.appdata.nzbs[i - 1]
                self.appdata.nzbs[i - 1] = r
                self.appdata.nzbs[i] = oldval
            # self.update_liststore()
            self.appdata.order_changed = True

    def on_buttondown_clicked(self, button):
        with self.lock:
            ros = [(i, self.appdata.nzbs[i]) for i, ro in enumerate(self.liststore) if ro[6]]
            for i, r in reversed(ros):
                if i == len(self.appdata.nzbs) - 1:
                    break
                oldval = self.appdata.nzbs[i + 1]
                self.appdata.nzbs[i + 1] = r
                self.appdata.nzbs[i] = oldval
            # self.update_liststore()
            self.appdata.order_changed = True

    def on_buttonfullup_clicked(self, button):
        with self.lock:
            newnzbs = []
            for i, ro in enumerate(self.liststore):
                if ro[6]:
                    newnzbs.append(self.appdata.nzbs[i])
            for i, ro in enumerate(self.liststore):
                if not ro[6]:
                    newnzbs.append(self.appdata.nzbs[i])
            self.appdata.nzbs = newnzbs[:]
            # self.update_liststore()
            self.appdata.order_changed = True

    def on_buttonfulldown_clicked(self, button):
        with self.lock:
            newnzbs = []
            for i, ro in enumerate(self.liststore):
                if not ro[6]:
                    newnzbs.append(self.appdata.nzbs[i])
            for i, ro in enumerate(self.liststore):
                if ro[6]:
                    newnzbs.append(self.appdata.nzbs[i])
            self.appdata.nzbs = newnzbs[:]
            # self.update_liststore()
            self.appdata.order_changed = True

    def on_inverted_toggled(self, widget, path):
        with self.lock:
            self.liststore[path][6] = not self.liststore[path][6]
            i = int(path)
            newnzb = list(self.appdata.nzbs[i])
            newnzb[6] = self.liststore[path][6]
            self.appdata.nzbs[i] = tuple(newnzb)
            self.toggle_buttons()

    def update_liststore(self, only_eta=False):
        # n_name, n_perc, n_dl, n_size, etastr, str(n_perc) + "%", selected, n_status))
        if only_eta:
            for i, nzb in enumerate(self.appdata.nzbs):
                # skip first one as it will be updated anyway
                if i == 0:
                    continue
                try:
                    path = Gtk.TreePath(i)
                    iter = self.liststore.get_iter(path)
                except Exception as e:
                    print(str(e))
                    continue
                if self.appdata.mbitsec > 0 and self.dl_running:
                    overall_size = nzb[3]
                    gbdown = nzb[2]
                    eta0 = (((overall_size - gbdown) * 1024) / (self.appdata.mbitsec / 8))
                    etastr = str(datetime.timedelta(seconds=int(eta0)))
                else:
                    etastr = "-"
                self.liststore.set_value(iter, 4, etastr)
            return

        self.liststore.clear()
        for i, nzb in enumerate(self.appdata.nzbs):
            nzb_as_list = list(nzb)
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
            if i == 0:
                self.current_iter = self.liststore.append(nzb_as_list)
            else:
                self.liststore.append(nzb_as_list)

    def update_liststore_dldata(self):
        if len(self.liststore) == 0:
            return
        path = Gtk.TreePath(0)
        iter = self.liststore.get_iter(path)

        if self.appdata.overall_size > 0:
            n_perc = min(int((self.appdata.gbdown / self.appdata.overall_size) * 100), 100)
        else:
            n_perc = 0
        # print(">>>" + str(n_perc))
        n_dl = self.appdata.gbdown
        n_size = self.appdata.overall_size

        if not self.appdata.dl_running:
            self.nzb_status_string = "paused"
            n_bgcolor = "white"
        else:
            n_bgcolor = get_bg_color(self.nzb_status_string)

        self.liststore.set_value(iter, 1, n_perc)
        self.liststore.set_value(iter, 2, n_dl)
        self.liststore.set_value(iter, 3, n_size)
        self.liststore.set_value(iter, 5, str(n_perc) + "%")
        self.liststore.set_value(iter, 7, self.nzb_status_string)
        self.liststore.set_value(iter, 8, n_bgcolor)

        # print(self.liststore.get_value(iter, 5))
        if self.appdata.mbitsec > 0 and self.dl_running:
            eta0 = (((self.appdata.overall_size - self.appdata.gbdown) * 1024) / (self.appdata.mbitsec / 8))
            etastr = str(datetime.timedelta(seconds=int(eta0)))
        else:
            etastr = "-"
        self.liststore.set_value(iter, 4, etastr)
        # (n_name, n_perc, n_dl, n_size, etastr, str(n_perc) + "%", selected))
        newnzb = (self.appdata.nzbs[0][0], n_perc, n_dl, n_size, etastr, str(n_perc) + "%", self.appdata.nzbs[0][6], self.appdata.nzbs[0][7])
        self.appdata.nzbs[0] = newnzb

        # levelbar_is_hidden = not self.box_levelbar.get_property("visible")
        # if self.appdata.mbitsec == 0 and not levelbar_is_hidden:
        #    self.box_levelbar.hide()
        # if self.appdata.mbitsec > 0:
        if self.appdata.mbitsec > 0 and self.appdata.dl_running:
            self.levelbar.set_value(int(self.appdata.mbitsec))
            mbitsecstr = str(int(self.appdata.mbitsec)) + " MBit/s"
            self.mbitlabel2.set_text(mbitsecstr.rjust(11))
        else:
            self.levelbar.set_value(0)
            self.mbitlabel2.set_text("")
            # if levelbar_is_hidden:
            #    self.box_levelbar.show_all()

    def toggle_buttons(self):
        one_is_selected = False
        if not one_is_selected:
            for ls in range(len(self.liststore)):
                path0 = Gtk.TreePath(ls)
                if self.liststore[path0][6]:
                    one_is_selected = True
                    break
        for b in self.gridbuttonlist:
            if one_is_selected:
                b.set_sensitive(True)
            else:
                b.set_sensitive(False)

    def on_selection_changed(self, selection):
        (model, iter) = selection.get_selected()

    def header_bar(self):
        hb = Gtk.HeaderBar(spacing=20)
        hb.set_show_close_button(True)
        hb.props.title = __appname__
        self.set_titlebar(hb)

        button_startstop = Gtk.Button()
        button_startstop.set_property("margin-left", 2)
        icon = Gio.ThemedIcon(name="media-playback-pause")
        image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
        button_startstop.add(image)
        button_startstop.connect("clicked", self.on_buttonstartstop_clicked)
        button_startstop.set_tooltip_text("Pause download")
        hb.pack_start(button_startstop)

        button_settings = Gtk.Button()
        icon2 = Gio.ThemedIcon(name="open-menu")
        image2 = Gtk.Image.new_from_gicon(icon2, Gtk.IconSize.BUTTON)
        button_settings.add(image2)
        # button_settings.connect("clicked", self.on_buttonsettings_clicked)
        button_settings.set_tooltip_text("Settings")
        hb.pack_end(button_settings)

    def on_buttonstartstop_clicked(self, button):
        with self.lock:
            self.appdata.dl_running = not self.appdata.dl_running
        if self.appdata.dl_running:
            icon = Gio.ThemedIcon(name="media-playback-pause")
            image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button.set_image(image)
        else:
            icon = Gio.ThemedIcon(name="media-playback-start")
            image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button.set_image(image)
        self.update_liststore_dldata()
        self.update_liststore()

    def closeall(self, a):
        # Gtk.main_quit()
        if self.mpp_main:
            os.kill(self.mpp_main.pid, signal.SIGTERM)
            self.mpp_main.join()

    def update_mainwindow_sortednzbs(self, sortednzblist0):
        if sortednzblist0:
            if sortednzblist0[0] == -1:
                sortednzblist0 = []
            # sort again just to make sure
            sortednzblist = sorted(sortednzblist0, key=lambda prio: prio[1])

            self.logger.debug(lpref + str(sortednzblist))
            self.logger.debug(lpref + "got new sortedlist")
            self.logger.debug(lpref + str(self.appdata.nzbs))
            self.logger.debug(lpref + str(sortednzblist))
            gibdivisor = (1024 * 1024 * 1024)
            do_update_list = False
            if len(self.appdata.nzbs) != len(sortednzblist):
                do_update_list = True
            else:
                # check if displayed data has to be updated
                for i, nzbdata in enumerate(sortednzblist):
                    n_name, n_prio, n_ts, n_status, n_siz, n_downloaded = nzbdata
                    n_name0, n_perc0, n_dl0, n_size0, hstr0, percstr0, sel0, stat0 = self.appdata.nzbs[i]
                    if n_name != n_name0 or n_siz != n_size0:
                        do_update_list = True
                        break
            # if yes: update liststore
            if do_update_list:
                nzbs_copy = self.appdata.nzbs.copy()
                self.appdata.nzbs = []
                for idx1, (n_name, n_prio, n_ts, n_status, n_siz, n_downloaded) in enumerate(sortednzblist):
                    # if first nzb is unchanged just change size in case
                    if False:   # nzbs_copy and idx1 == 0 and n_name == nzbs_copy[0][0]:
                        n_name0, n_perc0, n_dl0, n_size0, etastr0, n_percstr0, selected0, status0 = nzbs_copy[0]
                        self.appdata.nzbs.append((n_name0, n_perc0, n_dl0, n_siz / gibdivisor, etastr0, n_percstr0, selected0, status0))
                    else:
                        try:
                            n_perc = min(int((n_downloaded/n_siz) * 100), 100)
                        except ZeroDivisionError:
                            n_perc = 0
                        n_dl = n_downloaded / gibdivisor
                        n_size = n_siz / gibdivisor
                        if self.appdata.mbitsec > 0 and self.dl_running:
                            eta0 = (((n_size - n_dl) * 1024) / (self.appdata.mbitsec / 8))
                            etastr = str(datetime.timedelta(seconds=int(eta0)))
                        else:
                            etastr = "-"
                        selected = False
                        for n_name0, n_perc0, n_dl0, n_size0, etastr0, n_percstr0, selected0, status0 in nzbs_copy:
                            if n_name0 == n_name:
                                selected = selected0
                        self.appdata.nzbs.append((n_name, n_perc, n_dl, n_size, etastr, str(n_perc) + "%", selected, n_status))
                if nzbs_copy != self.appdata.nzbs:
                    self.update_liststore()
        return False

    def update_mainwindow_dl(self, data, pwdb_msg, server_config, threads, dl_running, nzb_status_string, netstat_mbitcur):
        nzbname = None
        if data:
            bytescount00, availmem00, avgmiblist00, filetypecounter00, nzbname, article_health, overall_size, already_downloaded_size = data
            mbitseccurr = 0
            # calc gbdown, mbitsec_avg
            gbdown0 = 0
            mbitdown_bandw = 0
            for t_bytesdownloaded, t_last_timestamp, t_idn, t_bandwbytes in threads:
                gbdown = t_bytesdownloaded / (1024 * 1024 * 1024)
                gbdown0 += gbdown
                mbitdown_bandw += t_bandwbytes / (1024 * 1024) * 8
            gbdown0 += already_downloaded_size
            mbitdown_delta = max(mbitdown_bandw - self.appdata.old_mbitdown, 0)
            self.appdata.old_mbitdown = mbitdown_bandw
            mbitdown_dt = max(time.time() - self.appdata.old_mbitdown_tt, 0.01)
            self.appdata.old_mbitdown_tt = time.time()
            mbitseccurr0 = mbitdown_delta / mbitdown_dt
            self.appdata.mbitdownlist.append(mbitseccurr0)
            if len(self.appdata.mbitdownlist) > 5:
                del self.appdata.mbitdownlist[0]
            mbitseccurr = netstat_mbitcur    # mean(self.appdata.mbitdownlist)
            if not dl_running:
                # mbitseccurr = 0
                self.dl_running = False
            else:
                self.dl_running = True
            self.nzb_status_string = nzb_status_string
            with self.lock:
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

        return False


class Application(Gtk.Application):

    def __init__(self, mpp_main, dirs, logger):
        Gtk.Application.__init__(self)
        self.mpp_main = mpp_main
        self.window = None
        self.logger = logger
        self.dirs = dirs

    def do_activate(self):
        self.window = AppWindow(self, self.mpp_main, self.dirs, self.logger)
        self.window.show_all()
        # self.window.box_levelbar.hide()

    def do_startup(self):
        Gtk.Application.do_startup(self)

        action = Gio.SimpleAction.new("settings", None)
        action.connect("activate", self.on_settings)
        self.add_action(action)

        action = Gio.SimpleAction.new("about", None)
        action.connect("activate", self.on_about)
        self.add_action(action)

        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", self.on_quit)
        self.add_action(action)

        builder = Gtk.Builder.new_from_string(MENU_XML, -1)
        self.set_app_menu(builder.get_object("app-menu"))

    def on_settings(self, action, param):
        pass

    def on_about(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.window, modal=True)
        about_dialog.set_program_name(__appname__)
        about_dialog.set_version(__version__)
        about_dialog.set_copyright("Copyright \xa9 2018 dermatty")
        about_dialog.set_comments("A binary newsreader for the gnome desktop")
        about_dialog.set_website("https://github.com/dermatty/GINZIBIX")
        about_dialog.set_website_label('Ginzibix on GitHub')
        try:
            about_dialog.set_logo(GdkPixbuf.Pixbuf.new_from_file_at_size(GBXICON, 64, 64))
        except GLib.GError as e:
            print("Cannot find icon file!")

        about_dialog.set_license_type(Gtk.License.GPL_3_0)

        about_dialog.present()

    def on_quit(self, action, param):
        if self.mpp_main:
            os.kill(self.mpp_main.pid, signal.SIGTERM)
            self.mpp_main.join()
        self.quit()


# connects to GUI_Connector in main.py and gets data for displaying
class GUI_Poller(Thread):

    def __init__(self, lock, appdata, update_mainwindow_dl, update_mainwindow_sortednzbs, logger, port="36603"):
        Thread.__init__(self)
        self.daemon = True
        self.context = zmq.Context()
        self.host = "localhost"
        self.port = port
        self.lock = lock
        self.data = None
        self.nzbname = None
        self.delay = 0.5
        self.appdata = appdata
        self.update_mainwindow_dl = update_mainwindow_dl
        self.update_mainwindow_sortednzbs = update_mainwindow_sortednzbs
        self.socket = self.context.socket(zmq.REQ)
        self.logger = logger

    def run(self):
        self.socket.setsockopt(zmq.LINGER, 0)
        socketurl = "tcp://" + self.host + ":" + self.port
        self.socket.connect(socketurl)
        # self.socket.RCVTIMEO = 1000
        dl_running = True
        order_changed = False
        while True:
            sortednzblist = []
            with self.lock:
                dl_running_new = self.appdata.dl_running
                order_changed = self.appdata.order_changed
            # if download state switched -> send to main.py
            if dl_running_new != dl_running:
                dl_running = dl_running_new
                if dl_running:
                    msg0 = "SET_RESUME"
                else:
                    msg0 = "SET_PAUSE"
                try:
                    self.socket.send_pyobj((msg0, None))
                    datatype, datarec = self.socket.recv_pyobj()
                except Exception as e:
                    self.logger.error("GUI_ConnectorMain: " + str(e))
            elif order_changed:
                with self.lock:
                    msg0 = "SET_NZB_ORDER"
                    orderednzbs = [nzb[0] for nzb in self.appdata.nzbs]
                    try:
                        self.socket.send_pyobj((msg0, orderednzbs))
                        datatype, datarec = self.socket.recv_pyobj()
                    except Exception as e:
                        self.logger.error("GUI_ConnectorMain: " + str(e))
                    self.appdata.order_changed = False
            else:
                try:
                    self.socket.send_pyobj(("REQ", None))
                    datatype, datarec = self.socket.recv_pyobj()
                    if datatype == "NOOK":
                        continue
                    elif datatype == "DL_DATA":
                        data, pwdb_msg, server_config, threads, dl_running, nzb_status_string, netstat_mbitcurr = datarec
                        try:
                            GLib.idle_add(self.update_mainwindow_dl, data, pwdb_msg, server_config, threads, dl_running, nzb_status_string, netstat_mbitcurr)
                        except Exception as e:
                            self.logger.debug(lpref + whoami() + ": " + str(e))
                    elif datatype == "NZB_DATA":
                        sortednzblist = datarec
                        try:
                            GLib.idle_add(self.update_mainwindow_sortednzbs, sortednzblist)
                        except Exception as e:
                            self.logger.debug(lpref + whoami() + ": " + str(e))
                except Exception as e:
                    self.logger.error("GUI_ConnectorMain: " + str(e))
            time.sleep(self.delay)


class File_Poller(Thread):

    def __init__(self, lock, appdata, update_mainwindow, logger, port="36601"):
        Thread.__init__(self)
        self.daemon = True
        self.lock = lock
        self.data = None
        self.nzbname = None
        self.delay = 1
        self.appdata = appdata
        self.update_mainwindow = update_mainwindow
        self.logger = logger
        self.inotify = inotify_simple.INotify()
        self.nzbdir = "/home/stephan/.ginzibix/"
        self.nzbfile = "NZB_DATA.TXT"
        self.nzbfile_full = self.nzbdir + self.nzbfile
        watch_flags = inotify_simple.flags.CREATE | inotify_simple.flags.DELETE | inotify_simple.flags.MODIFY | inotify_simple.flags.DELETE_SELF
        self.inotify.add_watch(self.nzbdir, watch_flags)
        sortednzblist = self.read_nzb_file()
        GLib.idle_add(self.update_mainwindow, None, None, None, None, None, None, sortednzblist)

    def get_inotify_events(self):
        noevent = True
        while noevent:
            for event in self.inotify.read():
                if event.name == self.nzbfile:
                    for flg in inotify_simple.flags.from_mask(event.mask):
                        if "flags.MODIFY" in str(flg):
                            noevent = False
                            break

    def read_nzb_file(self):
        sortednzblist = []
        with open(self.nzbfile_full, "r") as fp:
            for line in fp:
                ll = line.split()
                if ll:
                    linetuple = (ll[0], int(ll[1]), int(ll[2]), int(ll[3]), int(ll[4]), int(ll[5]))
                    sortednzblist.append(linetuple)
        return sorted(sortednzblist, key=lambda prio: prio[1])

    def run(self):
        while True:
            self.get_inotify_events()
            sortednzblist = self.read_nzb_file()
            GLib.idle_add(self.update_mainwindow, None, None, None, None, None, None, sortednzblist)
            time.sleep(self.delay)


# app = Application()
# exit_status = app.run(sys.argv)
# sys.exit(exit_status)