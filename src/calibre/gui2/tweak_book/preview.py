#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2015, Kovid Goyal <kovid at kovidgoyal.net>

# TODO:
# inspect element
# live css
# check that clicking on both internal and external links works
# check if you can remove the restriction that prevents inspector dock from being undocked
# check the context menu
# check syncing of position back and forth
# check all butotns and search functionality in preview panel
# rewrite JS from coffeescript to rapydscript
# pass user stylesheet with css for split

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import textwrap
import time
from collections import defaultdict
from functools import partial
from Queue import Empty, Queue
from threading import Thread
from urlparse import urlparse

from PyQt5.Qt import (
    QApplication, QBuffer, QByteArray, QIcon, QMenu, QSize, QTimer, QToolBar, QUrl,
    QVBoxLayout, QWidget, pyqtSignal, pyqtSlot
)
from PyQt5.QtWebEngineCore import QWebEngineUrlSchemeHandler
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineView
)

from calibre import prints
from calibre.constants import FAKE_HOST, FAKE_PROTOCOL, __version__
from calibre.ebooks.oeb.base import OEB_DOCS, XHTML_MIME, serialize
from calibre.ebooks.oeb.polish.parsing import parse
from calibre.gui2 import NO_URL_FORMATTING, error_dialog, open_url, secure_webengine
from calibre.gui2.tweak_book import TOP, actions, current_container, editors, tprefs
from calibre.gui2.widgets2 import HistoryLineEdit2
from calibre.utils.ipc.simple_worker import offload_worker
from polyglot.builtins import unicode_type

try:
    from PyQt5 import sip
except ImportError:
    import sip


shutdown = object()


def get_data(name):
    'Get the data for name. Returns a unicode string if name is a text document/stylesheet'
    if name in editors:
        return editors[name].get_raw_data()
    return current_container().raw_data(name)

# Parsing of html to add linenumbers {{{


def parse_html(raw):
    root = parse(raw, decoder=lambda x:x.decode('utf-8'), line_numbers=True, linenumber_attribute='data-lnum')
    return serialize(root, 'text/html').encode('utf-8')


class ParseItem(object):

    __slots__ = ('name', 'length', 'fingerprint', 'parsing_done', 'parsed_data')

    def __init__(self, name):
        self.name = name
        self.length, self.fingerprint = 0, None
        self.parsed_data = None
        self.parsing_done = False

    def __repr__(self):
        return 'ParsedItem(name=%r, length=%r, fingerprint=%r, parsing_done=%r, parsed_data_is_None=%r)' % (
            self.name, self.length, self.fingerprint, self.parsing_done, self.parsed_data is None)


class ParseWorker(Thread):

    daemon = True
    SLEEP_TIME = 1

    def __init__(self):
        Thread.__init__(self)
        self.requests = Queue()
        self.request_count = 0
        self.parse_items = {}
        self.launch_error = None

    def run(self):
        mod, func = 'calibre.gui2.tweak_book.preview', 'parse_html'
        try:
            # Connect to the worker and send a dummy job to initialize it
            self.worker = offload_worker(priority='low')
            self.worker(mod, func, '<p></p>')
        except:
            import traceback
            traceback.print_exc()
            self.launch_error = traceback.format_exc()
            return

        while True:
            time.sleep(self.SLEEP_TIME)
            x = self.requests.get()
            requests = [x]
            while True:
                try:
                    requests.append(self.requests.get_nowait())
                except Empty:
                    break
            if shutdown in requests:
                self.worker.shutdown()
                break
            request = sorted(requests, reverse=True)[0]
            del requests
            pi, data = request[1:]
            try:
                res = self.worker(mod, func, data)
            except:
                import traceback
                traceback.print_exc()
            else:
                pi.parsing_done = True
                parsed_data = res['result']
                if res['tb']:
                    prints("Parser error:")
                    prints(res['tb'])
                else:
                    pi.parsed_data = parsed_data

    def add_request(self, name):
        data = get_data(name)
        ldata, hdata = len(data), hash(data)
        pi = self.parse_items.get(name, None)
        if pi is None:
            self.parse_items[name] = pi = ParseItem(name)
        else:
            if pi.parsing_done and pi.length == ldata and pi.fingerprint == hdata:
                return
            pi.parsed_data = None
            pi.parsing_done = False
        pi.length, pi.fingerprint = ldata, hdata
        self.requests.put((self.request_count, pi, data))
        self.request_count += 1

    def shutdown(self):
        self.requests.put(shutdown)

    def get_data(self, name):
        return getattr(self.parse_items.get(name, None), 'parsed_data', None)

    def clear(self):
        self.parse_items.clear()

    def is_alive(self):
        return Thread.is_alive(self) or (hasattr(self, 'worker') and self.worker.is_alive())


