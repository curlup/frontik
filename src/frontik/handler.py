# -*- coding: utf-8 -*-

from __future__ import with_statement

from functools import partial
import datetime
import functools
import functools
import httplib
import os.path
import time
import traceback
import urlparse
import xml.sax.saxutils

import tornado.httpclient
import tornado.options
import tornado.web

from frontik import etree
import frontik.async
import frontik.auth
import frontik.doc
import frontik.http
import frontik.util
import frontik.handler_xml
import frontik.handler_whc_limit
import frontik.handler_debug

import logging
log = logging.getLogger('frontik.handler')

import future

# TODO cleanup this after release of frontik with frontik.async
AsyncGroup = frontik.async.AsyncGroup

class HTTPError(tornado.web.HTTPError):
    """An exception that will turn into an HTTP error response."""
    def __init__(self, status_code, *args, **kwargs):
        for kwarg in ["text", "xml", "xsl"]:
            setattr(self, kwarg, kwargs.setdefault(kwarg, None)) 
            del kwargs[kwarg]
        tornado.web.HTTPError.__init__(self, status_code, *args, **kwargs)


class Stats(object):
    def __init__(self):
        self.page_count = 0
        self.http_reqs_count = 0

    def next_request_id(self):
        self.page_count += 1
        return self.page_count

stats = Stats()

class PageLogger(logging.Logger):
    '''
    This class is supposed to fix huge memory 'leak' in logging
    module. I.e. every call to logging.getLogger(some_unique_name)
    wastes memory as resulting logger is memoized by
    module. PageHandler used to create unique logger on each request
    by call logging.getLogger('frontik.handler.%s' %
    (self.request_id,)). This lead to wasting about 10Mb per 10K
    requests.
    '''
    
    def __init__(self, request_id, page, zero_time):
        logging.Logger.__init__(self, 'frontik.handler.{0}'.format(request_id))
        self.page = page
        self._time = zero_time
        self.stages = []


    def handle(self, record):
        logging.Logger.handle(self, record)
        log.handle(record)

    def stage_tag(self, stage):
        self._stage_tag(stage, (time.time() - self._time)*1000 )
        self._time = time.time()

    def _stage_tag(self, stage, time_delta):
        self.stages.append( ( stage, time_delta) )
        self.debug('Stage: {stage}'.format(stage=stage) )

    def stage_tag_backdate(self, stage, time_delta):
        self._stage_tag(stage, time_delta )

    def process_stages(self):
        self.debug("Stages for {0} : ".format(self.page) + " ".join( ["{0}:{1:.2f}ms".format(k, v) for k,v in self.stages] ))


class PageHandlerGlobals(object):
    '''
    Объект с настройками для всех хендлеров
    '''
    def __init__(self, app_package):
        self.config = app_package.config

        self.xml = frontik.handler_xml.PageHandlerXMLGlobals(app_package.config)

        self.http_client = frontik.http.TimeoutingHttpFetcher(
                tornado.httpclient.AsyncHTTPClient(max_clients=200, max_simultaneous_connections=200))

        
