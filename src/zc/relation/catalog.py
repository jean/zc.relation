import sys
import copy

import persistent
import persistent.list
import persistent.wref
import BTrees
import BTrees.Length

import zope.interface
import zope.interface.interfaces

from zc.relation import interfaces

# use OOBTree, not items tuple

##############################################################################
# helpers
#

def multiunion(sets, data):
    sets = tuple(s for s in sets if s) # bool is appropriate here
    if not sets:
        res = data['Set']()
    elif data['multiunion'] is not None:
        res = data['multiunion'](sets)
    else:
        res = sets[0]
        for s in sets[1:]:
            res = data['union'](res, s)
    return res

def getModuleTools(module):
    return dict(
        (nm, getattr(module, nm, None)) for nm in 
        ('BTree', 'TreeSet', 'Bucket', 'Set',
         'intersection', 'multiunion', 'union', 'difference'))

def getMapping(tools):
    if tools['TreeSet'].__name__[0] == 'I':
        Mapping = BTrees.family32.IO.BTree
    elif tools['TreeSet'].__name__[0] == 'L':
        Mapping = BTrees.family64.IO.BTree
    else:
        assert tools['TreeSet'].__name__.startswith('O')
        Mapping = BTrees.family32.OO.BTree
    return Mapping

def makeCheckTargetFilter(targetQuery, targetFilter, catalog):
    targetCache = {}
    checkTargetFilter = None
    if targetQuery:
        targetData = catalog.getRelationTokens(targetQuery)
        if not targetData:
            return () # shortcut, indicating no values
        else:
            if targetFilter is not None:
                def checkTargetFilter(relchain, query):
                    return relchain[-1] in targetData and targetFilter(
                        relchain, query, catalog, targetCache)
            else:
                def checkTargetFilter(relchain, query):
                    return relchain[-1] in targetData
    elif targetFilter is not None:
        def checkTargetFilter(relchain, query):
            return targetFilter(relchain, query, catalog, targetCache)
    return checkTargetFilter

class Ref(persistent.Persistent):
    def __init__(self, ob):
        self.ob = ob

    def __call__(self):
        return self.ob

def createRef(ob):
    if isinstance(ob, persistent.Persistent):
        return persistent.wref.WeakRef(ob)
    else:
        return Ref(ob)

##############################################################################
# Any and any
#

class Any(object):
    def __init__(self, source):
        self.source = frozenset(source)

    def __iter__(self):
        return iter(self.source)

    def __eq__(self, other):
        return (isinstance(other, self.__class__) and
                self.source == other.source)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<%s.%s instance %r>' % (
            self.__class__.__module__, self.__class__.__name__,
            tuple(sorted(self.source)))

def any(*args):
    return Any(args)

##############################################################################
# the marker that shows that a path is circular
#

class CircularRelationPath(tuple):
    zope.interface.implements(interfaces.ICircularRelationPath)

    def __new__(kls, elements, cycled):
        res = super(CircularRelationPath, kls).__new__(kls, elements)
        res.cycled = cycled
        return res
    def __repr__(self):
        return 'cycle%s' % super(CircularRelationPath, self).__repr__()

##############################################################################
# the relation catalog

