from girder.plugin import GirderPlugin


class DsaCsvPlugin(GirderPlugin):
    DISPLAY_NAME = 'DSA CSV Import'

    def load(self, info):
        import cherrypy
        from .rest import DsaCsvResource, get_upload_html

        info['apiRoot'].dsa_tools = DsaCsvResource()

        class _CsvUploadPage:
            @cherrypy.expose
            def index(self):
                cherrypy.response.headers['Content-Type'] = 'text/html; charset=utf-8'
                return get_upload_html()

        info['serverRoot'].csv_upload = _CsvUploadPage()
