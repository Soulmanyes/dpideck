"""The "What's new" window shown once on the first start after an update. GTK 4 only (works
under libadwaita too). The notes come from updater.Updater.whats_new()."""

import threading

import updater
from gi.repository import GLib, Gtk


def show_when_updated(parent, updates, app_name):
    """Asks GitHub in the background whether this start follows an update and, if so,
    shows that release's notes over parent. Stays quiet when offline."""
    def work():
        try:
            releases = updates.whats_new()
        except (OSError, ValueError, KeyError):  # offline or an odd reply: next start
            return
        if releases:
            GLib.idle_add(_show, parent, updates, app_name, releases)

    threading.Thread(target=work, daemon=True).start()


def _show(parent, updates, app_name, releases):
    # same application as parent, so the compositor groups it with the app's own windows
    win = Gtk.Window(title=f"What's new in {app_name}", transient_for=parent, modal=True,
                     application=parent.get_application(), default_width=520,
                     default_height=440)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                  margin_bottom=18, margin_start=18, margin_end=18)
    head = Gtk.Label(label=f"Updated to {releases[0]['tag']}", xalign=0)
    head.add_css_class("title-2")
    box.append(head)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for rel in releases:
        if len(releases) > 1:
            tag = Gtk.Label(label=rel["tag"], xalign=0)
            tag.add_css_class("heading")
            body.append(tag)
        notes = Gtk.Label(xalign=0, wrap=True, use_markup=True)
        notes.set_markup(updater.notes_markup(rel["notes"]))
        body.append(notes)
    scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                child=body)
    box.append(scroll)
    buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
    if releases[0]["url"]:
        buttons.append(Gtk.LinkButton(uri=releases[0]["url"], label="Open on GitHub"))
    close = Gtk.Button(label="Close")
    close.add_css_class("suggested-action")
    close.connect("clicked", lambda _b: win.close())
    buttons.append(close)
    box.append(buttons)
    win.set_child(box)
    win.set_default_widget(close)
    win.present()
    updates.mark_seen()
    return False
