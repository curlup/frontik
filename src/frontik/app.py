import sys
import os.path

import tornado.web
import tornado.ioloop
import logging
from tornado.options import options

log = logging.getLogger('frontik.server')        

import handler

class StatusHandler(tornado.web.RequestHandler):
    def get(self, *args, **kw):
        self.write('pages served: %s\n' % (handler.stats.page_count,))
        self.write('http reqs made: %s\n' % (handler.stats.http_reqs_count,))


class StopHandler(tornado.web.RequestHandler):
    def get(self, *args, **kw):
        log.info('requested shutdown')
        tornado.ioloop.IOLoop.instance().stop()


class CountPageHandlerInstances(tornado.web.RequestHandler):
    def get(self, *args, **kw):
        import gc
        import frontik.handler
        hh = tuple([i for i in gc.get_objects()
                    if isinstance(i, frontik.handler.PageHandler)])

        if len(hh) > 0:
            import pdb; pdb.set_trace()

        self.finish('{0}\n{1}'.format(len(hh), [i for i in gc.get_referrers(*hh)
                                                if i is not hh]))


def _init_app_package(app_dir, app_package_name):
    if app_dir:
        abs_app_dir = os.path.abspath(app_dir)
        log.debug('appending "%s" document_dir to sys.path', abs_app_dir)
        sys.path.insert(0, abs_app_dir)

    try:
        app_package = __import__(app_package_name)
    except:
        log.error('%s module cannot be found', app_package_name)
        raise

    try:
        app_package.config = __import__("{0}.config".format(app_package_name), fromlist=['config'])
    except:
        log.exception('%s.config module cannot be found', app_package_name)
        raise

    if app_dir:
        if not app_package.__file__.startswith(abs_app_dir):
            msg = '%s module is found at %s while %s expected' % (
                app_package_name, app_package.__file__, abs_app_dir)
            log.error(msg)
            raise Exception(msg)

    return app_package


class _FrontikApp(object):
    def __init__(self, url_prefix, package_name, ph_globals):
        self.url_prefix = url_prefix
        self.package_name = package_name
        self.ph_globals = ph_globals


class FrontikModuleDispatcher(object):
    def __init__(self, applications):
        '''
         applications :: [dirname]
        '''

        self.app_packages = dict()

        for (url_prefix, app_path) in applications:
            app_dir = os.path.dirname(app_path)
            app_package_name = os.path.basename(app_path)

            app_package = _init_app_package(app_dir, app_package_name)

            self.app_packages[url_prefix] = _FrontikApp(url_prefix, app_package_name, handler.PageHandlerGlobals(app_package))

    def pages_dispatcher(self, application, request):
        log.info('requested url: %s', request.uri)

        request_split = request.path.strip('/').split('/') # [app_name, *module_parts]

        url_prefix = request_split[0]
        page_module_name_parts = request_split[1:]

        if not url_prefix in self.app_packages:
            log.warn('invalid url prefix requested: %s', url_prefix)
            return tornado.web.ErrorHandler(application, request, 404)

        app = self.app_packages[url_prefix]

        if page_module_name_parts:
            page_module_name = '{0}.pages.{1}'.format(app.package_name,
                                                      '.'.join(page_module_name_parts))
        else:
            page_module_name = '{0}.pages'.format(app.package_name)

        try:
            page_module = __import__(page_module_name, fromlist=['Page'])
            log.debug('using %s from %s', page_module_name, page_module.__file__)
        except ImportError:
            log.exception('%s module not found', page_module_name)
            return tornado.web.ErrorHandler(application, request, 404)
        except:
            log.exception('error while importing %s module', page_module_name)
            return tornado.web.ErrorHandler(application, request, 500)

        try:
            return page_module.Page(app.ph_globals, application, request)
        except:
            log.exception('%s.Page class not found', page_module_name)
            return tornado.web.ErrorHandler(application, request, 500)


def get_app(pages_dispatcher):
    return tornado.web.Application([
            (r'/status/', StatusHandler),
            (r'/stop/', StopHandler),
            (r'/ph_count/', CountPageHandlerInstances),
            (r'/.*', pages_dispatcher),
            ])

