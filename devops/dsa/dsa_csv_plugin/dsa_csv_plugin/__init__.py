from girder.plugin import GirderPlugin


class DsaCsvPlugin(GirderPlugin):
    DISPLAY_NAME = 'DSA CSV Import'
    CLIENT_SOURCE_PATH = 'web_client'

    def load(self, info):
        import cherrypy
        from .rest import DsaCsvResource, get_filter_html, get_upload_html

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