parse_worker = ParseWorker()
# }}}

# Override network access to load data "live" from the editors {{{


class UrlSchemeHandler(QWebEngineUrlSchemeHandler):

    def __init__(self, parent=None):
        QWebEngineUrlSchemeHandler.__init__(self, parent)
        self.requests = defaultdict(list)

    def requestStarted(self, rq):
        if bytes(rq.requestMethod()) != b'GET':
            rq.fail(rq.RequestDenied)
            return
        url = rq.requestUrl()
        if url.host() != FAKE_HOST:
            rq.fail(rq.UrlNotFound)
            return
        name = url.path()[1:]
        try:
            c = current_container()
            if not c.has_name(name):
                rq.fail(rq.UrlNotFound)
                return
            mime_type = c.mime_map.get(name, 'application/octet-stream')
            if mime_type in OEB_DOCS:
                mime_type = XHTML_MIME
                self.requests[name].append((mime_type, rq))
                QTimer.singleShot(0, self.check_for_parse)
            else:
                data = get_data(name)
                if isinstance(data, type('')):
                    data = data.encode('utf-8')
                mime_type = {
                    # Prevent warning in console about mimetype of fonts
                    'application/vnd.ms-opentype':'application/x-font-ttf',
                    'application/x-font-truetype':'application/x-font-ttf',
                    'application/font-sfnt': 'application/x-font-ttf',
                }.get(mime_type, mime_type)
                self.send_reply(rq, mime_type, data)
        except Exception:
            import traceback
            traceback.print_exc()
            rq.fail(rq.RequestFailed)

    def send_reply(self, rq, mime_type, data):
        if sip.isdeleted(rq):
            return
        buf = QBuffer(parent=rq)
        buf.open(QBuffer.WriteOnly)
        # we have to copy data into buf as it will be garbage
        # collected by python
        buf.write(data)
        buf.seek(0)
        buf.close()
        buf.aboutToClose.connect(buf.deleteLater)
        rq.reply(mime_type.encode('ascii'), buf)

    def check_for_parse(self):
        remove = []
        for name, requests in self.requests.iteritems():
            data = parse_worker.get_data(name)
            if data is not None:
                if not isinstance(data, bytes):
                    data = data.encode('utf-8')
                for mime_type, rq in requests:
                    self.send_reply(rq, mime_type, data)
                remove.append(name)
        for name in remove:
            del self.requests[name]

        if self.requests:
            return QTimer.singleShot(10, self.check_for_parse)


# }}}


def uniq(vals):
    ''' Remove all duplicates from vals, while preserving order.  '''
    vals = vals or ()
    seen = set()
    seen_add = seen.add
    return tuple(x for x in vals if x not in seen and not seen_add(x))


def insert_scripts(profile, *scripts):
    sc = profile.scripts()
    for script in scripts:
        for existing in sc.findScripts(script.name()):
            sc.remove(existing)
    for script in scripts:
        sc.insert(script)


def create_script(name, src, world=QWebEngineScript.ApplicationWorld, injection_point=QWebEngineScript.DocumentCreation, on_subframes=True):
    script = QWebEngineScript()
    script.setSourceCode(src)
    script.setName(name)
    script.setWorldId(world)
    script.setInjectionPoint(injection_point)
    script.setRunsOnSubFrames(on_subframes)
    return script


def create_profile():
    ans = getattr(create_profile, 'ans', None)
    if ans is None:
        ans = QWebEngineProfile(QApplication.instance())
        ua = 'calibre-editor-preview ' + __version__
        ans.setHttpUserAgent(ua)
        from calibre.utils.resources import compiled_coffeescript
        js = compiled_coffeescript('ebooks.oeb.display.utils', dynamic=False)
        js += P('csscolorparser.js', data=True, allow_user_override=False)
        js += compiled_coffeescript('ebooks.oeb.polish.preview', dynamic=False)
        insert_scripts(ans, create_script('editor-preview.js', js))
        url_handler = UrlSchemeHandler(ans)
        ans.installUrlSchemeHandler(QByteArray(FAKE_PROTOCOL.encode('ascii')), url_handler)
        s = ans.settings()
        s.setDefaultTextEncoding('utf-8')
        s.setAttribute(s.FullScreenSupportEnabled, False)
        s.setAttribute(s.LinksIncludedInFocusChain, False)
        create_profile.ans = ans
    return ans


