from disco import func
from disco.core import result_iterator, Params

def module(package):
    def module(object):
        import sys
        sys.modules['%s.%s' % (package, object.__name__)] = object
        return object
    return module

class DiscodexJob(object):
    combiner             = None
    map_input_stream     = staticmethod(func.map_input_stream)
    map_output_stream    = staticmethod(func.map_output_stream)
    map_reader           = staticmethod(func.map_line_reader)
    map_writer           = staticmethod(func.netstr_writer)
    params               = Params()
    partition            = staticmethod(func.default_partition)
    profile              = False
    reduce               = None
    reduce_reader        = staticmethod(func.netstr_reader)
    reduce_writer        = staticmethod(func.netstr_writer)
    reduce_output_stream = staticmethod(func.reduce_output_stream)
    result_reader        = staticmethod(func.netstr_reader)
    required_modules     = []
    scheduler            = {}
    sort                 = False
    nr_reduces           = 1

    @staticmethod
    def map(*args, **kwargs):
        raise NotImplementedError

    @property
    def name(self):
        return self._job.name

    @property
    def results(self):
        return result_iterator(self._job.wait(), reader=self.result_reader)

    def run(self, disco_master, disco_prefix):
        jobargs = {'name':              disco_prefix,
                   'input':             self.input,
                   'map':               self.map,
                   'map_input_stream':  self.map_input_stream,
                   'map_output_stream': self.map_output_stream,
                   'map_reader':        self.map_reader,
                   'map_writer':        self.map_writer,
                   'params':            self.params,
                   'partition':         self.partition,
                   'profile':           self.profile,
                   'required_modules':  self.required_modules,
                   'scheduler':         self.scheduler,
                   'sort':              self.sort}

        if self.combiner:
            jobargs.update({'combiner': self.combiner})

        if self.reduce:
            jobargs.update({'reduce':        self.reduce,
                            'reduce_reader': self.reduce_reader,
                            'reduce_writer': self.reduce_writer,
                            'reduce_output_stream': self.reduce_output_stream,
                            'nr_reduces':    self.nr_reduces})

        self._job = disco_master.new_job(**jobargs)
        return self

class Indexer(DiscodexJob):
    def __init__(self, dataset):
        self.input      = dataset.input
        self.map_reader = dataset.parser
        self.map        = dataset.demuxer
        self.partition  = dataset.balancer
        self.profile    = dataset.profile
        self.nr_reduces = dataset.nr_ichunks
        self.sort       = dataset.sort
        self.params     = Params(n=0)

        if dataset.k_viter:
            from discodex.mapreduce import demuxers
            self.sort = False
            self.map  = demuxers.iterdemux

    @staticmethod
    def reduce(iterator, out, params):
        # there should be a discodb writer of some sort
        from discodb import DiscoDB, kvgroup
        DiscoDB(kvgroup(iterator)).dump(out.fd)

    def reduce_output_stream(stream, partition, url, params):
        return stream, 'discodb:%s' % url.split(':', 1)[1]
    reduce_output_stream = [func.reduce_output_stream, reduce_output_stream]

class MetaIndexer(DiscodexJob):
    scheduler     = {'force_local': True}

    def __init__(self, metaset):
        self.input  = metaset.ichunks
        self.map    = metaset.metakeyer

    @staticmethod
    def map_reader(fd, size, fname):
        if hasattr(fd, '__iter__'):
            return fd
        return func.map_line_reader(fd, size, fname)

    @staticmethod
    def combiner(metakey, key, buf, done, params):
        from discodb import DiscoDB, MetaDB
        if done:
            datadb = Task.discodb
            metadb = DiscoDB(buf)
            yield None, MetaDB(datadb, metadb)
        else:
            keys = buf.get(metakey, [])
            keys.append(key)
            print buf
            buf[metakey] = keys

    @staticmethod
    def map_writer(fd, none, metadb, params):
        metadb.dump(fd)

    def map_output_stream(stream, partition, url, params):
        # there should be a metadb writer
        return stream, 'metadb:%s' % url.split(':', 1)[1]
    map_output_stream = [func.map_output_stream, map_output_stream]

class DiscoDBIterator(DiscodexJob):
    scheduler     = {'force_local': True}
    method        = 'keys'
    mapfilters    = []
    reducefilters = []

    def __init__(self, ichunks, target, mapfilters, reducefilters):
        self.ichunks = ichunks
        if target:
            self.method = '%s/%s' % (target, self.method)
        self.params = Params(mapfilters=mapfilters or self.mapfilters,
                             reducefilters=reducefilters or self.reducefilters)
        if reducefilters:
            self.reduce = self._reduce

    @property
    def input(self):
        return ['%s/%s/' % (ichunk, self.method) for ichunk in self.ichunks]

    @staticmethod
    def map_reader(fd, size, fname):
        if hasattr(fd, '__iter__'):
            return fd
        return func.map_line_reader(fd, size, fname)

    @staticmethod
    def map(entry, params):
        from discodex.mapreduce.func import filterchain, funcify, kviterify
        filterfn = filterchain(funcify(name) for name in params.mapfilters)
        return kviterify(filterfn(entry))

    @staticmethod
    def _reduce(iterator, out, params):
        from discodex.mapreduce.func import filterchain, funcify, kviterify, kvgroup
        filterfn = filterchain(funcify(name) for name in params.reducefilters)
        for items in kvgroup(iterator):
            for k, v in kviterify(filterfn(items)):
                out.add(k, v)

    @property
    def results(self):
        for k, v in result_iterator(self._job.wait(), reader=self.result_reader):
            yield (k, v) if k else v

class KeyIterator(DiscoDBIterator):
    pass

class ValuesIterator(DiscoDBIterator):
    method = 'values'

class ItemsIterator(DiscoDBIterator):
    method     = 'items'
    mapfilters = ['kvungroup']

class Queryer(DiscoDBIterator):
    method = 'query'

    def __init__(self, ichunks, target, mapfilters, reducefilters, query):
        super(Queryer, self).__init__(ichunks, target, mapfilters, reducefilters)
        self.params.discodb_query = query

class Record(object):
    __slots__ = ('fields', 'fieldnames')

    def __init__(self, *fields, **namedfields):
        for name in namedfields:
            if name in self.__slots__:
                raise ValueError('Use of reserved fieldname: %r' % name)
        self.fields = (list(fields) + namedfields.values())
        self.fieldnames = len(fields) * [None] + namedfields.keys()

    def __getattr__(self, attr):
        for n, name in enumerate(self.fieldnames):
            if attr == name:
                return self[n]
        raise AttributeError('%r has no attribute %r' % (self, attr))

    def __getitem__(self, index):
        return self.fields[index]

    def __iter__(self):
        from itertools import izip
        return izip(self.fieldnames, self.fields)

    def __repr__(self):
        return 'Record(%s)' % ', '.join('%s=%r' % (n, f) if n else '%r' % f
                                      for f, n in zip(self.fields, self.fieldnames))


# ichunk parser == func.discodb_reader (iteritems)


# parser:  data -> records       \
#                                 | kvgenerator            \
# demuxer: record -> k, v ...    /                          |
#                                                           | indexer
#                                                           |
# balancer: (k, ) ... -> (p, (k, )) ...   \                /
#                                          | ichunkbuilder
# ichunker: (p, (k, v) ... -> ichunks     /
