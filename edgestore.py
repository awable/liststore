from itertools import chain
from lib.edgestore.db import DB
from lib.edgestore.config import DATABASE_HOSTS
from contextlib import contextmanager

class EdgeStore(object):

    # keep ids in 32 bit range for the time being
    _MAX_COLO_ID = (1 << 32) - 1
    _NUM_HOSTS = len(DATABASE_HOSTS)

    _instances = {}

    @staticmethod
    def getInstance(dbname='edgestore'):
        instance =  EdgeStore._instances.get(dbname)
        if not instance:
            instance = EdgeStore(dbname)
        return instance

    def __init__(self, dbname):
        self._dbname = dbname
        self._shards = {}
        self._locked_colos = set()
        self.lastAddWasOverwrite = False

    def colo(self, gid):
        return gid >> 32

    def generateGid(self, colo_gid=None, colo=None):
        assert not (colo_gid and colo), "cannot specify both colo and colo_gid"
        colo = (
            colo
            or (colo_gid and self.colo(colo_gid))
            or random.randrange(1, self._MAX_COLO_ID + 1))
        return self._getColoShard(colo).generateGid(colo)

    def add(self, edgetype, gid1, gid2, encoding, data, indices=[], overwrite=False):
        shard = self._getShard(gid1)
        edge = shard.add(edgetype, gid1, gid2, encoding, data, indices, overwrite)
        self.lastAddWasOverwrite = shard.lastAddWasOverwrite
        return edge

    def delete(self, edgetype, gid1, gid2, indextypes=[]):
        return self._getShard(gid1).delete(edgetype, gid1, gid2, indextypes)

    def query(self, edgetype, index=None, gid1=None, colo=None):
        assert not (gid1 and colo), "cannot query with both parent gid and colo"

        if colo or gid1:
            colo = colo or self.colo(gid1)
            return self._getColoShard(colo).query(edgetype, indextype, indexrange, gid1)

        return list(chain.from_iterable([
            self._getHostShard(hostindex).query(edgetype, indextype, indexrange, None)
            for hostindex in range(self._NUM_HOSTS)]))

    def get(self, edgetype, gid1, gid2, index=None):
        return self._getShard(gid1).get(edgetype, gid1, gid2, indextype, indexrange)

    def count(self, edgetype, gid1):
        return self._getShard(gid1).count(edgetype, gid1)

    def insideLock(self):
        return len(self._locks)

    def isLocked(self, edgetype, gid1):
        return (edgetype, gid1) in self._locks

    @contextmanager
    def lock(self, colo):
        if colo in self._locked_colos:
            # nested locks are noops
            yield
            return

        try:
            self._locked_colos.add(colo)
            shard = self._getColoShard(colo)
            with shard.transaction():
                yield shard.lock(colo)
        finally:
            self._locked_colos.remove(colo)

    def _getShard(self, gid):
        return self._getColoShard(self.colo(gid))

    def _getColoShard(self, colo):
        return self._getHostShard(colo % self.NUM_HOSTS)

    def _getHostShard(self, hostindex):
        db = DB.getInstance(DATABASE_HOSTS[hostindex], self._dbname)
        shard = self._shards.get(db)
        if not shard:
            shard = self._shards[db] = EdgeStoreShard(db)
        return shard

