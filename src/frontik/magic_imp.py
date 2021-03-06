import sys
import os.path
import imp
import functools

import logging
log = logging.getLogger('frontik.imp')

def gen_module_name(app_name, module_name=None):
    if module_name:
        return 'frontik.imp.{0}.{1}'.format(app_name, module_name)
    else:
        return 'frontik.imp.{0}'.format(app_name)
    

class FrontikAppImporter(object):
    '''
    apps :: {app_name: app_root}
    '''
    def __init__(self, apps):
        self.app_roots = dict(apps)
        self.modules = dict()

    def imp_app_module(self, app_name, module_name):
        '''
        app_name :: 'chameleon'
        page_path :: 'pages.index'
        '''

        if not app_name in self.app_roots:
            raise ImportError('{0} frontik app not found'.format(app_name))

        app_module_name = gen_module_name(app_name, module_name)

        if app_module_name in self.modules:
            log.debug('get %s from module cache', app_module_name)
            return self.modules[app_module_name]

        app_root = self.app_roots[app_name]

        module_name_as_path = os.path.join(*module_name.split('.'))

        app_module_probable_filenames = [
            os.path.join(app_root, module_name_as_path, '__init__.py'),
            os.path.join(app_root, module_name_as_path, 'index.py'),
            os.path.join(app_root, '{0}.py'.format(module_name_as_path))]

        for app_module_filename in app_module_probable_filenames:
            if os.path.exists(app_module_filename):
                break
        else:
            raise ImportError('{module_name} module was not found in {app_name}, {app_module_filenames} expected'.format(
                module_name=module_name,
                app_name=app_name,
                app_module_filenames=app_module_probable_filenames))

        log.debug('importing %s from %s', app_module_name, app_module_filename)
        
        module = imp.new_module(app_module_name)
        sys.modules[module.__name__] = module

        module.__file__ = app_module_filename
        module.frontik_import = functools.partial(self.in_module_import_single, module, app_name)
        
        execfile(app_module_filename, module.__dict__)

        self.modules[app_module_name] = module
        
        return module

    def in_module_import_single(self, app_module, app_name, module_name):
        if '.' in module_name:
            raise ImportError('{0}: compound modules are not supported in frontik apps'.format(module_name))

        module = self.imp_app_module(app_name, module_name)
        setattr(app_module, module_name, module)
        return module