class Catalog(persistent.Persistent):
    zope.interface.implements(interfaces.ICatalog)

    family = BTrees.family32
    _listeners = _queryFactories = _searchIndexes = ()

    def __init__(self, dumpRel, loadRel, relFamily=None, family=None):
        if family is not None:
            self.family = family
        else:
            family = self.family
        self._name_TO_mapping = family.OO.BTree()
        # held mappings are objtoken to (relcount, relset)
        self._EMPTY_name_TO_relcount_relset = family.OO.BTree()
        self._reltoken_name_TO_objtokenset = family.OO.BTree()
        if relFamily is None:
            relFamily = family.IF
        self._relTools = getModuleTools(relFamily)
        self._relTools['load'] = loadRel
        self._relTools['dump'] = dumpRel
        self._relLength = BTrees.Length.Length()
        self._relTokens = self._relTools['TreeSet']()
        # private; only mutate via indexValue and unindexValue
        self._attrs = _attrs = self.family.OO.Bucket() # _attrs name is legacy

    # The code is divided into the following sections by ReST-like headers:
    #
    # Administration and Introspection
    #   General
    #   Value Indexes
    #   Listeners
    #   DefaultQueryFactories
    #   Search Indexes
    # Indexing
    #   Top-Level
    #   Indexing Values
    # Tokenization
    # Searching

    # Administration and Introspection
    # ================================

    # General
    # -------

    def __contains__(self, rel):
        return self.tokenizeRelation(rel) in self._relTokens

    def __len__(self):
        return self._relLength.value

    def __iter__(self):
        cache = {}
        for token in self._relTokens:
            yield self._relTools['load'](token, self, cache)

    def clear(self):
        for v in self._name_TO_mapping.values():
            v.clear()
        self._EMPTY_name_TO_relcount_relset.clear()
        self._reltoken_name_TO_objtokenset.clear()
        self._relTokens.clear()
        self._relLength.set(0)
        for l in self._iterListeners():
            l.sourceCleared(self)

    def copy(self, klass=None):
        if klass is None:
            klass = self.__class__
        res = klass.__new__(klass)
        res._relTokens = self._relTools['TreeSet']()
        res._relTokens.update(self._relTokens)
        res.family = self.family
        res._name_TO_mapping = self.family.OO.BTree()
        for name, mapping in self._name_TO_mapping.items():
            new = mapping.__class__()
            for k, (l, s) in mapping.items():
                new[k] = (copy.copy(l), copy.copy(s))
            res._name_TO_mapping[name] = new
        res._EMPTY_name_TO_relcount_relset = self.family.OO.BTree()
        for k, (l, s) in self._EMPTY_name_TO_relcount_relset.items():
            res._EMPTY_name_TO_relcount_relset[k] = (
                copy.copy(l), copy.copy(s))
        res._reltoken_name_TO_objtokenset = self.family.OO.BTree()
        for k, s in self._reltoken_name_TO_objtokenset.items():
            res._reltoken_name_TO_objtokenset[k] = copy.copy(s)
        res._attrs = self.family.OO.Bucket(
            [(k, self.family.OO.Bucket(v)) for k, v in self._attrs.items()])
        res._relTools = dict(self._relTools)
        res._listeners = self._listeners # it's a tuple
        res._queryFactories = self._queryFactories # it's a tuple
        res._relLength = BTrees.Length.Length()
        res._relLength.set(self._relLength.value)
        res._searchIndexes = tuple(ix.copy(self) for ix in self._searchIndexes)
        return res
    
    # Value Indexes
    # -------------

    def _fixLegacyAttrs(self):
        if isinstance(self._attrs, dict): # legacy
            # because _attrs used to be a normal dict
            self._attrs = self.family.OO.Bucket(self._attrs)

    def addValueIndex(self, element, dump=None, load=None, btree=None,
                      multiple=False, name=None):
        if btree is None:
            btree = self.family.IF
        res = self.family.OO.Bucket(getModuleTools(btree))
        res['dump'] = dump
        res['load'] = load
        res['multiple'] = multiple
        if (res['dump'] is None) ^ (res['load'] is None):
            raise ValueError(
                "either both of 'dump' and 'load' must be None, or "
                "neither")
            # when both load and dump are None, this is a small
            # optimization that can be a large optimization if the returned
            # value is one of the main four options of the selected btree
            # family (BTree, TreeSet, Set, Bucket).
        if zope.interface.interfaces.IElement.providedBy(element):
            key = 'element'
            res['attrname'] = defaultname = element.__name__
            res['interface'] = element.interface
            res['call'] = zope.interface.interfaces.IMethod.providedBy(element)
        else:
            key = 'callable'
            defaultname = getattr(element, '__name__', None)
        if [d for d in self._attrs.values() if d.get(key) == element]:
            raise ValueError('element already indexed', element)
        res[key] = element
        if name is None:
            if defaultname is None:
                raise ValueError('no name specified')
            name = defaultname
        if name in self._attrs:
            raise ValueError('name already used', name)
        res['name'] = name
        self._name_TO_mapping[name] = getMapping(res)()
        # these are objtoken to (relcount, relset)
        self._attrs[name] = res
        load = self._relTools['load']
        cache = {}
        for token in self._relTokens:
            additions = {}
            additions[name] = (None, self._indexNew(
                token, load(token, self, cache), res))
            for l in self._iterListeners():
                l.relationModified(token, self, additions, {})
        self._fixLegacyAttrs()

    def iterValueIndexInfo(self):
        for d in self._attrs.values():
            res = {}
            res['name'] = d['name']
            res['element'] = d.get('element', d.get('callable'))
            res['multiple'] = d['multiple']
            res['dump'] = d['dump']
            res['load'] = d['load']
            res['btree'] = sys.modules[d['TreeSet'].__module__]
            yield res

    def removeValueIndex(self, name):
        del self._attrs[name]
        self._fixLegacyAttrs()
        del self._name_TO_mapping[name]
        if name in self._EMPTY_name_TO_relcount_relset:
            del self._EMPTY_name_TO_relcount_relset[name]

    # Listeners
    # -----------
    
    def addListener(self, listener):
        res = [ref for ref in self._listeners if ref() is not None]
        res.append(createRef(listener))
        self._listeners = tuple(res)
        listener.sourceAdded(self)

    def iterListeners(self):
        for ref in self._listeners:
            l = ref()
            if l is not None:
                yield l

    def removeListener(self, listener):
        if listener is None:
            raise LookupError('listener not found', listener)
        res = []
        found = False
        for ref in reversed(self._listeners):
            l = ref()
            if l is listener and not found:
                found = True
                continue
            res.append(ref)
        if not found:
            raise LookupError('listener not found', listener)
        res.reverse()
        self._listeners = tuple(res)
        listener.sourceRemoved(self)

    # DefaultQueryFactories
    # -----------------------
    
    def addDefaultQueryFactory(self, factory):
        res = [ref for ref in self._queryFactories if ref() is not None]
        res.append(createRef(factory))
        self._queryFactories = tuple(res)

    def iterDefaultQueryFactories(self):
        for ref in self._queryFactories:
            factory = ref()
            if factory is not None:
                yield factory

    def removeDefaultQueryFactory(self, factory):
        if factory is None:
            raise LookupError('factory not found', factory)
        res = []
        found = False
        for ref in reversed(self._queryFactories):
            l = ref()
            if l is factory and not found:
                found = True
                continue
            res.append(ref)
        if not found:
            raise LookupError('factory not found', factory)
        res.reverse()
        self._queryFactories = tuple(res)

    # Search Indexes
    # --------------

    def addSearchIndex(self, ix):
        ix.setCatalog(self)
        self._searchIndexes += (ix,)

    def iterSearchIndexes(self):
        return iter(self._searchIndexes)

    def removeSearchIndex(self, ix):
        res = tuple(i for i in self._searchIndexes if i is not ix)
        if len(res) == len(self._searchIndexes):
            raise LookupError('index not found', ix)
        self._searchIndexes = res
        ix.setCatalog(None)

    # Indexing
    # ========

    # Top-Level
    # ---------

    def _indexNew(self, token, rel, data):
        assert self._reltoken_name_TO_objtokenset.get(
            (token, data['name']), self) is self
        values, tokens, optimization = self._getValuesAndTokens(
            rel, data)
        if optimization and tokens is not None:
            tokens = data['TreeSet'](tokens)
        self._add(token, tokens, data['name'], tokens)
        return tokens

    def index(self, rel):
        self.index_doc(self._relTools['dump'](rel, self, {}), rel)

    def index_doc(self, relToken, rel):
        additions = {}
        removals = {}
        if relToken in self._relTokens:
            # reindex
            for data in self._attrs.values():
                values, newTokens, optimization = self._getValuesAndTokens(
                    rel, data)
                oldTokens = self._reltoken_name_TO_objtokenset[
                    (relToken, data['name'])]
                if newTokens != oldTokens:
                    if newTokens is not None and oldTokens is not None:
                        added = data['difference'](newTokens, oldTokens)
                        removed = data['difference'](oldTokens, newTokens)
                        if optimization:
                            # the goal of this optimization is to not have to
                            # recreate a TreeSet (which can be large and
                            # relatively timeconsuming) when only small changes
                            # have been made.  We ballpark this by saying
                            # "if there are only a few removals, do them, and
                            # then do an update: it's almost certainly a win
                            # over essentially generating a new TreeSet and
                            # updating it with *all* values.  On the other
                            # hand, if there are a lot of removals, it's
                            # probably quicker just to make a new one."  See
                            # timeit/set_creation_vs_removal.py for details.
                            # A len is pretty cheap--`removed` is a single
                            # bucket, and `oldTokens` should have all of its
                            # buckets in memory already, and adding up bucket
                            # lens in C is pretty fast.
                            len_removed = len(removed)
                            if len_removed < 5:
                                recycle = True
                            else:
                                len_old = len(oldTokens)
                                ratio = float(len_old)/len_removed
                                recycle = (ratio <= 0.1 or len_old > 500
                                           and ratio < 0.2)
                            if recycle:
                                for t in removed:
                                    oldTokens.remove(t)
                                oldTokens.update(added)
                                newTokens = oldTokens
                            else:
                                newTokens = data['TreeSet'](newTokens)
                    else:
                        if optimization and newTokens is not None:
                            newTokens = data['TreeSet'](newTokens)
                        removed = oldTokens
                        added = newTokens
                    self._remove(relToken, removed, data['name'])
                    if removed:
                        removals[data['name']] = removed
                    self._add(relToken, added, data['name'], newTokens)
                    if added:
                        additions[data['name']] = added
            for l in self._iterListeners():
                l.relationModified(relToken, self, additions, removals)
        else:
            # new token
            for data in self._attrs.values():
                additions[data['name']] = self._indexNew(relToken, rel, data)
            self._relTokens.insert(relToken)
            self._relLength.change(1)
            for l in self._iterListeners():
                l.relationAdded(relToken, self, additions)

    def unindex(self, rel):
        self.unindex_doc(self._relTools['dump'](rel, self, {}))

    def unindex_doc(self, relToken):
        removals = {}
        if relToken in self._relTokens:
            for data in self._attrs.values():
                tokens = self._reltoken_name_TO_objtokenset.pop(
                    (relToken, data['name']))
                if tokens:
                    removals[data['name']] = tokens
                self._remove(relToken, tokens, data['name'])
            self._relTokens.remove(relToken)
            self._relLength.change(-1)
        for l in self._iterListeners():
            l.relationRemoved(relToken, self, removals)

    # Indexing Values
    # ---------------

    def _getValuesAndTokens(self, rel, data):
        values = None
        if 'interface' in data:
            valueSource = data['interface'](rel, None)
            if valueSource is not None:
                values = getattr(valueSource, data['attrname'])
                if data['call']:
                    values = values()
        else:
            values = data['callable'](rel, self)
        if not data['multiple'] and values is not None:
            # None is a marker for no value
            values = (values,)
        optimization = data['dump'] is None and (
            values is None or 
            isinstance(values, (
                data['TreeSet'], data['BTree'], data['Bucket'], data['Set'])))
        if not values:
            return None, None, optimization
        elif optimization:
            # this is the optimization story (see _add)
            return values, values, optimization
        else:
            cache = {}
            if data['dump'] is None:
                tokens = data['TreeSet'](values)
            else:
                tokens = data['TreeSet'](
                    data['dump'](o, self, cache) for o in values)
            return values, tokens, False

    def _add(self, relToken, tokens, name, fullTokens):
        self._reltoken_name_TO_objtokenset[(relToken, name)] = fullTokens
        if tokens is None:
            dataset = self._EMPTY_name_TO_relcount_relset
            keys = (name,)
        else:
            dataset = self._name_TO_mapping[name]
            keys = tokens
        for key in keys:
            data = dataset.get(key)
            if data is None:
                data = dataset[key] = (
                    BTrees.Length.Length(), self._relTools['TreeSet']())
            res = data[1].insert(relToken)
            assert res, 'Internal error: relToken existed in data'
            data[0].change(1)

    def _remove(self, relToken, tokens, name):
        if tokens is None:
            dataset = self._EMPTY_name_TO_relcount_relset
            keys = (name,)
        else:
            dataset = self._name_TO_mapping[name]
            keys = tokens
        for key in keys:
            data = dataset[key]
            data[1].remove(relToken)
            data[0].change(-1)
            if not data[0].value:
                del dataset[key]
            else:
                assert data[0].value > 0

    # Tokenization
    # ============

    def tokenizeQuery(self, query):
        res = {}
        for k, v in query.items():
            if k is None:
                tools = self._relTools
            else:
                tools = self._attrs[k]
            if isinstance(v, Any):
                if tools['dump'] is not None:
                    cache = {}
                    v = Any(tools['dump'](val, self, cache) for val in v)
            else:
                if v is not None and tools['dump'] is not None:
                    v = tools['dump'](v, self, {})
            res[k] = v
        return res

    def resolveQuery(self, query):
        res = {}
        for k, v in query.items():
            if k is None:
                tools = self._relTools
            else:
                tools = self._attrs[k]
            if isinstance(v, Any):
                if tools['load'] is not None:
                    cache = {}
                    v = Any(tools['load'](val, self, cache) for val in v)
            else:
                if v is not None and tools['load'] is not None:
                    v = tools['load'](v, self, {})
            res[k] = v
        return res

    def tokenizeValues(self, values, name):
        dump = self._attrs[name]['dump']
        if dump is None:
            return values
        cache = {}
        return (dump(v, self, cache) for v in values)

    def resolveValueTokens(self, tokens, name):
        load = self._attrs[name]['load']
        if load is None:
            return tokens
        cache = {}
        return (load(t, self, cache) for t in tokens)

    def tokenizeRelation(self, rel):
        return self._relTools['dump'](rel, self, {})

    def resolveRelationToken(self, token):
        return self._relTools['load'](token, self, {})

    def tokenizeRelations(self, rels):
        cache = {}
        return (self._relTools['dump'](r, self, cache) for r in rels)

    def resolveRelationTokens(self, tokens):
        cache = {}
        return (self._relTools['load'](t, self, cache) for t in tokens)

    # Searching
    # =========

    # Internal Helpers
    # ----------------

    def _relData(self, query):
        # query must be BTrees.family32.OO.Bucket.  The key may be
        # a value index name or None, indicating one or more relations.  The
        # val may be token, None, or iterator (object with a `next` method)
        # of tokens (may not include None).
        if not query:
            return self._relTokens
        data = []
        tools = self._relTools
        explicit_relations = False
        for name, value in query.items():
            if name is None:
                explicit_relations = True
                if not isinstance(value, Any):
                    value = (value,)
                rels = tools['Set'](value)
                length = len(rels)
            else:
                if isinstance(value, Any):
                    get = self._name_TO_mapping[name].get
                    rels = multiunion(
                        (get(token, (None, None))[1] for token in value),
                        self._relTools)
                    length = len(rels)
                else:
                    if value is None:
                        relData = self._EMPTY_name_TO_relcount_relset.get(name)
                    else:
                        relData = self._name_TO_mapping[name].get(value)
                    if relData is None:
                        return None
                    length = relData[0].value
                    rels = relData[1]
            if not length:
                return None
            data.append((length, rels))
        # we don't want to sort on the set values!! just the lengths.
        data.sort(key=lambda i: i[0])
        if explicit_relations and len(data) == 1:
            # we'll need to intersect with our set of relations to make
            # sure the relations are actually members.  This set should
            # be the biggest possible set, so we put it at the end after
            # sorting.
            data.append((self._relLength.value, self._relTokens))
        # we know we have at least one result now.  intersect all.  Work
        # from smallest to largest, until we're done or we don't have any
        # more results.
        res = data.pop(0)[1]
        while res and data:
            res = self._relTools['intersection'](res, data.pop(0)[1])
        return res

    def _getSearchIndexResults(self, name, query, maxDepth, filter,
                               targetQuery, targetFilter, queryFactory):
        for ix in self._searchIndexes:
            res = ix.getResults(name, query, maxDepth, filter, targetQuery,
                                targetFilter, queryFactory)
            if res is not None:
                return res

    def _iterListeners(self):
        # fix up ourself first
        for ix in self._searchIndexes:
            yield ix
        # then tell others
        for l in self.iterListeners():
            yield l

    def _getQueryFactory(self, query, queryFactory):
        res = None
        if queryFactory is not None:
            res = queryFactory(query, self)
        else:
            for queryFactory in self.iterDefaultQueryFactories():
                res = queryFactory(query, self)
                if res is not None:
                    break
        if res is None:
            queryFactory = None
        return queryFactory, res

    def _parse(self, query, maxDepth, filter, targetQuery, targetFilter,
               getQueries):
        assert (isinstance(query, BTrees.family32.OO.Bucket) and
                isinstance(targetQuery, BTrees.family32.OO.Bucket)), (
               'internal error: parse expects query and targetQuery '
               'to already be normalized (to OO.Bucket.')
        if maxDepth is not None and (
            not isinstance(maxDepth, (int, long)) or maxDepth < 1):
            raise ValueError('maxDepth must be None or a positive integer')
        if getQueries is not None:
            queries = getQueries(())
        else:
            queries = (query,)
        if getQueries is None and maxDepth is not None:
            raise ValueError(
                'if maxDepth not in (None, 1), queryFactory must be available')
        relData = (r for r in (self._relData(q) for q in queries) if r)
        if filter is not None:
            filterCache = {}
            def checkFilter(relchain, query):
                return filter(relchain, query, self, filterCache)
        else:
            checkFilter = None
        checkTargetFilter = makeCheckTargetFilter(
            targetQuery, targetFilter, self)
        if checkTargetFilter is not None and not checkTargetFilter:
            relData = ()
        return (query, relData, maxDepth, checkFilter, checkTargetFilter,
                getQueries)

    # API to help plugin writers
    # --------------------------

    def getRelationModuleTools(self):
        return self._relTools
    
    def getValueModuleTools(self, name):
        return self._attrs[name]

    def getRelationTokens(self, query=None):
        if query is None:
            return self._relTokens
        else:
            if not isinstance(query, BTrees.family32.OO.Bucket):
                query = BTrees.family32.OO.Bucket(query)
            return self._relData(query)

    def getValueTokens(self, name, reltoken=None):
        if reltoken is None:
            return self._name_TO_mapping[name]
        else:
            return self._reltoken_name_TO_objtokenset.get(
                (reltoken, name))

    def yieldRelationTokenChains(self, query, relData, maxDepth, checkFilter,
                                 checkTargetFilter, getQueries,
                                 findCycles=True):
        stack = []
        for d in relData:
            stack.append(((), iter(d)))
        while stack:
            tokenChain, relDataIter = stack[0]
            try:
                relToken = relDataIter.next()
            except StopIteration:
                stack.pop(0)
            else:
                tokenChain += (relToken,)
                if checkFilter is not None and not checkFilter(
                    tokenChain, query):
                    continue
                walkFurther = maxDepth is None or len(tokenChain) < maxDepth
                if getQueries is not None and (walkFurther or findCycles):
                    oldInputs = frozenset(tokenChain)
                    next = set()
                    cycled = []
                    for q in getQueries(tokenChain):
                        relData = self._relData(q)
                        if relData:
                            intersection = oldInputs.intersection(relData)
                            if intersection:
                                # it's a cycle
                                cycled.append(q)
                            elif walkFurther:
                                next.update(relData)
                    if walkFurther and next:
                        stack.append((tokenChain, iter(next)))
                    if cycled:
                        tokenChain = CircularRelationPath(
                            tokenChain, cycled)
                if (checkTargetFilter is None or
                    checkTargetFilter(tokenChain, query)):
                    yield tokenChain

    # Main search API
    # ---------------

    def findValueTokens(self, name, query=(), maxDepth=None,
                        filter=None, targetQuery=(), targetFilter=None,
                        queryFactory=None):
        data = self._attrs.get(name)
        if data is None:
            raise ValueError('name not indexed', name)
        query = BTrees.family32.OO.Bucket(query)
        getQueries = None
        if queryFactory is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        targetQuery = BTrees.family32.OO.Bucket(targetQuery)
        if (((maxDepth is None and queryFactory is None)
             or maxDepth==1) and filter is None and targetFilter is None):
            # return a set
            if not query and not targetQuery:
                return self._name_TO_mapping[name]
            rels = self._relData(query)
            if targetQuery and rels:
                # well, it's kind of odd to have specified query and
                # targetQuery without a transitive search, but hey, this
                # should be the result.
                rels = self._relTools['intersection'](
                    rels, self._relData(targetQuery))
            if not rels:
                return data['Set']()
            elif len(rels) == 1:
                res = self._reltoken_name_TO_objtokenset.get(
                    (rels.maxKey(), name))
                if res is None:
                    res = self._attrs[name]['Set']()
                return res
            else:
                return multiunion(
                    (self._reltoken_name_TO_objtokenset.get((r, name))
                     for r in rels), data)
        res = self._getSearchIndexResults(
            name, query, maxDepth, filter, targetQuery,
            targetFilter, queryFactory)
        if res is not None:
            return res
        res = self._getSearchIndexResults(
            None, query, maxDepth, filter, targetQuery, targetFilter,
            queryFactory)
        if res is not None:
            return multiunion(
                (self._reltoken_name_TO_objtokenset.get((r, name))
                 for r in res),
                data)
        if getQueries is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        return self._yieldValueTokens(
            name, *self._parse( # query and targetQuery normalized above
                query, maxDepth, filter, targetQuery, targetFilter,
                getQueries))

    def findValues(self, name, query=(), maxDepth=None, filter=None,
                   targetQuery=(), targetFilter=None,
                   queryFactory=None):
        res = self.findValueTokens(name, query, maxDepth, filter,
                                   targetQuery, targetFilter,
                                   queryFactory)
        resolve = self._attrs[name]['load']
        if resolve is None:
            return res
        else:
            cache = {}
            return (resolve(t, self, cache) for t in res)

    def _yieldValueTokens(
        self, name, query, relData, maxDepth, checkFilter,
        checkTargetFilter, getQueries, yieldSets=False):
        # this is really an internal bit of findValueTokens, and is only
        # used there.
        relSeen = set()
        objSeen = set()
        for path in self.yieldRelationTokenChains(
            query, relData, maxDepth, checkFilter, checkTargetFilter,
            getQueries, findCycles=False):
            relToken = path[-1]
            if relToken not in relSeen:
                relSeen.add(relToken)
                outputSet = self._reltoken_name_TO_objtokenset.get(
                    (relToken, name))
                if outputSet:
                    if yieldSets: # this is needed for zc.relationship!!!
                        yield outputSet
                    else:
                        for token in outputSet:
                            if token not in objSeen:
                                yield token
                                objSeen.add(token)

    def findRelationTokens(self, query=(), maxDepth=None, filter=None,
                           targetQuery=(), targetFilter=None,
                           queryFactory=None):
        query = BTrees.family32.OO.Bucket(query)
        getQueries = None
        if queryFactory is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        targetQuery = BTrees.family32.OO.Bucket(targetQuery)
        if (((maxDepth is None and queryFactory is None)
             or maxDepth==1)
            and filter is None and not targetQuery and targetFilter is None):
            res = self._relData(query)
            if res is None:
                res = self._relTools['Set']()
            return res
        res = self._getSearchIndexResults(
            None, query, maxDepth, filter, targetQuery, targetFilter,
            queryFactory)
        if res is not None:
            return res
        if getQueries is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        seen = self._relTools['Set']()
        return (res[-1] for res in self.yieldRelationTokenChains(
                    *self._parse(
                        query, maxDepth, filter, targetQuery,
                        targetFilter, getQueries) +
                    (False,))
                if seen.insert(res[-1]))

    def findRelations(self, query=(), maxDepth=None, filter=None,
                          targetQuery=(), targetFilter=None,
                          queryFactory=None):
        return self.resolveRelationTokens(
            self.findRelationTokens(
                query, maxDepth, filter, targetQuery, targetFilter,
                queryFactory))

    def findRelationChains(self, query, maxDepth=None, filter=None,
                               targetQuery=(), targetFilter=None,
                               queryFactory=None):
        """find relation tokens that match the query.
        
        same arguments as findValueTokens except no name.
        """
        query = BTrees.family32.OO.Bucket(query)
        queryFactory, getQueries = self._getQueryFactory(
            query, queryFactory)
        return self._yieldRelationChains(*self._parse(
            query, maxDepth, filter, BTrees.family32.OO.Bucket(targetQuery),
            targetFilter, getQueries))

    def _yieldRelationChains(self, query, relData, maxDepth, checkFilter,
                                 checkTargetFilter, getQueries,
                                 findCycles=True):
        # this is really an internal bit of findRelationChains, and is only
        # used there.
        resolve = self._relTools['load']
        cache = {}
        for p in self.yieldRelationTokenChains(
            query, relData, maxDepth, checkFilter, checkTargetFilter,
            getQueries, findCycles):
            t = (resolve(t, self, cache) for t in p)
            if interfaces.ICircularRelationPath.providedBy(p):
                res = CircularRelationPath(t, p.cycled)
            else:
                res = tuple(t)
            yield res

    def findRelationTokenChains(self, query, maxDepth=None, filter=None,
                                    targetQuery=(), targetFilter=None,
                                    queryFactory=None):
        """find relation tokens that match the query.
        
        same arguments as findValueTokens except no name.
        """
        query = BTrees.family32.OO.Bucket(query)
        queryFactory, getQueries = self._getQueryFactory(
            query, queryFactory)
        return self.yieldRelationTokenChains(*self._parse(
            query, maxDepth, filter, BTrees.family32.OO.Bucket(targetQuery),
            targetFilter, getQueries))

    def canFind(self, query, maxDepth=None, filter=None,
                 targetQuery=(), targetFilter=None,
                 queryFactory=None):
        query = BTrees.family32.OO.Bucket(query)
        getQueries = None
        if queryFactory is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        targetQuery = BTrees.family32.OO.Bucket(targetQuery)
        res = self._getSearchIndexResults(
            None, query, maxDepth, filter, targetQuery, targetFilter,
            queryFactory)
        if res is not None:
            return bool(res)
        if getQueries is None:
            queryFactory, getQueries = self._getQueryFactory(
                query, queryFactory)
        try:
            self.yieldRelationTokenChains(
                *self._parse(
                    query, maxDepth, filter, targetQuery,
                    targetFilter, getQueries) +
                (False,)).next()
        except StopIteration:
            return False
        else:
            return True