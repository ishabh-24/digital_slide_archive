try:
    from girder.plugin import GirderPlugin
except ImportError:  # the offline tools import this package without Girder installed
    GirderPlugin = object


class DsaCsvPlugin(GirderPlugin):
    DISPLAY_NAME = 'DSA CSV Import'
    CLIENT_SOURCE_PATH = 'web_client'

    def load(self, info):
        import cherrypy
        from .rest import (DsaCsvResource, get_annotation_html, get_convert_html,
                           get_filter_html, get_format_html, get_tools_html,
                           get_upload_html)

        info['apiRoot'].dsa_tools = DsaCsvResource()

        # Girder's server root uses cherrypy's MethodDispatcher, so a webroot
        # page must expose HTTP-verb methods (GET), not `index`.
        class _HtmlPage:
            exposed = True

            def __init__(self, render):
                self._render = render

            def GET(self, **params):
                cherrypy.response.headers['Content-Type'] = 'text/html;charset=utf-8'
                return self._render()

        info['serverRoot'].csv_upload = _HtmlPage(get_upload_html)
        info['serverRoot'].slidefilter = _HtmlPage(get_filter_html)
        info['serverRoot'].annotation_upload = _HtmlPage(get_annotation_html)
        info['serverRoot'].annotation_tools = _HtmlPage(get_tools_html)
        info['serverRoot'].annotation_convert = _HtmlPage(get_convert_html)
        info['serverRoot'].annotation_format = _HtmlPage(get_format_html)