class WebPage(QWebEnginePage):

    sync_requested = pyqtSignal(object, object, object)
    split_requested = pyqtSignal(object, object)

    def __init__(self, parent):
        QWebEnginePage.__init__(self, create_profile(), parent)
        secure_webengine(self, for_viewer=True)
        # TOD: Implement this
        # css = '[data-in-split-mode="1"] [data-is-block="1"]:hover { cursor: pointer !important; border-top: solid 5px green !important }'

    def javaScriptConsoleMessage(self, level, msg, linenumber, source_id):
        prints('%s:%s: %s' % (source_id, linenumber, msg))

    def acceptNavigationRequest(self, url, req_type, is_main_frame):
        if req_type == self.NavigationTypeReload:
            return True
        if url.scheme() in (FAKE_PROTOCOL, 'data'):
            return True
        open_url(url)
        return False

    @pyqtSlot(str, str, str)
    def request_sync(self, tag_name, href, sourceline_address):
        try:
            self.sync_requested.emit(unicode_type(tag_name), unicode_type(href), json.loads(unicode_type(sourceline_address)))
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass

    def go_to_anchor(self, anchor, lnum):
        self.runjs('window.calibre_preview_integration.go_to_anchor(%s, %s)' % (
            json.dumps(anchor), json.dumps(str(lnum))))

    @pyqtSlot(str, str)
    def request_split(self, loc, totals):
        actions['split-in-preview'].setChecked(False)
        loc, totals = json.loads(unicode_type(loc)), json.loads(unicode_type(totals))
        if not loc or not totals:
            return error_dialog(self.view(), _('Invalid location'),
                                _('Cannot split on the body tag'), show=True)
        self.split_requested.emit(loc, totals)

    def runjs(self, src, callback=None):
        if callback is None:
            self.runJavaScript(src, QWebEngineScript.ApplicationWorld)
        else:
            self.runJavaScript(src, QWebEngineScript.ApplicationWorld, callback)

    def go_to_sourceline_address(self, sourceline_address):
        lnum, tags = sourceline_address
        if lnum is None:
            return
        tags = [x.lower() for x in tags]
        self.runjs('window.calibre_preview_integration.go_to_sourceline_address(%d, %s)' % (lnum, json.dumps(tags)))

    def split_mode(self, enabled):
        self.runjs(
            'window.calibre_preview_integration.split_mode(%s)' % (
                'true' if enabled else 'false'))


class WebView(QWebEngineView):

    def __init__(self, parent=None):
        QWebEngineView.__init__(self, parent)
        self.inspector = QWebEngineView(self)
        w = QApplication.instance().desktop().availableGeometry(self).width()
        self._size_hint = QSize(int(w/3), int(w/2))
        self._page = WebPage(self)
        self.setPage(self._page)
        self.clear()
        self.setAcceptDrops(False)
        self.renderProcessTerminated.connect(self.render_process_terminated)

    def render_process_terminated(self):
        error_dialog(self, _('Render process crashed'), _(
            'The Qt WebEngine Render process has crashed so Preview/Live css'
            ' will not work. You should try restarting the editor.'), show=True)

    def sizeHint(self):
        return self._size_hint

    def refresh(self):
        self.pageAction(QWebEnginePage.Reload).trigger()

    def clear(self):
        self.setHtml(_(
            '''
            <h3>Live preview</h3>

            <p>Here you will see a live preview of the HTML file you are currently editing.
            The preview will update automatically as you make changes.

            <p style="font-size:x-small; color: gray">Note that this is a quick preview
            only, it is not intended to simulate an actual e-book reader. Some
            aspects of your e-book will not work, such as page breaks and page margins.
            '''))

    def inspect(self):
        raise NotImplementedError('TODO: Implement this')
        # self.inspector.parent().show()
        # self.inspector.parent().raise_()
        # self.pageAction(self.page().InspectElement).trigger()

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        p = self._page
        mf = p.mainFrame()
        r = mf.hitTestContent(ev.pos())
        url = unicode_type(r.linkUrl().toString(NO_URL_FORMATTING)).strip()
        ca = self.pageAction(QWebEnginePage.Copy)
        if ca.isEnabled():
            menu.addAction(ca)
        menu.addAction(actions['reload-preview'])
        menu.addAction(QIcon(I('debug.png')), _('Inspect element'), self.inspect)
        if url.partition(':')[0].lower() in {'http', 'https'}:
            menu.addAction(_('Open link'), partial(open_url, r.linkUrl()))
        menu.exec_(ev.globalPos())