class PageHandler(tornado.web.RequestHandler):
    def __init__(self, ph_globals, application, request):
        self.handler_started = time.time()

        self.request_id = request.headers.get('X-Request-Id', stats.next_request_id())
        self.path = urlparse.urlparse(request.uri).path or request.uri
        self.log = PageLogger(self.request_id, self.path, self.handler_started)

        tornado.web.RequestHandler.__init__(self, application, request, logger=self.log)

        self.ph_globals = ph_globals
        self.config = self.ph_globals.config
        self.http_client = self.ph_globals.http_client

        self.debug = frontik.handler_debug.PageHandlerDebug(self)  

        self.whc_limit = frontik.handler_whc_limit.PageHandlerWHCLimit(self)

        self.xml = frontik.handler_xml.PageHandlerXML(self)
        self.doc = self.xml.doc # backwards compatibility for self.doc.put
        
        self.text = None

        self.finish_group = frontik.async.AsyncGroup(self.async_callback(self._finish_page),
                                                     name='finish',
                                                     log=self.log.debug)

        if self.get_argument('nopost', None) is not None:
            self.log.debug('apply_postprocessor==False due to ?nopost query arg')
            self.apply_postprocessor = False
            self.require_debug_access()
        else:
            self.apply_postprocessor = True

    def require_debug_access(self):
        if not tornado.options.options.debug:
            frontik.auth.require_basic_auth(self, tornado.options.options.debug_login,
                                            tornado.options.options.debug_password)

    def get_error_html(self, status_code, **kwargs):
        if self.debug.debug_mode_logging:
            return self.debug.get_debug_page(status_code, **kwargs)
        else:
            return tornado.web.RequestHandler.get_error_html(self, status_code, **kwargs)

    def send_error(self, status_code=500, **kwargs):
        def standard_send_error():
            return super(PageHandler, self).send_error(status_code, **kwargs)

        def xsl_send_error():
            return

        def plaintext_send_error():
            return
            
        exception = kwargs.get("exception", None)

        if exception:
            self.set_status(status_code)

            if getattr(exception, "text", None) is not None:
                self.set_plaintext_response(exception.text)
                return plaintext_send_error()

            if getattr(exception, "xml", None) is not None:
                self.doc.put(exception.xml)

                if getattr(exception, "xsl", None) is not None:
                    self.set_xsl(exception.xsl)
                    return xsl_send_error()
                elif self.transform:
                    return xsl_send_error()
                else:
                    return standard_send_error()
        return standard_send_error()

    # эта заляпа сливает обработчики get и post запросов
    @tornado.web.asynchronous
    def post(self, *args, **kw):
        self.get(*args, **kw)

    @tornado.web.asynchronous
    def get(self, *args, **kw):
        self.get_page()
        self.finish_page()

    def finish(self, chunk=None):
        if hasattr(self, 'whc_limit'):
            self.whc_limit.release()

        self.log.debug('done in %.2fms', (time.time() - self.handler_started)*1000)
        self.log.process_stages()

        # if debug_mode is on: ignore any output we intended to write
        # and use debug log instead
        if hasattr(self, 'debug') and self.debug.debug_mode:
            self.set_header('Content-Type', 'text/html')
            res = self.debug.get_debug_page(self._status_code)
        else:
            res = chunk

        tornado.web.RequestHandler.finish(self, res)

    def get_page(self):
        ''' Эта функция должна быть переопределена в наследнике и
        выполнять актуальную работу хендлера '''
        pass

    ###

    def async_callback(self, callback, *args, **kw):
        return tornado.web.RequestHandler.async_callback(self, self.check_finished(callback, *args, **kw))

    def check_finished(self, callback, *args, **kwargs):
        if args or kwargs:
            callback = partial(callback, *args, **kwargs)

        def wrapper(*args, **kwargs):
            if self._finished:
                self.log.warn('Page was already finished, %s ignored', callback)
            else:
                callback(*args, **kwargs)
        
        return wrapper

    ###

    def fetch_url(self, url, callback=None):
        """
        Прокси метод для get_url, логирующий употребления fetch_url
        """
        from urlparse import parse_qs, urlparse

        self.log.error("Used deprecated method `fetch_url`. %s", traceback.format_stack()[-2][:-1])
        scheme, netloc, path, params, query, fragment = urlparse(url)
        new_url = "{0}://{1}{2}".format(scheme, netloc, path)
        query = parse_qs(query)

        return self.get_url(new_url, data=query, callback=callback)

    def fetch_request(self, req, callback):
        if not self._finished:
            stats.http_reqs_count += 1

            req.headers['X-Request-Id'] = self.request_id

            return self.http_client.fetch(
                    req,
                    self.finish_group.add(self.async_callback(callback)))
        else:
            self.log.warn('attempted to make http request to %s while page is already finished; ignoring', req.url)

    def get_url(self, url, data={}, headers={}, connect_timeout=0.5, request_timeout=2, callback=None):
        placeholder = future.Placeholder()

        self.fetch_request(
            frontik.util.make_get_request(url, data, headers, connect_timeout, request_timeout),
            partial(self._fetch_request_response, placeholder, callback))

        return placeholder

    def get_url_retry(self, url, data={}, headers={}, retry_count=3, retry_delay=0.1, connect_timeout=0.5, request_timeout=2, callback=None):
        placeholder = future.Placeholder()

        req = frontik.util.make_get_request(url, data, headers, connect_timeout, request_timeout)

        def step1(retry_count, response):
            if response.error and retry_count > 0:
                self.log.warn('failed to get %s; retries left = %s; retrying', response.effective_url, retry_count)
                # TODO use handler-specific ioloop
                if retry_delay > 0:
                    tornado.ioloop.IOLoop.instance().add_timeout(time.time() + retry_delay,
                        self.finish_group.add(self.async_callback(partial(step2, retry_count))))
                else:
                    step2(retry_count)
            else:
                if response.error and retry_count == 0:
                    self.log.warn('failed to get %s; no more retries left; give up retrying', response.effective_url)

                self._fetch_request_response(placeholder, callback, response)

        def step2(retry_count):
            self.http_client.fetch(req, self.finish_group.add(self.async_callback(partial(step1, retry_count - 1))))

        self.http_client.fetch(req, self.finish_group.add(self.async_callback(partial(step1, retry_count - 1))))
        
        return placeholder

    def post_url(self, url, data={},
                 headers={},
                 files={},
                 connect_timeout=0.5, request_timeout=2,
                 callback=None):
        
        placeholder = future.Placeholder()
        
        self.fetch_request(
            frontik.util.make_post_request(url, data, headers, files, connect_timeout, request_timeout),
            partial(self._fetch_request_response, placeholder, callback))
        
        return placeholder

    def _parse_response(self, response):
        '''
        return :: (placeholder_data, response_as_xml)
        None - в случае ошибки парсинга
        '''

        if response.error:
            self.log.warn('%s failed %s (%s)', response.code, response.effective_url, str(response.error))
            data = [etree.Element('error', dict(url=response.effective_url, reason=str(response.error), code=str(response.code)))]

            if response.body:
                try:
                    data.append(etree.Comment(response.body.replace("--", "%2D%2D")))
                except ValueError:
                    self.log.warn("Could not add debug info in XML comment with unparseable response.body. non-ASCII response.")
                    
            return (data, None)
        else:
            try:
                element = etree.fromstring(response.body)
            except:
                if len(response.body) > 100:
                    body_preview = '{0}...'.format(response.body[:100])
                else:
                    body_preview = response.body

                self.log.warn('failed to parse XML response from %s data "%s"',
                                 response.effective_url,
                                 body_preview)

                return (etree.Element('error', dict(url=response.effective_url, reason='invalid XML')),
                        None)

            else:
                return ([frontik.handler_xml._source_comment(response.effective_url), element],
                        element)

    def _fetch_request_response(self, placeholder, callback, response):
        self.log.debug('got %s %s in %.2fms', response.code, response.effective_url, response.request_time*1000)
        
        data, xml = self._parse_response(response)
        placeholder.set_data(data)

        if callback:
            callback(xml, response)

    ###

    def set_plaintext_response(self, text):
        self.text = text

    ###

    def finish_page(self):
        self.finish_group.try_finish()

    def _finish_page(self):        
        if not self._finished:
            self.log.stage_tag("page")

            res = None
            
            if self.text is not None:
                res = self._prepare_finish_plaintext()
            else:
                res = self.xml._finish_xml()
            
            if hasattr(self.config, 'postprocessor'):
                if self.apply_postprocessor:
                    self.log.debug('applying postprocessor')
                    self.async_callback(self.config.postprocessor)(self, res, self.async_callback(partial(self._wait_postprocessor, time.time())))
                else:
                    self.log.debug('skipping postprocessor')
                    self.finish(res)
            else:
                self.finish(res)

        else:
            self.log.warn('trying to finish already finished page, probably bug in a workflow, ignoring')

    def _wait_postprocessor(self, start_time, data):
        self.log.stage_tag("postprocess")
        self.log.debug("applied postprocessor '%s' in %.2fms",
                self.config.postprocessor,
                (time.time() - start_time)*1000)
        self.finish(data)

    def _prepare_finish_plaintext(self):
        self.log.debug("finishing plaintext")
        return self.text

    ###

    def xml_from_file(self, filename):
        return self.xml.xml_from_file(filename)

    def set_xsl(self, filename):
        return self.xml.set_xsl(filename)