class EdgeStoreShard(object):

    def __init__(self, db):
        self._db = db
        self.lastAddWasOverwrite = False


    _generateGidSQL = """
       INSERT INTO colo
       (`colo`, `counter`)
       VALUES (%s, LAST_INSERT_ID(%s))
       ON DUPLICATE KEY
       UPDATE counter = LAST_INSERT_ID(counter + 1)
    """

    def generateGid(self, colo, start=1):
        self._db.run(_generateGidSQL, colo, start)
        return (colo << 32) + self._db.getLastInsertID()

    _addSQL = """
      INSERT INTO edgedata
      (edgetype, gid1, gid2, revision, encoding, data)
      VALUES (%s, %s, %s, LAST_INSERT_ID(%s), %s, %s)
    """

    _addOverwriteSQL = """
      INSERT INTO edgedata
      (edgetype, gid1, gid2, revision, encoding, data)
      VALUES (%s, %s, %s, LAST_INSERT_ID(%s), %s, %s)
      ON DUPLICATE KEY
      UPDATE data = VALUES(data),
         revision = LAST_INSERT_ID(revision),
         revision = VALUES(revision),
         encoding = VALUES(encoding),
             data = VALUES(data)
    """

    _uniqueIndexSQL = """
      SELECT COUNT(1)
      FROM edgeindex
      WHERE indextype = %s and indexvalue = %s
    """

    _deleteIndexSQL = """
      DELETE FROM edgeindex
      WHERE indextype = %s
        AND gid1 = %s
        AND revision = %s
    """

    _addIndexSQL = """
      INSERT INTO edgeindex
      (indextype, indexvalue, gid1, revision)
      VALUES (%s, %s, %s, %s)
    """

    def add(self, edgetype, gid1, gid2, encoding, data, indices=[], overwrite=False):
        with self._db.transaction():

            # get new revision
            revision = self._incrementRevision(edgetype, gid1)
            edgedata = (edgetype, gid1, gid2, revision, encoding, data)

            add_sql = EdgeStoreShard._addOverwriteSQL if overwrite else EdgeStoreShard._addSQL
            self._db.run(add_sql, edgedata)

            affected_rows = self._db.getAffectedRows()
            prev_revision = self._db.getLastInsertID()

            # if the edge was added, increment edge count
            if affected_rows == 1:
                self._incrementCount(edgetype, gid1)
                assert prev_revision == revision, "added edge should not have a previous revision"
            elif affected_rows == 2:
                self.lastAddWasOverwrite = True
                assert prev_revision == (revision - 1), "data changed during update"

            for indextype, indexvalue, unique in indices:

                # if the edge already existed, delete old indices
                if affected_rows == 2:
                    self._db.run(EdgeStoreShard._deleteIndexSQL, (indextype, gid1, prev_revision))

                if unique:
                    count = self._db.run(EdgeStoreShard._uniqueIndexSQL, (indextype, indexvalue))
                    assert not count, "edge violates index uniqueness"

                self._db.run(EdgeStoreShard._addIndexSQL, (indextype, indexvalue, gid1, revision))

            return edgedata

    _deleteSQL = """
      DELETE FROM edgedata
      WHERE edgetype = %s
        AND gid1 = %s
        AND gid2 = %s
        AND revision = LAST_INSERT_ID(revision)
    """

    _lastInsertIDSQL = "SELECT LAST_INSERT_ID()"

    def delete(self, edgetype, gid1, gid2, indextypes=[]):
        with self._db.transaction():

            # increment revision since we are making a change
            self._incrementRevision(edgetype, gid1)
            self._db.run(EdgeStoreShard._deleteSQL, (edgetype, gid1, gid2))
            affected_rows = self._db.getAffectedRows()

            # _mysql API doesn't update insertid on DELETE statements so we
            # explicitly fetch the LAST_INSERT_ID()
            del_revision = self._db.getOne(EdgeStoreShard._lastInsertIDSQL)[0]

            # decrement edge count and remove old indices if we actually deleted something
            if affected_rows:
                self._incrementCount(edgetype, gid1, -1)

                assert del_revision, "missing revision for deleted edge"
                for indextype in indextypes:
                    self._db.run(EdgeStoreShard._deleteIndexSQL, (indextype, gid1, del_revision))

            return (affected_rows == 1)


    _listSQL = """
      SELECT edgetype, gid1, gid2, revision, encoding, data
      FROM edgedata
      WHERE edgetype = %s
        AND gid1 = %s
      ORDER BY revision DESC
    """

    _listIndexSQL = """
      SELECT edgedata.edgetype,
             edgedata.gid1,
             edgedata.gid2,
             edgedata.revision,
             edgedata.encoding,
             edgedata.data
      FROM edgeindex STRAIGHT_JOIN edgedata
      ON (edgedata.edgetype = %s
        AND edgedata.gid1 = %s
        AND edgedata.revision = edgeindex.revision)
      WHERE edgeindex.indextype = %s
        AND edgeindex.indexvalue BETWEEN %s AND %s
      ORDER BY edgeindex.indexvalue, edgeindex.revision DESC
    """

    _searchIndexSQL = """
      SELECT edgedata.edgetype,
             edgedata.gid1,
             edgedata.gid2,
             edgedata.revision,
             edgedata.encoding,
             edgedata.data
      FROM edgeindex STRAIGHT_JOIN edgedata
      ON (edgedata.edgetype = %s
        AND edgedata.gid1 = edgeindex.gid1
        AND edgedata.revision = edgeindex.revision)
      WHERE edgeindex.indextype = %s
        AND edgeindex.indexvalue BETWEEN %s and %s
      ORDER BY edgeindex.indexvalue, edgeindex.revision DESC
    """

    def query(self, edge_type, indextype, indexrange, gid1=None):
        if gid1 and indextype:
            indexstart, indexend = indexrange
            query = EdgeStoreShard._listIndexSQL
            args = (edge_type, gid1, indextype, indexstart, indexend)
        elif gid1:
            query = EdgeStoreShard._listSQL
            args = (edge_type, gid1)
        else:
            indexstart, indexend = indexrange
            query = EdgeStoreShard._searchIndexSQL
            args = (edge_type, indextype, indexstart, indexend)

        return self._db.get(query, args)

    _getSQL = """
      SELECT edgetype, gid1, gid2, revision, encoding, data
      FROM edgedata
      WHERE edgetype = %s
        AND gid1 = %s
        AND gid2 = %s
    """

    _getIndexSQL = """
      SELECT edgedata.edgetype,
             edgedata.gid1,
             edgedata.gid2,
             edgedata.revision,
             edgedata.encoding,
             edgedata.data
      FROM edgeindex
      STRAIGHT_JOIN edgedata
      ON (edgedata.edgetype = %s
        AND edgedata.gid1 = %s
        AND edgedata.gid2 = %s
        AND edgedata.revision = edgeindex.revision)
      WHERE edgeindex.indextype = %s
        AND edgeindex.indexvalue BETWEEN %s AND %s
    """

    def get(self, edge_type, gid1, gid2, indextype=None, indexrange=None):
        if indextype:
            indexstart, indexend = indexrange
            query = EdgeStoreShard._getIndexSQL
            args = (edge_type, gid1, gid2, indextype, indexstart, indexend)
        else:
            query = EdgeStoreShard._getSQL
            args = (edge_type, gid1, gid2)

        return self._db.getOne(query, args)


    _countSQL = """
      SELECT `count` from edgemeta
      WHERE edgetype = %s AND gid1 = %s
    """

    def count(self, edgetype, gid1):
        row = self._db.getOne(EdgeStoreShard._countSQL, (edgetype, gid1))
        return row[0] if row else 0

    def lock(self, colo):
        assert self._db.hasOngoingTransaction()
        return self.generateGid(colo, start=0)

    def transaction(self):
        return self._db.transaction()

    _incrementRevisionSQL = """
        INSERT INTO edgemeta
        (edgetype, gid1, revision, count)
        VALUES (%s, %s, LAST_INSERT_ID(1), 0)
        ON DUPLICATE KEY
        UPDATE revision = LAST_INSERT_ID(revision + 1)
    """

    def _incrementRevision(self, edgetype, gid1):
        self._db.run(EdgeStoreShard._incrementRevisionSQL, (edgetype, gid1))
        return self._db.getLastInsertID()

    _incrementCountSQL = """
        UPDATE edgemeta
        SET `count` = `count` + %s
        WHERE edgetype = %s AND gid1 = %s
    """

    def _incrementCount(self, edgetype, gid1, inc=1):
        self._db.run(EdgeStoreShard._incrementCountSQL, (inc, edgetype, gid1))