class Preview(QWidget):

    sync_requested = pyqtSignal(object, object)
    split_requested = pyqtSignal(object, object, object)
    split_start_requested = pyqtSignal()
    link_clicked = pyqtSignal(object, object)
    refresh_starting = pyqtSignal()
    refreshed = pyqtSignal()

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.l = l = QVBoxLayout()
        self.setLayout(l)
        l.setContentsMargins(0, 0, 0, 0)
        self.view = WebView(self)
        self.view._page.sync_requested.connect(self.request_sync)
        self.view._page.split_requested.connect(self.request_split)
        self.view._page.loadFinished.connect(self.load_finished)
        self.inspector = self.view.inspector
        l.addWidget(self.view)
        self.bar = QToolBar(self)
        l.addWidget(self.bar)

        ac = actions['auto-reload-preview']
        ac.setCheckable(True)
        ac.setChecked(True)
        ac.toggled.connect(self.auto_reload_toggled)
        self.auto_reload_toggled(ac.isChecked())
        self.bar.addAction(ac)

        ac = actions['sync-preview-to-editor']
        ac.setCheckable(True)
        ac.setChecked(True)
        ac.toggled.connect(self.sync_toggled)
        self.sync_toggled(ac.isChecked())
        self.bar.addAction(ac)

        self.bar.addSeparator()

        ac = actions['split-in-preview']
        ac.setCheckable(True)
        ac.setChecked(False)
        ac.toggled.connect(self.split_toggled)
        self.split_toggled(ac.isChecked())
        self.bar.addAction(ac)

        ac = actions['reload-preview']
        ac.triggered.connect(self.refresh)
        self.bar.addAction(ac)

        actions['preview-dock'].toggled.connect(self.visibility_changed)

        self.current_name = None
        self.last_sync_request = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        parse_worker.start()
        self.current_sync_request = None

        self.search = HistoryLineEdit2(self)
        self.search.initialize('tweak_book_preview_search')
        self.search.setPlaceholderText(_('Search in preview'))
        self.search.returnPressed.connect(self.find_next)
        self.bar.addSeparator()
        self.bar.addWidget(self.search)
        for d in ('next', 'prev'):
            ac = actions['find-%s-preview' % d]
            ac.triggered.connect(getattr(self, 'find_' + d))
            self.bar.addAction(ac)

    def find(self, direction):
        text = unicode_type(self.search.text())
        self.view.findText(text, QWebEnginePage.FindWrapsAroundDocument | (
            QWebEnginePage.FindBackward if direction == 'prev' else QWebEnginePage.FindFlags(0)))

    def find_next(self):
        self.find('next')

    def find_prev(self):
        self.find('prev')

    def request_sync(self, tagname, href, lnum):
        if self.current_name:
            c = current_container()
            if tagname == 'a' and href:
                if href and href.startswith('#'):
                    name = self.current_name
                else:
                    name = c.href_to_name(href, self.current_name) if href else None
                if name == self.current_name:
                    return self.view._page.go_to_anchor(urlparse(href).fragment, lnum)
                if name and c.exists(name) and c.mime_map[name] in OEB_DOCS:
                    return self.link_clicked.emit(name, urlparse(href).fragment or TOP)
            self.sync_requested.emit(self.current_name, lnum)

    def request_split(self, loc, totals):
        if self.current_name:
            self.split_requested.emit(self.current_name, loc, totals)

    def sync_to_editor(self, name, sourceline_address):
        self.current_sync_request = (name, sourceline_address)
        QTimer.singleShot(100, self._sync_to_editor)

    def _sync_to_editor(self):
        if not actions['sync-preview-to-editor'].isChecked():
            return
        try:
            if self.refresh_timer.isActive() or self.current_sync_request[0] != self.current_name:
                return QTimer.singleShot(100, self._sync_to_editor)
        except TypeError:
            return  # Happens if current_sync_request is None
        sourceline_address = self.current_sync_request[1]
        self.current_sync_request = None
        self.view._page.go_to_sourceline_address(sourceline_address)

    def report_worker_launch_error(self):
        if parse_worker.launch_error is not None:
            tb, parse_worker.launch_error = parse_worker.launch_error, None
            error_dialog(self, _('Failed to launch worker'), _(
                'Failed to launch the worker process used for rendering the preview'), det_msg=tb, show=True)

    def name_to_qurl(self, name=None):
        name = name or self.current_name
        qurl = QUrl()
        qurl.setScheme(FAKE_PROTOCOL), qurl.setAuthority(FAKE_HOST), qurl.setPath('/' + name)
        return qurl

    def show(self, name):
        if name != self.current_name:
            self.refresh_timer.stop()
            self.current_name = name
            self.report_worker_launch_error()
            parse_worker.add_request(name)
            self.view.setUrl(self.name_to_qurl())
            return True

    def refresh(self):
        if self.current_name:
            self.refresh_timer.stop()
            # This will check if the current html has changed in its editor,
            # and re-parse it if so
            self.report_worker_launch_error()
            parse_worker.add_request(self.current_name)
            # Tell webkit to reload all html and associated resources
            current_url = self.name_to_qurl()
            self.refresh_starting.emit()
            if current_url != self.view.url():
                # The container was changed
                self.view.setUrl(current_url)
            else:
                self.view.refresh()
            self.refreshed.emit()

    def clear(self):
        self.view.clear()
        self.current_name = None

    @property
    def is_visible(self):
        return actions['preview-dock'].isChecked()

    @property
    def live_css_is_visible(self):
        try:
            return actions['live-css-dock'].isChecked()
        except KeyError:
            return False

    def start_refresh_timer(self):
        if self.live_css_is_visible or (self.is_visible and actions['auto-reload-preview'].isChecked()):
            self.refresh_timer.start(tprefs['preview_refresh_time'] * 1000)

    def stop_refresh_timer(self):
        self.refresh_timer.stop()

    def auto_reload_toggled(self, checked):
        if self.live_css_is_visible and not actions['auto-reload-preview'].isChecked():
            actions['auto-reload-preview'].setChecked(True)
            error_dialog(self, _('Cannot disable'), _(
                'Auto reloading of the preview panel cannot be disabled while the'
                ' Live CSS panel is open.'), show=True)
        actions['auto-reload-preview'].setToolTip(_(
            'Auto reload preview when text changes in editor') if not checked else _(
                'Disable auto reload of preview'))

    def sync_toggled(self, checked):
        actions['sync-preview-to-editor'].setToolTip(_(
            'Disable syncing of preview position to editor position') if checked else _(
                'Enable syncing of preview position to editor position'))

    def visibility_changed(self, is_visible):
        if is_visible:
            self.refresh()

    def split_toggled(self, checked):
        actions['split-in-preview'].setToolTip(textwrap.fill(_(
            'Abort file split') if checked else _(
                'Split this file at a specified location.\n\nAfter clicking this button, click'
                ' inside the preview panel above at the location you want the file to be split.')))
        if checked:
            self.split_start_requested.emit()
        else:
            self.view._page.split_mode(False)

    def do_start_split(self):
        self.view._page.split_mode(True)

    def stop_split(self):
        actions['split-in-preview'].setChecked(False)

    def load_finished(self, ok):
        if actions['split-in-preview'].isChecked():
            if ok:
                self.do_start_split()
            else:
                self.stop_split()

    def apply_settings(self):
        s = self.view.settings()
        s.setFontSize(s.DefaultFontSize, tprefs['preview_base_font_size'])
        s.setFontSize(s.DefaultFixedFontSize, tprefs['preview_mono_font_size'])
        s.setFontSize(s.MinimumLogicalFontSize, tprefs['preview_minimum_font_size'])
        s.setFontSize(s.MinimumFontSize, tprefs['preview_minimum_font_size'])
        sf, ssf, mf = tprefs['preview_serif_family'], tprefs['preview_sans_family'], tprefs['preview_mono_family']
        s.setFontFamily(s.StandardFont, {'serif':sf, 'sans':ssf, 'mono':mf, None:sf}[tprefs['preview_standard_font_family']])
        s.setFontFamily(s.SerifFont, sf)
        s.setFontFamily(s.SansSerifFont, ssf)
        s.setFontFamily(s.FixedFont, mf)
