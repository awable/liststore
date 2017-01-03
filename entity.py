from lib.edgestore.data import Data
from lib.edgestore.attr import Attr
from lib.edgestore.datametaclass import DataMetaClass

class Entity(Data):

    gid = Attr.PrimaryGid()

    @classmethod
    def getColoGid(cls, attrs):
        colo_gid = None
        if cls.__cologidattrname__:
            colo_gid = attrs.get(cls.__cologidattrname__)
            assert colo_gid, "missing colo gid"
        return colo_gid

    @classmethod
    def add(cls, gid=None, attrs={}):
        gid = gid or Gid.generate(colo_gid=cls.getColoGid(attrs))
        with Data.lock(cls.lock_key(gid)):
            return super(Entity, cls).add(gid, gid, attrs=attrs)

    @classmethod
    def delete(cls, gid):
        return super(Entity, cls).delete(gid, gid)

    def remove(self):
        return self.delete(self.gid)

    @classmethod
    def get(cls, gid, gid2=None):
        result = super(Entity, cls).list(gid)
        return result[0] if result else None